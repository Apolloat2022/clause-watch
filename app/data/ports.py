"""Storage and messaging ports.

The pipeline depends on these Protocols, never on an Azure SDK type. That is
what lets the ingest path be built and tested end to end with no subscription —
the local adapters in `app/data/local.py` satisfy the same interfaces the Azure
ones do, and the pipeline cannot tell them apart.

Protocols rather than ABCs so the local adapters don't inherit anything and
stay readable as plain classes.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.domain.models import Clause, Contract, Obligation


@runtime_checkable
class BlobStore(Protocol):
    """The immutable raw-PDF store."""

    async def put(self, name: str, data: bytes) -> str:
        """Store bytes and return a URI. Overwriting an existing name is an
        error — contracts are write-once so extraction is always re-runnable
        from the original bytes."""
        ...

    async def get(self, uri: str) -> bytes: ...


@runtime_checkable
class ContractRepository(Protocol):
    async def upsert(self, contract: Contract) -> None: ...

    async def get(self, contract_id: str) -> Contract | None: ...

    async def find_by_hash(self, content_hash: str) -> Contract | None:
        """Backs upload idempotency: identical bytes must not start a second
        extraction."""
        ...

    async def list(self, *, status: str | None = None) -> list[Contract]: ...


@runtime_checkable
class ClauseRepository(Protocol):
    async def replace_for_contract(self, contract_id: str, clauses: list[Clause]) -> None:
        """Replace wholesale rather than append.

        Re-ingesting after a chunker change must not leave the old clauses
        behind alongside the new ones — obligations would then cite clause ids
        that no longer correspond to anything.
        """
        ...

    async def list_for_contract(self, contract_id: str) -> list[Clause]: ...

    async def search(
        self, contract_id: str, query_vector: list[float], *, k: int = 5
    ) -> list[tuple[Clause, float]]:
        """Nearest clauses within one contract, best first, with their scores.

        Scoped to a contract rather than the corpus: retrieval exists to ground
        an extraction or answer a question about *this* agreement, and it keeps
        the query inside a single partition.
        """
        ...


@runtime_checkable
class ObligationRepository(Protocol):
    async def replace_for_contract(
        self, contract_id: str, obligations: list[Obligation]
    ) -> None:
        """Replace wholesale, same reasoning as ClauseRepository: a re-extraction
        after a prompt change must not leave the previous run's obligations
        beside the new ones."""
        ...

    async def list_for_contract(self, contract_id: str) -> list[Obligation]: ...

    async def get(self, obligation_id: str) -> Obligation | None:
        """Fetch one obligation.

        The contract id is recoverable from the obligation id
        (`{contract_id}:ob:{n}`), so this stays a single-partition point read
        rather than a cross-partition scan.
        """
        ...

    async def upsert(self, obligation: Obligation) -> None:
        """Write one obligation. Used by the scanner for state transitions and
        by the satisfy/waive endpoints — both change a single row, so wholesale
        replacement would be the wrong tool."""
        ...

    async def list_active(self) -> list[Obligation]:
        """Every obligation not in a terminal state.

        Cross-partition by necessity: the scanner has no contract to scope to.
        Bounded in practice by excluding SATISFIED/WAIVED/SUPERSEDED, which is
        where obligations accumulate over time.
        """
        ...

    async def list_due_before(self, cutoff: str) -> list[Obligation]:
        """Cross-partition by design — the scanner's query (phase 5). See
        ARCHITECTURE.md section 4 for the partition-key tradeoff."""
        ...


@runtime_checkable
class AuditLog(Protocol):
    async def record(
        self, *, contract_id: str, action: str, actor: str, detail: dict | None = None
    ) -> None: ...


@runtime_checkable
class Notifier(Protocol):
    async def notify(self, result) -> None:
        """Deliver a scan summary. Implementations must not raise — a delivery
        failure costs one day's visibility, while an exception would make a
        successful scan look like a failed job."""
        ...


@runtime_checkable
class LeasedMessage(Protocol):
    """A received message that is still owned by the broker until settled.

    The lease exists because the earlier `receive() -> list[str]` shape could
    not be implemented correctly over Service Bus: returning the id would mean
    settling on receipt, so a crash mid-ingest would drop the contract
    silently. A deque hid that; a real broker does not.
    """

    @property
    def contract_id(self) -> str: ...

    async def complete(self) -> None:
        """Done — remove it from the queue for good."""
        ...

    async def abandon(self) -> None:
        """Failed — release it for redelivery.

        Increments the broker's delivery count, so a permanently-poison
        document eventually reaches the dead-letter queue rather than looping.
        """
        ...


@runtime_checkable
class IngestQueue(Protocol):
    async def enqueue(self, contract_id: str) -> None: ...

    async def receive(self, max_messages: int = 1) -> list[LeasedMessage]:
        """Take up to `max_messages` leases. Each must be completed or
        abandoned by the caller; dropping one on the floor stalls it until the
        lock expires."""
        ...
