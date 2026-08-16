"""The obligation register, its evidence trail, and clause search.

GET /api/v1/obligations                    filter by contract, state, type, due window
GET /api/v1/obligations/{id}/evidence      cited clause text + page + bounding box
GET /api/v1/contracts/{id}/search?q=...    vector search over that contract's clauses

`/evidence` is what makes the system trustworthy rather than merely useful. It
answers "where does this obligation actually come from" with source text and a
page region, not a model assertion — the read-side counterpart to the
write-side rule that an obligation citing an unsupplied clause is discarded.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.domain.models import Clause, Obligation, ObligationState

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["obligations"])

MAX_SEARCH_RESULTS = 25

_TERMINAL = frozenset(
    {ObligationState.SATISFIED, ObligationState.WAIVED, ObligationState.SUPERSEDED}
)


class ClauseHit(BaseModel):
    """A search result. Score semantics differ between the local cosine
    implementation and Cosmos's VectorDistance, so it is reported as an opaque
    ranking signal rather than something to threshold on."""

    clause: Clause
    score: float


class Evidence(BaseModel):
    obligation: Obligation
    cited_clauses: list[Clause]
    # True when a cited clause id no longer resolves — which should be
    # impossible, since citations are validated at write time. If it is ever
    # true, the clause set was replaced without re-extracting, and the
    # obligation is stale.
    has_missing_citations: bool


def _strip_embeddings(clauses: list[Clause]) -> list[Clause]:
    """A 1536-float array per clause dwarfs the text it belongs to and no
    consumer of these endpoints uses it."""
    return [c.model_copy(update={"embedding": None}) for c in clauses]


@router.get("/obligations", response_model=list[Obligation])
async def list_obligations(
    request: Request,
    contract_id: str | None = None,
    state: str | None = None,
    obligation_type: str | None = None,
    due_before: date | None = None,
    min_confidence: float | None = Query(default=None, ge=0.0, le=1.0),
) -> list[Obligation]:
    """The register.

    Without `contract_id` this is a cross-partition query — acceptable at
    portfolio scale and the same tradeoff the scanner job makes (see
    ARCHITECTURE.md section 4). Filtering is applied in-process because the two
    repositories express queries differently; pushing predicates down is worth
    doing when the corpus makes it matter, not before.
    """
    deps = request.app.state.deps
    if deps.obligations is None:
        raise HTTPException(status_code=503, detail="obligation storage is not configured")

    if contract_id is not None:
        obligations = await deps.obligations.list_for_contract(contract_id)
    elif due_before is not None:
        obligations = await deps.obligations.list_due_before(due_before.isoformat())
    else:
        raise HTTPException(
            status_code=400,
            detail="provide contract_id or due_before; an unfiltered register scan is not offered",
        )

    if state is not None:
        obligations = [o for o in obligations if o.state.value == state]
    if obligation_type is not None:
        obligations = [o for o in obligations if o.obligation_type.value == obligation_type]
    if due_before is not None:
        obligations = [
            o for o in obligations if o.due_date is not None and o.due_date < due_before
        ]
    if min_confidence is not None:
        obligations = [o for o in obligations if o.confidence >= min_confidence]

    return obligations


@router.get("/obligations/{obligation_id}/evidence", response_model=Evidence)
async def obligation_evidence(request: Request, obligation_id: str) -> Evidence:
    deps = request.app.state.deps
    if deps.obligations is None:
        raise HTTPException(status_code=503, detail="obligation storage is not configured")

    obligation = await deps.obligations.get(obligation_id)
    if obligation is None:
        raise HTTPException(status_code=404, detail=f"obligation {obligation_id} not found")

    clauses = await deps.clauses.list_for_contract(obligation.contract_id)
    by_id = {clause.id: clause for clause in clauses}
    cited = [by_id[cid] for cid in obligation.cited_clause_ids if cid in by_id]

    missing = len(cited) != len(obligation.cited_clause_ids)
    if missing:
        # Not a 404: the obligation exists and is worth showing. But it means
        # clauses were replaced without re-extracting, so the citation is
        # dangling and the caller needs to know rather than see a short list.
        logger.warning(
            "obligation cites clauses that no longer exist",
            extra={"obligation_id": obligation_id, "contract_id": obligation.contract_id},
        )

    return Evidence(
        obligation=obligation,
        cited_clauses=_strip_embeddings(cited),
        has_missing_citations=missing,
    )


class DecisionRequest(BaseModel):
    note: str | None = None


async def _record_decision(
    request: Request, obligation_id: str, state: ObligationState, note: str | None
) -> Obligation:
    """Move an obligation to a terminal state.

    Terminal states are the one thing the scanner will not touch, so this is
    the only path into them — and it is one-way. Re-satisfying a satisfied
    obligation is a no-op rather than an error, because a retried click should
    not read as a failure.
    """
    deps = request.app.state.deps
    if deps.obligations is None:
        raise HTTPException(status_code=503, detail="obligation storage is not configured")

    obligation = await deps.obligations.get(obligation_id)
    if obligation is None:
        raise HTTPException(status_code=404, detail=f"obligation {obligation_id} not found")

    if obligation.state is state:
        return obligation
    if obligation.state in _TERMINAL:
        raise HTTPException(
            status_code=409,
            detail=(
                f"obligation is already {obligation.state.value}; "
                "terminal states are not interchangeable"
            ),
        )

    updated = obligation.model_copy(
        update={"state": state, "updated_at": datetime.now(UTC)}
    )
    await deps.obligations.upsert(updated)
    await deps.audit.record(
        contract_id=obligation.contract_id,
        action=state.value,
        actor="api",
        detail={"obligation_id": obligation_id, "note": note},
    )
    return updated


@router.post("/obligations/{obligation_id}/satisfy", response_model=Obligation)
async def satisfy_obligation(
    request: Request, obligation_id: str, body: DecisionRequest = DecisionRequest()
) -> Obligation:
    return await _record_decision(request, obligation_id, ObligationState.SATISFIED, body.note)


@router.post("/obligations/{obligation_id}/waive", response_model=Obligation)
async def waive_obligation(
    request: Request, obligation_id: str, body: DecisionRequest = DecisionRequest()
) -> Obligation:
    return await _record_decision(request, obligation_id, ObligationState.WAIVED, body.note)


@router.get("/contracts/{contract_id}/search", response_model=list[ClauseHit])
async def search_clauses(
    request: Request,
    contract_id: str,
    q: str = Query(min_length=1),
    k: int = Query(default=5, ge=1, le=MAX_SEARCH_RESULTS),
) -> list[ClauseHit]:
    deps = request.app.state.deps
    if await deps.contracts.get(contract_id) is None:
        raise HTTPException(status_code=404, detail=f"contract {contract_id} not found")
    if deps.embedder is None:
        raise HTTPException(
            status_code=503,
            detail="search is unavailable: no embedder configured",
        )

    query_vector = (await deps.embedder.embed_batch([q]))[0]
    hits = await deps.clauses.search(contract_id, query_vector, k=k)
    return [
        ClauseHit(clause=clause.model_copy(update={"embedding": None}), score=score)
        for clause, score in hits
    ]
