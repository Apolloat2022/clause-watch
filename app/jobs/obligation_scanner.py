"""Container Apps Job, cron-triggered daily.

This is the job that justifies the system existing. Extraction is a one-time
act; the failure it exists to fix is a contract read once, eighteen months ago,
that nothing has re-read since.

Two invariants shape the whole module:

**Terminal states are never revisited.** SATISFIED, WAIVED, and SUPERSEDED are
human decisions. A scanner that re-opened a satisfied obligation because a date
passed would be worse than no scanner at all — it would train people to ignore
it, which is exactly the outcome the system exists to prevent.

**An unschedulable rule is reported, never guessed.** "Within 30 days of each
invoice" needs an invoice feed. Obligations like that are counted and surfaced
as unscheduled rather than assigned an invented date (see
`app/domain/recurrence.py`).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from app.config import settings
from app.data.ports import AuditLog, Notifier, ObligationRepository
from app.deps import Dependencies, build_dependencies
from app.domain.models import Obligation, ObligationState
from app.domain.recurrence import next_occurrence, parse_recurrence

logger = logging.getLogger(__name__)

# Set by a person, not by the calendar. The scanner reads them and moves on.
TERMINAL_STATES = frozenset(
    {ObligationState.SATISFIED, ObligationState.WAIVED, ObligationState.SUPERSEDED}
)


@dataclass(slots=True)
class ScanResult:
    scanned: int = 0
    transitions: list[tuple[Obligation, ObligationState]] = field(default_factory=list)
    # Recurring obligations whose rule is anchored to an event we don't observe.
    # Counted rather than silently skipped: a growing number here means the
    # register is quietly going blind to a class of duty.
    unscheduled: list[Obligation] = field(default_factory=list)
    skipped_terminal: int = 0

    @property
    def newly_overdue(self) -> list[Obligation]:
        return [o for o, state in self.transitions if state is ObligationState.OVERDUE]

    @property
    def newly_due_soon(self) -> list[Obligation]:
        return [o for o, state in self.transitions if state is ObligationState.DUE_SOON]


def effective_due_date(
    obligation: Obligation, *, anchor: date, today: date
) -> tuple[date | None, bool]:
    """The date this obligation should be judged against.

    Returns `(due_date, is_unscheduled)`. A fixed `due_date` wins outright. A
    recurring obligation is projected from `anchor`; one whose rule is not
    calendar-periodic comes back `(None, True)` — unscheduled, not overdue.
    """
    if obligation.due_date is not None:
        return obligation.due_date, False

    recurrence = parse_recurrence(obligation.recurrence)
    if recurrence is None:
        # No date and no rule: nothing to judge it against. Not a defect —
        # "Supplier shall maintain insurance throughout the Term" is a standing
        # duty with no deadline.
        return None, False
    if not recurrence.schedulable:
        return None, True

    # `after` is yesterday so an occurrence falling exactly today is returned
    # rather than skipped to the next period.
    return next_occurrence(recurrence, anchor=anchor, after=today - timedelta(days=1)), False


def next_state(due: date | None, *, today: date, due_soon_days: int) -> ObligationState:
    if due is None:
        return ObligationState.OPEN
    if due < today:
        return ObligationState.OVERDUE
    if due <= today + timedelta(days=due_soon_days):
        return ObligationState.DUE_SOON
    return ObligationState.OPEN


async def scan(
    obligations_repo: ObligationRepository,
    *,
    contracts_repo,
    notifier: Notifier | None = None,
    audit: AuditLog | None = None,
    today: date | None = None,
    due_soon_days: int = 30,
) -> ScanResult:
    """Re-evaluate every non-terminal obligation against the calendar.

    `today` is injectable so the state machine can be tested at a fixed date
    rather than against whatever day the suite happens to run on.

    `audit` is optional for the same reason `notifier` is — the state-machine
    tests are about the state machine — but `main()` always supplies it, so
    every transition made by the deployed job is recorded.
    """
    today = today or datetime.now(UTC).date()
    result = ScanResult()

    active = await obligations_repo.list_active()
    anchors: dict[str, date] = {}

    for obligation in active:
        result.scanned += 1

        if obligation.state in TERMINAL_STATES:
            # list_active should already exclude these; counted rather than
            # trusted, because a repository bug here silently reopens
            # closed obligations.
            result.skipped_terminal += 1
            continue

        if obligation.contract_id not in anchors:
            contract = await contracts_repo.get(obligation.contract_id)
            anchors[obligation.contract_id] = (
                contract.effective_date
                if contract is not None and contract.effective_date is not None
                # Falling back to ingest time is a guess about the parties'
                # clock, so it is logged where it matters below.
                else (contract.created_at.date() if contract is not None else today)
            )
        anchor = anchors[obligation.contract_id]

        due, unscheduled = effective_due_date(obligation, anchor=anchor, today=today)
        if unscheduled:
            result.unscheduled.append(obligation)
            continue

        target = next_state(due, today=today, due_soon_days=due_soon_days)
        if target is obligation.state and obligation.next_due_date == due:
            continue

        updated = obligation.model_copy(
            update={
                "state": target,
                "next_due_date": due,
                "updated_at": datetime.now(UTC),
            }
        )
        await obligations_repo.upsert(updated)
        if target is not obligation.state:
            result.transitions.append((updated, target))
            if audit is not None:
                # After the upsert, matching app/ingest/pipeline.py. There is no
                # transaction spanning the two containers either way, so the
                # choice is which direction to be wrong in: this one can miss a
                # row for a change that happened, the other would record a row
                # for a change that did not. A trail that overstates is the
                # worse failure — it is the one nobody thinks to check.
                await audit.record(
                    contract_id=obligation.contract_id,
                    action=target.value,
                    actor="obligation-scanner",
                    detail={
                        "obligation_id": obligation.id,
                        "from_state": obligation.state.value,
                        "due_date": due.isoformat() if due is not None else None,
                    },
                )

    if result.unscheduled:
        logger.warning(
            "%d obligation(s) have event-anchored rules and cannot be scheduled",
            len(result.unscheduled),
        )

    if notifier is not None and result.transitions:
        await notifier.notify(result)

    logger.info(
        "scan complete",
        extra={
            "scanned": result.scanned,
            "transitions": len(result.transitions),
            "unscheduled": len(result.unscheduled),
        },
    )
    return result


async def main() -> None:
    logging.basicConfig(level=settings.log_level)
    deps: Dependencies = build_dependencies(settings)
    try:
        if deps.obligations is None:
            raise RuntimeError("obligation storage is not configured")
        await scan(
            deps.obligations,
            contracts_repo=deps.contracts,
            notifier=deps.notifier,
            audit=deps.audit,
            due_soon_days=settings.due_soon_days,
        )
    finally:
        await deps.aclose()


if __name__ == "__main__":
    asyncio.run(main())
