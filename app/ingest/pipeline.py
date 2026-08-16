"""The phase-2 ingest pipeline: blob -> layout -> clauses -> Cosmos.

    PENDING -> EXTRACTING -> READY
                          -> FAILED (with a reason)

No LLM yet. Phase 3 inserts extraction between clauses and READY; nothing in
this module should need to change when it does, because the obligation write
is a separate step against a separate repository.

The pipeline takes its collaborators as arguments rather than reaching for
globals, which is what lets it run against either the local adapters or the
Azure ones — and lets the tests assert on real behavior instead of mocks.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime

from app.data.ports import (
    AuditLog,
    BlobStore,
    ClauseRepository,
    ContractRepository,
    ObligationRepository,
)
from app.domain.models import BoundingBox as DomainBox
from app.domain.models import Clause, Contract, ContractStatus
from app.ingest.chunker import to_clauses
from app.ingest.doc_intelligence import LayoutAnalyzer
from app.ingest.embeddings import Embedder
from app.ingest.extractor import ObligationModel, extract_obligations
from app.ingest.layout import BoundingBox as LayoutBox

logger = logging.getLogger(__name__)


def content_hash(data: bytes) -> str:
    """SHA-256 of the raw upload — the idempotency key.

    Hashing bytes rather than filename or title on purpose: the same contract
    uploaded twice under different names is the same contract, and a renamed
    re-upload must not trigger a second extraction.
    """
    return hashlib.sha256(data).hexdigest()


def _to_domain_box(box: LayoutBox | None) -> DomainBox | None:
    """The layout and domain boxes are structurally identical but deliberately
    separate types — the layout one is an ingest detail, the domain one is part
    of the stored record and the API contract."""
    if box is None:
        return None
    return DomainBox(page=box.page, x=box.x, y=box.y, width=box.width, height=box.height)


async def register_upload(
    *,
    filename: str,
    data: bytes,
    title: str,
    contracts: ContractRepository,
    blobs: BlobStore,
    audit: AuditLog,
) -> tuple[Contract, bool]:
    """Store the PDF and record a PENDING contract.

    Returns `(contract, is_new)`. When the same bytes have been seen before the
    existing contract is returned with `is_new=False` and nothing is written —
    the caller turns that into a 200 rather than a 202, because there is no new
    work to wait on.
    """
    digest = content_hash(data)
    existing = await contracts.find_by_hash(digest)
    if existing is not None:
        logger.info(
            "duplicate upload ignored",
            extra={"contract_id": existing.contract_id, "content_hash": digest},
        )
        return existing, False

    contract_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    # Namespaced by contract id so two uploads of the same filename cannot
    # collide in the write-once store.
    blob_uri = await blobs.put(f"{contract_id}/{filename}", data)

    contract = Contract(
        id=contract_id,
        contract_id=contract_id,
        title=title,
        blob_uri=blob_uri,
        content_hash=digest,
        status=ContractStatus.PENDING,
        created_at=now,
        updated_at=now,
    )
    await contracts.upsert(contract)
    await audit.record(
        contract_id=contract_id,
        action="UPLOADED",
        actor="api",
        detail={"filename": filename, "bytes": len(data)},
    )
    return contract, True


async def run_ingest(
    contract_id: str,
    *,
    contracts: ContractRepository,
    clauses: ClauseRepository,
    blobs: BlobStore,
    analyzer: LayoutAnalyzer,
    audit: AuditLog,
    obligations: ObligationRepository | None = None,
    model: ObligationModel | None = None,
    embedder: Embedder | None = None,
    model_version: str | None = None,
    prompt_version: str = "v1",
) -> Contract:
    """Analyze one contract, persist its clauses, and extract its obligations.

    Safe to re-run: clauses and obligations are both replaced wholesale, so
    reprocessing after a chunker or prompt change leaves no stale rows behind.
    Any failure moves the contract to FAILED with the reason attached rather
    than leaving it stuck in EXTRACTING — a stuck row is invisible, a FAILED
    one is not.

    `model` is optional so the clause-only path stays runnable without a
    Foundry resource (and so the phase-2 tests still describe real behavior).
    When it is None the contract reaches READY with clauses and no obligations;
    when it is supplied, an extraction failure fails the whole ingest rather
    than quietly producing a contract that looks complete but has none.
    """
    if model is not None and obligations is None:
        # Caught here rather than as an AttributeError deep in the try block,
        # where it would be reported as an ingest failure and mark the contract
        # FAILED for what is actually a wiring mistake.
        raise ValueError("run_ingest was given a model but no obligation repository")

    contract = await contracts.get(contract_id)
    if contract is None:
        raise KeyError(f"contract {contract_id} not found")

    contract = contract.model_copy(
        update={"status": ContractStatus.EXTRACTING, "updated_at": datetime.now(UTC)}
    )
    await contracts.upsert(contract)
    await audit.record(contract_id=contract_id, action="EXTRACTING", actor="ingest-worker")

    try:
        pdf_bytes = await blobs.get(contract.blob_uri)
        layout = await analyzer.analyze(pdf_bytes)
        chunks = to_clauses(layout)

        if not chunks:
            # A zero-clause document is a failure, not an empty success. It
            # means a scanned PDF with no text layer, or an analyzer that
            # returned nothing — either way it needs a human, and silently
            # marking it READY would hide that.
            raise ValueError("no clauses extracted; document may have no text layer")

        records = [
            Clause(
                id=f"{contract_id}:{chunk.ordinal}",
                contract_id=contract_id,
                ordinal=chunk.ordinal,
                heading=chunk.heading,
                text=chunk.text,
                page=chunk.page,
                bounding_box=_to_domain_box(chunk.bounding_box),
            )
            for chunk in chunks
        ]

        if embedder is not None:
            # Embed before the write, not after: a clause stored without its
            # vector is invisible to search, and nothing would ever come back
            # to fill it in.
            vectors = await embedder.embed_batch([record.text for record in records])
            if len(vectors) != len(records):
                raise ValueError(
                    f"embedder returned {len(vectors)} vectors for {len(records)} clauses"
                )
            records = [
                record.model_copy(update={"embedding": vector})
                for record, vector in zip(records, vectors, strict=True)
            ]

        await clauses.replace_for_contract(contract_id, records)
        await audit.record(
            contract_id=contract_id,
            action="CLAUSES_EXTRACTED",
            actor="ingest-worker",
            detail={"clause_count": len(records), "page_count": layout.page_count},
        )

        extraction_detail: dict = {}
        if model is not None:
            extraction = await extract_obligations(
                contract_id,
                records,
                model=model,
                model_version=model_version or "unknown",
                prompt_version=prompt_version,
            )
            await obligations.replace_for_contract(contract_id, extraction.obligations)
            extraction_detail = {
                "obligation_count": len(extraction.obligations),
                "rejected_count": len(extraction.rejected),
                "rejection_rate": round(extraction.rejection_rate, 3),
                "prompt_version": prompt_version,
            }
            await audit.record(
                contract_id=contract_id,
                action="OBLIGATIONS_EXTRACTED",
                actor="ingest-worker",
                # Rejections are recorded, not just logged: a rejection-rate
                # spike after a prompt change is the earliest signal that the
                # extraction regressed, and it is only visible if it is stored.
                detail=extraction_detail,
            )

        contract = contract.model_copy(
            update={
                "status": ContractStatus.READY,
                "failure_reason": None,
                "updated_at": datetime.now(UTC),
            }
        )
        await contracts.upsert(contract)
        await audit.record(
            contract_id=contract_id,
            action="READY",
            actor="ingest-worker",
            detail={"clause_count": len(records), **extraction_detail},
        )
        logger.info(
            "ingest complete",
            extra={"contract_id": contract_id, "clause_count": len(records)},
        )
        return contract

    except Exception as exc:
        logger.exception("ingest failed", extra={"contract_id": contract_id})
        contract = contract.model_copy(
            update={
                "status": ContractStatus.FAILED,
                "failure_reason": str(exc),
                "updated_at": datetime.now(UTC),
            }
        )
        await contracts.upsert(contract)
        await audit.record(
            contract_id=contract_id,
            action="FAILED",
            actor="ingest-worker",
            detail={"error": str(exc)},
        )
        raise
