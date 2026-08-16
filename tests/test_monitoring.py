"""Recurrence parsing, the obligation state machine, and the scanner.

Two behaviors here matter more than the rest and are tested hardest:

* A terminal state is never revisited. A scanner that reopens a satisfied
  obligation trains people to ignore it, which defeats the whole system.
* An event-anchored rule is never given an invented date. "Within 30 days of
  each invoice" needs an invoice feed; guessing would put a fabricated deadline
  in front of someone.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.domain.models import Obligation, ObligationState, ObligationType
from app.domain.recurrence import add_months, next_occurrence, parse_recurrence
from app.jobs.obligation_scanner import effective_due_date, next_state, scan

TODAY = date(2026, 6, 15)


def obligation(**overrides) -> Obligation:
    now = datetime.now(UTC)
    payload = {
        "id": "c:ob:0",
        "contract_id": "c",
        "description": "Customer shall pay each invoice.",
        "obligor_party": "Customer",
        "obligation_type": ObligationType.PAYMENT,
        "cited_clause_ids": ["c:1"],
        "confidence": 0.9,
        "model_version": "m",
        "prompt_version": "v1",
        "created_at": now,
        "updated_at": now,
    }
    payload.update(overrides)
    return Obligation(**payload)


# ----------------------------------------------------------- recurrence


@pytest.mark.parametrize(
    ("text", "months", "days"),
    [
        ("quarterly", 3, 0),
        ("each quarter", 3, 0),
        ("monthly", 1, 0),
        ("annually", 12, 0),
        ("per annum", 12, 0),
        ("semi-annually", 6, 0),
        ("every 6 months", 6, 0),
        ("every three months", 3, 0),
        ("weekly", 0, 7),
        ("every 10 days", 0, 10),
        ("fortnightly", 0, 14),
    ],
)
def test_calendar_periodic_rules_are_schedulable(text, months, days):
    recurrence = parse_recurrence(text)
    assert recurrence.schedulable
    assert (recurrence.months, recurrence.days) == (months, days)


@pytest.mark.parametrize(
    "text",
    [
        "within 30 days of each invoice",
        "within thirty (30) days of receipt",
        "no later than 5 business days after the end of each quarter",
        "prior to the expiry of the Term",
    ],
)
def test_event_anchored_rules_are_not_schedulable(text):
    # The scope boundary from ARCHITECTURE.md section 8. These need an event
    # feed; parsing harder does not help.
    recurrence = parse_recurrence(text)
    assert recurrence.event_relative
    assert not recurrence.schedulable


def test_event_anchored_rule_beats_a_period_word_in_the_same_phrase():
    # "quarterly" appears, but the rule is anchored to the quarter *ending* —
    # a naive period match would schedule it and invent a date.
    recurrence = parse_recurrence("within 5 days of the end of each quarter")
    assert not recurrence.schedulable


def test_unrecognized_phrase_is_unscheduled_not_guessed():
    recurrence = parse_recurrence("as and when reasonably requested")
    assert recurrence is not None
    assert not recurrence.schedulable
    assert not recurrence.event_relative


def test_no_recurrence_text_returns_none():
    assert parse_recurrence(None) is None
    assert parse_recurrence("   ") is None


# ------------------------------------------------------ date arithmetic


def test_add_months_clamps_to_the_end_of_a_short_month():
    # Rolling over to 3 March would drift a monthly obligation forward a few
    # days every short month.
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)  # leap year


def test_add_months_crosses_year_boundaries():
    assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)


def test_next_occurrence_projects_from_the_anchor():
    quarterly = parse_recurrence("quarterly")
    assert next_occurrence(quarterly, anchor=date(2026, 1, 1), after=TODAY) == date(2026, 7, 1)


def test_next_occurrence_on_a_long_dormant_contract_converges():
    # A weekly obligation on a contract signed years ago is hundreds of
    # periods away; the closed form must not be a loop that times out.
    weekly = parse_recurrence("weekly")
    result = next_occurrence(weekly, anchor=date(2015, 1, 1), after=TODAY)
    assert result > TODAY
    assert (result - date(2015, 1, 1)).days % 7 == 0


def test_next_occurrence_before_the_anchor_returns_the_anchor():
    monthly = parse_recurrence("monthly")
    assert next_occurrence(monthly, anchor=date(2027, 1, 1), after=TODAY) == date(2027, 1, 1)


def test_next_occurrence_of_an_unschedulable_rule_is_none():
    rule = parse_recurrence("within 30 days of each invoice")
    assert next_occurrence(rule, anchor=date(2026, 1, 1), after=TODAY) is None


# --------------------------------------------------------- state machine


@pytest.mark.parametrize(
    ("due", "expected"),
    [
        (date(2026, 6, 14), ObligationState.OVERDUE),
        (date(2026, 6, 15), ObligationState.DUE_SOON),  # today counts as due soon
        (date(2026, 7, 15), ObligationState.DUE_SOON),  # exactly 30 days out
        (date(2026, 7, 16), ObligationState.OPEN),
        (None, ObligationState.OPEN),
    ],
)
def test_state_boundaries(due, expected):
    assert next_state(due, today=TODAY, due_soon_days=30) is expected


def test_effective_date_prefers_an_explicit_due_date():
    fixed = obligation(due_date=date(2026, 9, 1), recurrence="quarterly")
    due, unscheduled = effective_due_date(fixed, anchor=date(2026, 1, 1), today=TODAY)
    assert due == date(2026, 9, 1)
    assert unscheduled is False


def test_effective_date_projects_a_recurring_obligation():
    recurring = obligation(recurrence="quarterly")
    due, unscheduled = effective_due_date(recurring, anchor=date(2026, 1, 1), today=TODAY)
    assert due == date(2026, 7, 1)
    assert unscheduled is False


def test_effective_date_flags_an_event_anchored_rule():
    event = obligation(recurrence="within 30 days of each invoice")
    due, unscheduled = effective_due_date(event, anchor=date(2026, 1, 1), today=TODAY)
    assert due is None
    assert unscheduled is True


def test_an_occurrence_falling_exactly_today_is_not_skipped():
    # `after` is yesterday for precisely this case; using today would push a
    # due-today obligation to the next period and hide it.
    daily = obligation(recurrence="every 30 days")
    due, _ = effective_due_date(daily, anchor=date(2026, 5, 16), today=TODAY)
    assert due == TODAY


# --------------------------------------------------------------- scanner


class FakeContracts:
    def __init__(self, effective: date | None = date(2026, 1, 1)):
        self._effective = effective

    async def get(self, contract_id: str):
        now = datetime.now(UTC)
        from app.domain.models import Contract

        return Contract(
            id=contract_id,
            contract_id=contract_id,
            title="t",
            blob_uri="file:///x",
            content_hash="h",
            effective_date=self._effective,
            created_at=now,
            updated_at=now,
        )


class RecordingNotifier:
    def __init__(self):
        self.results = []

    async def notify(self, result) -> None:
        self.results.append(result)


async def test_scanner_moves_a_passed_obligation_to_overdue(deps):
    await deps.obligations.upsert(obligation(due_date=date(2026, 5, 1)))

    result = await scan(
        deps.obligations, contracts_repo=FakeContracts(), today=TODAY, due_soon_days=30
    )

    assert len(result.transitions) == 1
    stored = await deps.obligations.get("c:ob:0")
    assert stored.state is ObligationState.OVERDUE
    assert stored.next_due_date == date(2026, 5, 1)


async def test_scanner_never_revisits_a_terminal_state(deps):
    # The invariant the whole job rests on. A satisfied obligation whose date
    # has long passed must stay satisfied.
    await deps.obligations.upsert(
        obligation(due_date=date(2026, 1, 1), state=ObligationState.SATISFIED)
    )

    result = await scan(
        deps.obligations, contracts_repo=FakeContracts(), today=TODAY, due_soon_days=30
    )

    assert result.transitions == []
    assert (await deps.obligations.get("c:ob:0")).state is ObligationState.SATISFIED


async def test_scanner_reports_unschedulable_rules_without_inventing_dates(deps):
    await deps.obligations.upsert(obligation(recurrence="within 30 days of each invoice"))

    result = await scan(
        deps.obligations, contracts_repo=FakeContracts(), today=TODAY, due_soon_days=30
    )

    assert len(result.unscheduled) == 1
    stored = await deps.obligations.get("c:ob:0")
    assert stored.next_due_date is None
    assert stored.state is ObligationState.OPEN


async def test_scanner_is_idempotent(deps):
    await deps.obligations.upsert(obligation(due_date=date(2026, 6, 20)))
    kwargs = {"contracts_repo": FakeContracts(), "today": TODAY, "due_soon_days": 30}

    first = await scan(deps.obligations, **kwargs)
    second = await scan(deps.obligations, **kwargs)

    # A daily cron re-running over a stable corpus must not emit a fresh
    # notification every morning for the same obligation.
    assert len(first.transitions) == 1
    assert second.transitions == []


async def test_scanner_notifies_only_when_something_changed(deps):
    await deps.obligations.upsert(obligation(due_date=date(2026, 5, 1)))
    notifier = RecordingNotifier()
    kwargs = {
        "contracts_repo": FakeContracts(),
        "notifier": notifier,
        "today": TODAY,
        "due_soon_days": 30,
    }

    await scan(deps.obligations, **kwargs)
    await scan(deps.obligations, **kwargs)

    assert len(notifier.results) == 1
    assert len(notifier.results[0].newly_overdue) == 1


async def test_scanner_falls_back_to_ingest_time_when_no_effective_date(deps):
    # A guess about the parties' clock, but a bounded one — better than
    # refusing to schedule every recurring obligation on a contract whose
    # effective date the extractor did not find.
    await deps.obligations.upsert(obligation(recurrence="monthly"))

    result = await scan(
        deps.obligations, contracts_repo=FakeContracts(effective=None), today=TODAY,
        due_soon_days=30,
    )

    assert result.unscheduled == []
    assert (await deps.obligations.get("c:ob:0")).next_due_date is not None


# ------------------------------------------------------------------- api


async def test_satisfy_marks_terminal_and_audits(api_client):
    deps = api_client.deps
    await deps.obligations.upsert(obligation(due_date=date(2026, 5, 1)))

    response = await api_client.post(
        "/api/v1/obligations/c:ob:0/satisfy", json={"note": "paid 2026-05-02"}
    )

    assert response.status_code == 200
    assert response.json()["state"] == "SATISFIED"
    assert any(e["action"] == "SATISFIED" for e in deps.audit.entries)


async def test_satisfying_twice_is_a_no_op_not_an_error(api_client):
    await api_client.deps.obligations.upsert(obligation())
    await api_client.post("/api/v1/obligations/c:ob:0/satisfy", json={})

    # A retried click should not read as a failure.
    second = await api_client.post("/api/v1/obligations/c:ob:0/satisfy", json={})
    assert second.status_code == 200


async def test_waiving_a_satisfied_obligation_is_a_conflict(api_client):
    await api_client.deps.obligations.upsert(obligation())
    await api_client.post("/api/v1/obligations/c:ob:0/satisfy", json={})

    response = await api_client.post("/api/v1/obligations/c:ob:0/waive", json={})

    # Terminal states are not interchangeable — silently overwriting one
    # decision with another would erase the record of what was decided.
    assert response.status_code == 409


async def test_decision_on_unknown_obligation_is_404(api_client):
    assert (await api_client.post("/api/v1/obligations/nope:ob:0/waive", json={})).status_code == 404
