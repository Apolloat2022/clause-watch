"""Cosmos DB adapters for the contract, clause, and audit containers.

All four containers partition on `/contract_id`, which is the model's own field
name. Storing `model_dump(mode="json")` directly then needs no field mapping
layer — and a mapping layer between the domain model and the stored document is
exactly the kind of code that silently drifts out of sync with the schema it is
supposed to mirror. (ARCHITECTURE.md originally said `/contractId`; changed
here and in the Bicep for this reason — see docs/DECISIONS.md 005.)

One `CosmosClient` per process, built from DefaultAzureCredential. Local auth
is disabled on the account in Bicep, so keys are not merely discouraged — they
do not work.
"""

from __future__ import annotations

import logging

from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential

from app.domain.models import Clause, Contract, Obligation

logger = logging.getLogger(__name__)

CONTRACTS = "contracts"
CLAUSES = "clauses"
OBLIGATIONS = "obligations"
AUDIT = "audit"


class CosmosConnection:
    """Owns the client and hands out container proxies.

    Shared by all three repositories so a process holds one connection rather
    than three, and so `aclose()` has a single owner.
    """

    def __init__(self, endpoint: str, database: str):
        self._credential = DefaultAzureCredential()
        self._client = CosmosClient(url=endpoint, credential=self._credential)
        self._database = self._client.get_database_client(database)

    def container(self, name: str):
        return self._database.get_container_client(name)

    async def aclose(self) -> None:
        await self._client.close()
        await self._credential.close()


class CosmosContractRepository:
    def __init__(self, connection: CosmosConnection):
        self._container = connection.container(CONTRACTS)

    async def upsert(self, contract: Contract) -> None:
        # mode="json" so datetimes and dates become ISO strings; the raw
        # objects are not JSON-serializable and Cosmos would reject them.
        await self._container.upsert_item(contract.model_dump(mode="json"))

    async def get(self, contract_id: str) -> Contract | None:
        try:
            item = await self._container.read_item(item=contract_id, partition_key=contract_id)
        except CosmosResourceNotFoundError:
            return None
        return Contract.model_validate(item)

    async def find_by_hash(self, content_hash: str) -> Contract | None:
        """Cross-partition by necessity: the hash is the lookup key and the
        partition key is the contract id, so there is no partition to scope to.
        Bounded with TOP 1 — this runs on every upload, and it is the one query
        in the ingest path whose RU cost grows with the corpus."""
        query = "SELECT TOP 1 * FROM c WHERE c.content_hash = @hash"
        params = [{"name": "@hash", "value": content_hash}]
        async for item in self._container.query_items(query=query, parameters=params):
            return Contract.model_validate(item)
        return None

    async def list(self, *, status: str | None = None) -> list[Contract]:
        query = "SELECT * FROM c"
        params: list[dict] = []
        if status is not None:
            query += " WHERE c.status = @status"
            params.append({"name": "@status", "value": status})
        query += " ORDER BY c.created_at DESC"
        return [
            Contract.model_validate(item)
            async for item in self._container.query_items(query=query, parameters=params)
        ]


class CosmosClauseRepository:
    def __init__(self, connection: CosmosConnection):
        self._container = connection.container(CLAUSES)

    async def replace_for_contract(self, contract_id: str, clauses: list[Clause]) -> None:
        """Upsert the new set, then delete whatever the old set had beyond it.

        Deliberately not delete-then-insert. Clause ids are deterministic
        (`{contract_id}:{ordinal}`), so upserting overwrites in place and there
        is never a window where a reader sees zero clauses for a contract that
        has some. Only the tail — ordinals the new extraction no longer
        produces — has to be removed.

        Not atomic: a crash between the upserts and the prune leaves stale
        trailing clauses. That is recoverable by re-running ingest, which is
        idempotent, and is the better failure than a contract that momentarily
        appears to have no clauses at all.
        """
        for clause in clauses:
            await self._container.upsert_item(clause.model_dump(mode="json"))

        stale = await self._ordinals_at_or_above(contract_id, len(clauses))
        for clause_id in stale:
            try:
                await self._container.delete_item(item=clause_id, partition_key=contract_id)
            except CosmosResourceNotFoundError:
                pass  # already gone; nothing to do

        if stale:
            logger.info(
                "pruned stale clauses",
                extra={"contract_id": contract_id, "pruned": len(stale)},
            )

    async def _ordinals_at_or_above(self, contract_id: str, minimum: int) -> list[str]:
        query = "SELECT c.id FROM c WHERE c.contract_id = @cid AND c.ordinal >= @min"
        params = [
            {"name": "@cid", "value": contract_id},
            {"name": "@min", "value": minimum},
        ]
        return [
            item["id"]
            async for item in self._container.query_items(
                query=query, parameters=params, partition_key=contract_id
            )
        ]

    async def list_for_contract(self, contract_id: str) -> list[Clause]:
        query = "SELECT * FROM c WHERE c.contract_id = @cid ORDER BY c.ordinal"
        params = [{"name": "@cid", "value": contract_id}]
        return [
            Clause.model_validate(item)
            async for item in self._container.query_items(
                query=query, parameters=params, partition_key=contract_id
            )
        ]

    async def search(
        self, contract_id: str, query_vector: list[float], *, k: int = 5
    ) -> list[tuple[Clause, float]]:
        """DiskANN vector search, scoped to one contract's partition.

        This is what replaces Azure AI Search (ARCHITECTURE.md section 5). The
        container's vector policy declares cosine distance on `/embedding`, and
        `VectorDistance` uses that policy — the function is not told which
        metric to apply, the index is.

        ⚠️ UNVERIFIED AGAINST A LIVE ACCOUNT. `VectorDistance()` has no local
        emulator, so neither the projection syntax nor the sort direction has
        ever executed. The documented pattern is a bare `ORDER BY
        VectorDistance(...)`, which is what is used here. **If results come back
        worst-first, that ordering is the first thing to check** — a reversed
        sort would look like plausible-but-wrong retrieval rather than an error.
        """
        query = (
            "SELECT TOP @k c.id, c.contract_id, c.ordinal, c.heading, c.text, "
            "c.page, c.bounding_box, "
            "VectorDistance(c.embedding, @vec) AS score "
            "FROM c WHERE c.contract_id = @cid "
            "ORDER BY VectorDistance(c.embedding, @vec)"
        )
        params = [
            {"name": "@k", "value": k},
            {"name": "@vec", "value": query_vector},
            {"name": "@cid", "value": contract_id},
        ]
        results: list[tuple[Clause, float]] = []
        async for item in self._container.query_items(
            query=query, parameters=params, partition_key=contract_id
        ):
            score = item.pop("score", 0.0)
            # The projection omits `embedding` deliberately: returning a
            # 1536-float array per hit multiplies the response size by an order
            # of magnitude for data no caller of search() uses.
            results.append((Clause.model_validate(item), float(score)))
        return results


