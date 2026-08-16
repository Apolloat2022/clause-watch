"""Local adapters — filesystem blobs, in-memory everything else.

These exist so the whole ingest path runs on a laptop with no Azure resources:
`uvicorn app.main:app`, POST a PDF, watch clauses appear. They are also what
the tests run against, which keeps the suite fast and offline.

They are explicitly **not** production stand-ins. Nothing here is durable
across a restart except the blobs, `InMemoryIngestQueue` has no lease or
dead-letter semantics, and every repository is a dict. `app.data.azure_*`
carries the real implementations of the same ports.
"""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from app.domain.models import Clause, Contract, Obligation

LOCAL_ROOT = Path(".local")


class LocalBlobStore:
    """Writes to `.local/blobs/`. Gitignored."""

    def __init__(self, root: Path | None = None):
        self._root = (root or LOCAL_ROOT / "blobs").resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, name: str) -> Path:
        # `name` is derived from a client-supplied filename, so it is untrusted:
        # resolve and confine to the root, or a crafted name walks out of it.
        candidate = (self._root / name).resolve()
        if not candidate.is_relative_to(self._root):
            raise ValueError(f"blob name escapes the store root: {name!r}")
        return candidate

    async def put(self, name: str, data: bytes) -> str:
        path = self._path_for(name)
        if path.exists():
            raise FileExistsError(f"blob already exists: {name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)
        return path.as_uri()

    async def get(self, uri: str) -> bytes:
        # Not a string strip: `file:///tmp/x` minus `file:///` is `tmp/x`, a
        # *relative* path, so every read fails off a POSIX absolute path. It
        # survives on Windows only because `file:///C:/x` happens to leave a
        # usable `C:/x` behind. url2pathname round-trips as_uri() on both, and
        # percent-decodes the names that as_uri() escaped.
        if uri.startswith("file:"):
            path = Path(url2pathname(urlparse(uri).path))
        else:
            path = Path(uri)
        return await asyncio.to_thread(path.read_bytes)


class InMemoryContractRepository:
    def __init__(self):
        self._by_id: dict[str, Contract] = {}

    async def upsert(self, contract: Contract) -> None:
        self._by_id[contract.contract_id] = contract

    async def get(self, contract_id: str) -> Contract | None:
        return self._by_id.get(contract_id)

    async def find_by_hash(self, content_hash: str) -> Contract | None:
        for contract in self._by_id.values():
            if contract.content_hash == content_hash:
                return contract
        return None

    async def list(self, *, status: str | None = None) -> list[Contract]:
        items = list(self._by_id.values())
        if status is not None:
            items = [c for c in items if c.status.value == status]
        return sorted(items, key=lambda c: c.created_at, reverse=True)


class InMemoryClauseRepository:
    def __init__(self):
        self._by_contract: dict[str, list[Clause]] = {}

    async def replace_for_contract(self, contract_id: str, clauses: list[Clause]) -> None:
        self._by_contract[contract_id] = list(clauses)

    async def list_for_contract(self, contract_id: str) -> list[Clause]:
        return list(self._by_contract.get(contract_id, []))

    async def search(
        self, contract_id: str, query_vector: list[float], *, k: int = 5
    ) -> list[tuple[Clause, float]]:
        """Brute-force cosine over the contract's clauses.

        Linear and unindexed, which is fine for one contract's worth of clauses
        and is exactly what Cosmos's DiskANN index does efficiently at scale.
        Clauses with no embedding are skipped rather than scored as zero — an
        un-embedded clause is missing data, not a poor match.
        """
        from app.ingest.embeddings import cosine

        scored = [
            (clause, cosine(query_vector, clause.embedding))
            for clause in self._by_contract.get(contract_id, [])
            if clause.embedding
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:k]


class InMemoryObligationRepository:
    def __init__(self):
        self._by_contract: dict[str, list[Obligation]] = {}

    async def replace_for_contract(self, contract_id: str, obligations: list[Obligation]) -> None:
        self._by_contract[contract_id] = list(obligations)

    async def list_for_contract(self, contract_id: str) -> list[Obligation]:
        return list(self._by_contract.get(contract_id, []))

    async def get(self, obligation_id: str) -> Obligation | None:
        for obligations in self._by_contract.values():
            for obligation in obligations:
                if obligation.id == obligation_id:
                    return obligation
        return None

    async def upsert(self, obligation: Obligation) -> None:
        bucket = self._by_contract.setdefault(obligation.contract_id, [])
        for index, existing in enumerate(bucket):
            if existing.id == obligation.id:
                bucket[index] = obligation
                return
        bucket.append(obligation)

    async def list_active(self) -> list[Obligation]:
        from app.jobs.obligation_scanner import TERMINAL_STATES

        return [
            obligation
            for obligations in self._by_contract.values()
            for obligation in obligations
            if obligation.state not in TERMINAL_STATES
        ]

    async def list_due_before(self, cutoff: str) -> list[Obligation]:
        return [
            obligation
            for obligations in self._by_contract.values()
            for obligation in obligations
            if obligation.due_date is not None and obligation.due_date.isoformat() < cutoff
        ]


class InMemoryAuditLog:
    def __init__(self):
        self.entries: list[dict] = []

    async def record(
        self, *, contract_id: str, action: str, actor: str, detail: dict | None = None
    ) -> None:
        self.entries.append(
            {"contract_id": contract_id, "action": action, "actor": actor, "detail": detail}
        )


class InMemoryIngestQueue:
    """A deque, not a queue.

    Leases are honored to the extent a deque can: `abandon()` puts the message
    back at the head so a retry loop behaves the way it would against Service
    Bus. What is missing is everything durable — no lock expiry, no delivery
    count, no dead-letter, and nothing survives a restart. A permanently-poison
    document therefore loops here forever instead of dead-lettering.
    """

    def __init__(self):
        self._items: deque[str] = deque()

    async def enqueue(self, contract_id: str) -> None:
        self._items.append(contract_id)

    async def receive(self, max_messages: int = 1) -> list[_LocalLease]:
        leases: list[_LocalLease] = []
        while self._items and len(leases) < max_messages:
            leases.append(_LocalLease(self._items.popleft(), self))
        return leases

    def __len__(self) -> int:
        return len(self._items)


class _LocalLease:
    """In-memory counterpart to a Service Bus message lock."""

    __slots__ = ("_contract_id", "_queue", "_settled")

    def __init__(self, contract_id: str, queue: InMemoryIngestQueue):
        self._contract_id = contract_id
        self._queue = queue
        self._settled = False

    @property
    def contract_id(self) -> str:
        return self._contract_id

    async def complete(self) -> None:
        self._settled = True

    async def abandon(self) -> None:
        if self._settled:
            return
        self._settled = True
        # Head, not tail: preserves ordering on retry, which makes test
        # behavior deterministic.
        self._queue._items.appendleft(self._contract_id)
