"""Contract upload and status.

POST /api/v1/contracts        multipart PDF -> Blob + queue, 202 (or 200 on a
                              duplicate, see below)
GET  /api/v1/contracts        list, optional status filter
GET  /api/v1/contracts/{id}   detail incl. failure_reason when FAILED
GET  /api/v1/contracts/{id}/clauses

Upload returns **202**, not 201: extraction is asynchronous and the caller
polls status. 201 would imply the clauses exist, which they do not yet.

A re-upload of identical bytes returns **200** with the existing contract —
not 202 and not 409. There is no new work to wait on, and 409 would force
callers to treat a harmless retry as an error.
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile

from app.domain.models import Clause, Contract, Obligation
from app.ingest.pipeline import register_upload

router = APIRouter(prefix="/api/v1/contracts", tags=["contracts"])

# Document Intelligence's own per-file ceiling on the S0 tier is far higher,
# but an unbounded upload is a memory-exhaustion vector on a scale-to-zero
# container with modest limits.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


@router.post("", response_model=Contract)
async def upload_contract(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
) -> Contract:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit",
        )
    # Content-type is client-supplied and trivially spoofed, so this is a
    # usability check rather than a security control — the analyzer is the
    # thing that actually rejects a non-PDF.
    if file.content_type and file.content_type != "application/pdf":
        raise HTTPException(status_code=415, detail="only application/pdf is accepted")

    deps = request.app.state.deps
    contract, is_new = await register_upload(
        filename=file.filename or "contract.pdf",
        data=data,
        title=title or file.filename or "Untitled contract",
        contracts=deps.contracts,
        blobs=deps.blobs,
        audit=deps.audit,
    )

    if is_new:
        await deps.queue.enqueue(contract.contract_id)
        response.status_code = 202
    else:
        response.status_code = 200
    return contract


@router.get("", response_model=list[Contract])
async def list_contracts(request: Request, status: str | None = None) -> list[Contract]:
    return await request.app.state.deps.contracts.list(status=status)


@router.get("/{contract_id}", response_model=Contract)
async def get_contract(request: Request, contract_id: str) -> Contract:
    contract = await request.app.state.deps.contracts.get(contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail=f"contract {contract_id} not found")
    return contract


@router.get("/{contract_id}/obligations", response_model=list[Obligation])
async def list_obligations(request: Request, contract_id: str) -> list[Obligation]:
    """Readback of what extraction produced.

    Deliberately minimal — the filterable register and the /evidence endpoint
    that resolves cited clause ids to text and page regions are phase 4. This
    exists so extraction is observable without reading the database.
    """
    deps = request.app.state.deps
    if await deps.contracts.get(contract_id) is None:
        raise HTTPException(status_code=404, detail=f"contract {contract_id} not found")
    if deps.obligations is None:
        raise HTTPException(status_code=503, detail="obligation storage is not configured")
    return await deps.obligations.list_for_contract(contract_id)


@router.get("/{contract_id}/clauses", response_model=list[Clause])
async def list_clauses(request: Request, contract_id: str) -> list[Clause]:
    deps = request.app.state.deps
    if await deps.contracts.get(contract_id) is None:
        raise HTTPException(status_code=404, detail=f"contract {contract_id} not found")
    clauses = await deps.clauses.list_for_contract(contract_id)
    # Embeddings are excluded from the wire: a 1536-float array per clause is
    # pure noise in an API response and dwarfs the text it belongs to.
    return [c.model_copy(update={"embedding": None}) for c in clauses]
