"""Container Apps Job, KEDA-triggered off the Service Bus ingest queue.

One message = one contract, processed end to end by `pipeline.run_ingest`.

The job drains and exits rather than looping forever: KEDA starts a fresh
execution per batch of queue messages, so a long-lived loop would hold a
replica open (and bill for it) waiting on an empty queue.

Failure handling is deliberately split. `run_ingest` marks the contract FAILED
with a reason, which is the user-visible outcome; this loop then re-raises so
the platform sees a non-zero exit and the message follows its normal delivery-
count path to the dead-letter queue. Swallowing the error here would leave a
poison PDF looking like a clean run.
"""

from __future__ import annotations

import asyncio
import logging

from app.config import settings
from app.deps import Dependencies, build_dependencies
from app.ingest.pipeline import run_ingest

logger = logging.getLogger(__name__)

# Bounded so one execution can't run past the job's timeout on a deep backlog;
# KEDA simply starts another execution for the remainder.
MAX_MESSAGES_PER_RUN = 20


async def drain(deps: Dependencies, *, max_messages: int = MAX_MESSAGES_PER_RUN) -> int:
    """Process queued contracts until the queue is empty or the cap is hit.

    Returns the number processed successfully. Raises on the first failure,
    after the contract has been marked FAILED.
    """
    processed = 0
    while processed < max_messages:
        leases = await deps.queue.receive(max_messages=1)
        if not leases:
            break
        for lease in leases:
            contract_id = lease.contract_id
            logger.info("ingest starting", extra={"contract_id": contract_id})
            try:
                await run_ingest(
                    contract_id,
                    contracts=deps.contracts,
                    clauses=deps.clauses,
                    blobs=deps.blobs,
                    analyzer=deps.analyzer,
                    audit=deps.audit,
                    obligations=deps.obligations,
                    model=deps.model,
                    embedder=deps.embedder,
                    model_version=settings.extraction_model,
                    prompt_version=settings.prompt_version,
                )
            except Exception:
                # Abandon before re-raising: the contract is already marked
                # FAILED with a reason by run_ingest, and abandoning bumps the
                # broker's delivery count so a poison document eventually
                # dead-letters instead of looping.
                await lease.abandon()
                raise
            await lease.complete()
            processed += 1
    return processed


async def main() -> None:
    logging.basicConfig(level=settings.log_level)
    deps = build_dependencies(settings)
    try:
        count = await drain(deps)
        logger.info("ingest job complete", extra={"processed": count})
    finally:
        # The job is a short-lived process, but Service Bus and Cosmos both
        # hold credentials with background refresh tasks — exiting without
        # closing them produces unclosed-session warnings and, on Service Bus,
        # can delay lock release.
        await deps.aclose()


if __name__ == "__main__":
    asyncio.run(main())