class CosmosObligationRepository:
    def __init__(self, connection: CosmosConnection):
        self._container = connection.container(OBLIGATIONS)

    async def replace_for_contract(self, contract_id: str, obligations: list[Obligation]) -> None:
        """Upsert then prune, mirroring CosmosClauseRepository.

        Obligation ids are deterministic (`{contract_id}:ob:{index}`), so a
        re-extraction overwrites in place and only the tail — indexes the new
        run no longer produced — has to be deleted. Same non-atomicity caveat:
        a crash between the two leaves stale trailing obligations, recoverable
        by re-running ingest.
        """
        for obligation in obligations:
            await self._container.upsert_item(obligation.model_dump(mode="json"))

        stale = await self._ids_beyond(contract_id, len(obligations))
        for obligation_id in stale:
            try:
                await self._container.delete_item(item=obligation_id, partition_key=contract_id)
            except CosmosResourceNotFoundError:
                pass

    async def _ids_beyond(self, contract_id: str, count: int) -> list[str]:
        # Ids end in ":ob:{index}"; anything at or past the new count is stale.
        query = "SELECT c.id FROM c WHERE c.contract_id = @cid"
        params = [{"name": "@cid", "value": contract_id}]
        stale = []
        async for item in self._container.query_items(
            query=query, parameters=params, partition_key=contract_id
        ):
            suffix = item["id"].rsplit(":ob:", 1)
            if len(suffix) == 2 and suffix[1].isdigit() and int(suffix[1]) >= count:
                stale.append(item["id"])
        return stale

    async def list_for_contract(self, contract_id: str) -> list[Obligation]:
        query = "SELECT * FROM c WHERE c.contract_id = @cid"
        params = [{"name": "@cid", "value": contract_id}]
        return [
            Obligation.model_validate(item)
            async for item in self._container.query_items(
                query=query, parameters=params, partition_key=contract_id
            )
        ]

    async def get(self, obligation_id: str) -> Obligation | None:
        """Point read. The contract id is the prefix of the obligation id, so
        the partition key is recoverable without a lookup — this stays a single
        partition read rather than a cross-partition scan."""
        contract_id = obligation_id.split(":ob:", 1)[0]
        try:
            item = await self._container.read_item(
                item=obligation_id, partition_key=contract_id
            )
        except CosmosResourceNotFoundError:
            return None
        return Obligation.model_validate(item)

    async def upsert(self, obligation: Obligation) -> None:
        await self._container.upsert_item(obligation.model_dump(mode="json"))

    async def list_active(self) -> list[Obligation]:
        """Cross-partition, and deliberately so — the scanner has no contract
        to scope to. Excluding terminal states is what keeps it bounded: those
        are where obligations accumulate as a corpus ages."""
        query = "SELECT * FROM c WHERE c.state NOT IN (@satisfied, @waived, @superseded)"
        params = [
            {"name": "@satisfied", "value": "SATISFIED"},
            {"name": "@waived", "value": "WAIVED"},
            {"name": "@superseded", "value": "SUPERSEDED"},
        ]
        return [
            Obligation.model_validate(item)
            async for item in self._container.query_items(query=query, parameters=params)
        ]

    async def list_due_before(self, cutoff: str) -> list[Obligation]:
        """The scanner's query. Cross-partition by design — see the partition
        key tradeoff in ARCHITECTURE.md section 4."""
        query = (
            "SELECT * FROM c WHERE IS_DEFINED(c.due_date) AND c.due_date != null "
            "AND c.due_date < @cutoff"
        )
        params = [{"name": "@cutoff", "value": cutoff}]
        return [
            Obligation.model_validate(item)
            async for item in self._container.query_items(query=query, parameters=params)
        ]


class CosmosAuditLog:
    def __init__(self, connection: CosmosConnection):
        self._container = connection.container(AUDIT)

    async def record(
        self, *, contract_id: str, action: str, actor: str, detail: dict | None = None
    ) -> None:
        import uuid
        from datetime import UTC, datetime

        await self._container.create_item(
            {
                "id": str(uuid.uuid4()),
                "contract_id": contract_id,
                "action": action,
                "actor": actor,
                "detail": detail,
                "ts": datetime.now(UTC).isoformat(),
            }
        )
