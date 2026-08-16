"""Where a scan result goes when something changed.

Two implementations. `LoggingNotifier` is the default and writes structured
lines to the container log — enough to see the job working, not enough to be
told about anything. `WebhookNotifier` posts JSON to an incoming-webhook URL
(Teams, Slack) and is what makes the daily scan actually reach a person.

Azure Communication Services email would be the production answer for real
notification delivery, and is deliberately not built: it needs another
provisioned resource and a verified sender domain, which is real cost and setup
for a portfolio project whose scan can be routed to an existing chat channel
for nothing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.jobs.obligation_scanner import ScanResult


def summarize(result: ScanResult) -> str:
    """One-line summary, leading with the thing that needs acting on."""
    overdue = len(result.newly_overdue)
    due_soon = len(result.newly_due_soon)
    parts = []
    if overdue:
        parts.append(f"{overdue} newly overdue")
    if due_soon:
        parts.append(f"{due_soon} due soon")
    if result.unscheduled:
        parts.append(f"{len(result.unscheduled)} unscheduled")
    return ", ".join(parts) if parts else "no changes"


def _lines(result: ScanResult, limit: int = 20) -> list[str]:
    """Detail lines, overdue first — the ordering a reader needs, not the
    order the scan happened to produce them in."""
    ordered = result.newly_overdue + result.newly_due_soon
    lines = [
        f"[{o.state.value}] {o.obligor_party}: {o.description} "
        f"(due {o.next_due_date or o.due_date}, contract {o.contract_id})"
        for o in ordered[:limit]
    ]
    if len(ordered) > limit:
        lines.append(f"...and {len(ordered) - limit} more")
    return lines


class LoggingNotifier:
    """Default. Visible in container logs; reaches nobody."""

    async def notify(self, result: ScanResult) -> None:
        logger.info("obligation scan: %s", summarize(result))
        for line in _lines(result):
            logger.info("  %s", line)


class WebhookNotifier:
    """Posts to an incoming-webhook URL.

    Failures are logged and swallowed: a chat outage must not fail the scan or
    dead-letter its job execution. The state transitions are already persisted
    by the time this runs, so a missed message costs visibility for one day,
    while a raised exception would make the run look like it did no work.
    """

    def __init__(self, url: str, *, timeout: float = 10.0):
        self._url = url
        self._timeout = timeout

    async def notify(self, result: ScanResult) -> None:
        import httpx

        payload = {
            "text": f"ClauseWatch daily scan — {summarize(result)}",
            "detail": _lines(result),
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(self._url, json=payload)
                response.raise_for_status()
        except Exception:
            logger.exception("notification delivery failed; scan results are still persisted")
