"""Turning a recurrence phrase into a schedule — or refusing to.

The extractor deliberately never computes dates: when a contract says "within
30 days of each invoice" it records the rule verbatim and leaves `due_date`
empty. This module decides which of those rules the scanner can actually put on
a calendar.

**The line, drawn explicitly.** A rule is schedulable only when it is
*calendar-periodic* — "quarterly", "every 6 months", "annually" — because those
can be projected forward from a fixed anchor. A rule anchored to an event the
system does not observe is not schedulable, and no amount of parsing changes
that: "within 30 days of each invoice" needs an invoice feed, and inventing a
date for it would put a fabricated deadline in front of someone. Those stay
unscheduled and are reported as such.

This is the scope boundary flagged in ARCHITECTURE.md section 8. Widening it
means ingesting the events, not improving the regexes.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

# Checked first and on purpose: "within 30 days of each invoice" also contains
# a number and a unit, so a naive period match would happily schedule it.
_EVENT_RELATIVE = re.compile(
    r"\b(within|no later than|not later than|prior to|before|after|following)\b"
    r".{0,40}?\b(of|after|following|from|preceding)\b",
    re.IGNORECASE | re.DOTALL,
)

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}

_NAMED_PERIODS: list[tuple[re.Pattern, int, int]] = [
    # (pattern, months, days)
    (re.compile(r"\b(semi[- ]?annual(ly)?|twice (a |per )?year|bi[- ]?annual(ly)?)\b", re.IGNORECASE), 6, 0),
    (re.compile(r"\b(annual(ly)?|yearly|per annum|each year|every year)\b", re.IGNORECASE), 12, 0),
    (re.compile(r"\b(quarterly|each quarter|every quarter|per quarter)\b", re.IGNORECASE), 3, 0),
    (re.compile(r"\b(monthly|each month|every month|per month)\b", re.IGNORECASE), 1, 0),
    (re.compile(r"\b(fortnightly|bi[- ]?weekly|every two weeks)\b", re.IGNORECASE), 0, 14),
    (re.compile(r"\b(weekly|each week|every week|per week)\b", re.IGNORECASE), 0, 7),
]

_EVERY_N = re.compile(
    r"\bevery\s+(?P<n>\d{1,3}|" + "|".join(_WORD_NUMBERS) + r")\s+"
    r"(?P<unit>day|week|month|quarter|year)s?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Recurrence:
    """A parsed recurrence phrase.

    `months` and `days` are alternatives, not additive — a rule is expressed in
    one unit or the other. Both zero means the phrase was understood as
    non-schedulable (or not understood at all, which is treated the same way:
    unscheduled and reported, never guessed).
    """

    raw: str
    months: int = 0
    days: int = 0
    event_relative: bool = False

    @property
    def schedulable(self) -> bool:
        return not self.event_relative and (self.months > 0 or self.days > 0)


def parse_recurrence(text: str | None) -> Recurrence | None:
    """Parse a recurrence phrase. None in, None out."""
    if not text or not text.strip():
        return None
    raw = text.strip()

    if _EVENT_RELATIVE.search(raw):
        return Recurrence(raw=raw, event_relative=True)

    match = _EVERY_N.search(raw)
    if match:
        token = match.group("n").lower()
        n = int(token) if token.isdigit() else _WORD_NUMBERS[token]
        unit = match.group("unit").lower()
        if unit == "day":
            return Recurrence(raw=raw, days=n)
        if unit == "week":
            return Recurrence(raw=raw, days=n * 7)
        if unit == "month":
            return Recurrence(raw=raw, months=n)
        if unit == "quarter":
            return Recurrence(raw=raw, months=n * 3)
        return Recurrence(raw=raw, months=n * 12)

    for pattern, months, days in _NAMED_PERIODS:
        if pattern.search(raw):
            return Recurrence(raw=raw, months=months, days=days)

    # Understood as a phrase, not understood as a schedule.
    return Recurrence(raw=raw)


def add_months(start: date, months: int) -> date:
    """Calendar month arithmetic, clamping the day to the target month.

    31 January plus one month is 28 (or 29) February — the last day of the
    month, not 3 March. Rolling over would drift a monthly obligation forward
    by a few days every short month.
    """
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def next_occurrence(recurrence: Recurrence, *, anchor: date, after: date) -> date | None:
    """The first occurrence strictly after `after`, projected from `anchor`.

    Returns None for a rule that cannot be scheduled. `anchor` is normally the
    contract's effective date — the point the parties' clock starts from.
    """
    if not recurrence.schedulable:
        return None

    if recurrence.days:
        if anchor > after:
            return anchor
        # Closed form rather than a loop: a weekly obligation on a contract
        # signed years ago is hundreds of iterations otherwise.
        elapsed = (after - anchor).days
        periods = elapsed // recurrence.days + 1
        return anchor + timedelta(days=recurrence.days * periods)

    candidate = anchor
    # Jump most of the way in one step, then step month-by-month: the clamping
    # in add_months makes the sequence non-uniform, so the estimate can land
    # slightly early or late and must be corrected either way.
    if candidate <= after:
        estimate = ((after.year - anchor.year) * 12 + (after.month - anchor.month))
        periods = max(estimate // recurrence.months - 1, 0)
        candidate = add_months(anchor, recurrence.months * periods)

    guard = 0
    while candidate <= after:
        candidate = add_months(candidate, recurrence.months)
        guard += 1
        if guard > 1200:  # a century of monthly occurrences
            raise RuntimeError(f"recurrence did not converge: {recurrence!r}")
    return candidate
