"""Pipeline and API tests — the whole phase-2 path, offline.

Upload → blob → queue → worker → clauses, plus the failure and idempotency
behavior that is easy to get wrong and expensive to discover in production.
"""

from __future__ import annotations

import pytest

from app.domain.models import ContractStatus
from app.ingest.doc_intelligence import StaticLayoutAnalyzer
from app.ingest.layout import DocumentLayout
from app.ingest.pipeline import content_hash, register_upload, run_ingest
from app.jobs.ingest_worker import drain

PDF = b"%PDF-1.7 fake bytes for testing"


async def _upload(deps, data: bytes = PDF, filename: str = "msa.pdf"):
    return await register_upload(
        filename=filename,
        data=data,
        title="Meridian MSA",
        contracts=deps.contracts,
        blobs=deps.blobs,
        audit=deps.audit,
    )


# --------------------------------------------------------------- pipeline


async def test_upload_stores_blob_and_records_pending(deps):
    contract, is_new = await _upload(deps)

    assert is_new is True
    assert contract.status is ContractStatus.PENDING
    assert contract.content_hash == content_hash(PDF)
    assert await deps.blobs.get(contract.blob_uri) == PDF


async def test_identical_bytes_do_not_start_a_second_extraction(deps):
    first, first_is_new = await _upload(deps)
    # Same bytes, different filename — still the same contract. Hashing bytes
    # rather than names is what makes a renamed re-upload a no-op.
    second, second_is_new = await _upload(deps, filename="msa-copy.pdf")

    assert first_is_new is True
    assert second_is_new is False
    assert second.contract_id == first.contract_id
    assert len(await deps.contracts.list()) == 1


async def test_run_ingest_persists_clauses_and_marks_ready(deps):
    contract, _ = await _upload(deps)

    result = await run_ingest(
        contract.contract_id,
        contracts=deps.contracts,
        clauses=deps.clauses,
        blobs=deps.blobs,
        analyzer=deps.analyzer,
        audit=deps.audit,
    )

    assert result.status is ContractStatus.READY
    assert result.failure_reason is None

    clauses = await deps.clauses.list_for_contract(contract.contract_id)
    assert len(clauses) > 1
    assert all(c.contract_id == contract.contract_id for c in clauses)
    # Clause ids must be stable and derived, since obligations will cite them.
    assert clauses[0].id == f"{contract.contract_id}:0"


async def test_reingest_replaces_clauses_rather_than_appending(deps):
    contract, _ = await _upload(deps)
    kwargs = {
        "contracts": deps.contracts,
        "clauses": deps.clauses,
        "blobs": deps.blobs,
        "analyzer": deps.analyzer,
        "audit": deps.audit,
    }

    await run_ingest(contract.contract_id, **kwargs)
    first_count = len(await deps.clauses.list_for_contract(contract.contract_id))
    await run_ingest(contract.contract_id, **kwargs)
    second_count = len(await deps.clauses.list_for_contract(contract.contract_id))

    # Appending would leave obligations citing clause ids that no longer
    # correspond to anything after a chunker change.
    assert first_count == second_count


async def test_document_with_no_text_layer_fails_loudly(deps):
    contract, _ = await _upload(deps)

    with pytest.raises(ValueError, match="no clauses extracted"):
        await run_ingest(
            contract.contract_id,
            contracts=deps.contracts,
            clauses=deps.clauses,
            blobs=deps.blobs,
            # A scanned PDF analyzes to nothing. Marking it READY with zero
            # clauses would hide a document that needs a human.
            analyzer=StaticLayoutAnalyzer(DocumentLayout(page_count=1, blocks=[])),
            audit=deps.audit,
        )

    stored = await deps.contracts.get(contract.contract_id)
    assert stored.status is ContractStatus.FAILED
    assert "no text layer" in stored.failure_reason


async def test_failure_leaves_a_reason_not_a_stuck_extracting_row(deps):
    contract, _ = await _upload(deps)

    class Exploding:
        async def analyze(self, pdf_bytes):
            raise RuntimeError("Document Intelligence returned 503")

    with pytest.raises(RuntimeError):
        await run_ingest(
            contract.contract_id,
            contracts=deps.contracts,
            clauses=deps.clauses,
            blobs=deps.blobs,
            analyzer=Exploding(),
            audit=deps.audit,
        )

    stored = await deps.contracts.get(contract.contract_id)
    # A row stuck in EXTRACTING is invisible; a FAILED one is actionable.
    assert stored.status is ContractStatus.FAILED
    assert "503" in stored.failure_reason


async def test_every_stage_is_audited(deps):
    contract, _ = await _upload(deps)
    await run_ingest(
        contract.contract_id,
        contracts=deps.contracts,
        clauses=deps.clauses,
        blobs=deps.blobs,
        analyzer=deps.analyzer,
        audit=deps.audit,
    )

    actions = [e["action"] for e in deps.audit.entries]
    # No OBLIGATIONS_EXTRACTED: this container has no model, so ingest stops at
    # clauses rather than failing.
    assert actions == ["UPLOADED", "EXTRACTING", "CLAUSES_EXTRACTED", "READY"]


async def test_extraction_stage_is_audited_with_rejection_counts(deps):
    from app.ingest.extractor import ExtractionResponse, StaticObligationModel
    from tests.test_extractor import obligation

    contract, _ = await _upload(deps)
    await run_ingest(
        contract.contract_id,
        contracts=deps.contracts,
        clauses=deps.clauses,
        blobs=deps.blobs,
        analyzer=deps.analyzer,
        audit=deps.audit,
        obligations=deps.obligations,
        model=StaticObligationModel(
            ExtractionResponse(
                obligations=[
                    obligation(cited_clause_ids=[f"{contract.contract_id}:0"]),
                    obligation(description="Invented", cited_clause_ids=["nope:0"]),
                ]
            )
        ),
        model_version="claude-opus-5",
        prompt_version="v1",
    )

    actions = [e["action"] for e in deps.audit.entries]
    assert actions == [
        "UPLOADED",
        "EXTRACTING",
        "CLAUSES_EXTRACTED",
        "OBLIGATIONS_EXTRACTED",
        "READY",
    ]

    # The rejection rate is stored, not just logged: a spike after a prompt
    # change is the earliest signal extraction regressed, and it is only
    # visible if it was written down.
    detail = next(e for e in deps.audit.entries if e["action"] == "OBLIGATIONS_EXTRACTED")["detail"]
    assert detail["obligation_count"] == 1
    assert detail["rejected_count"] == 1
    assert detail["rejection_rate"] == 0.5

    stored = await deps.obligations.list_for_contract(contract.contract_id)
    assert len(stored) == 1


async def test_extraction_failure_fails_the_whole_ingest(deps):
    # A contract with clauses but no obligations looks complete and is not.
    contract, _ = await _upload(deps)

    class Refusing:
        async def extract(self, *, system, prompt):
            from app.domain.models import ExtractionRefusedError

            raise ExtractionRefusedError("model declined the request (category=cyber)")

    with pytest.raises(Exception, match="declined"):
        await run_ingest(
            contract.contract_id,
            contracts=deps.contracts,
            clauses=deps.clauses,
            blobs=deps.blobs,
            analyzer=deps.analyzer,
            audit=deps.audit,
            obligations=deps.obligations,
            model=Refusing(),
        )

    stored = await deps.contracts.get(contract.contract_id)
    assert stored.status is ContractStatus.FAILED
    assert "declined" in stored.failure_reason


async def test_blob_store_rejects_a_path_traversal_name(deps):
    # The blob name is derived from a client-supplied filename.
    with pytest.raises(ValueError, match="escapes the store root"):
        await deps.blobs.put("../../escaped.pdf", PDF)


# ------------------------------------------------------------------ worker


async def test_worker_drains_the_queue(deps):
    contract, _ = await _upload(deps)
    await deps.queue.enqueue(contract.contract_id)

    processed = await drain(deps)

    assert processed == 1
    assert len(deps.queue) == 0
    assert (await deps.contracts.get(contract.contract_id)).status is ContractStatus.READY


async def test_worker_stops_cleanly_on_an_empty_queue(deps):
    assert await drain(deps) == 0


async def test_worker_completes_the_lease_on_success(deps):
    contract, _ = await _upload(deps)
    await deps.queue.enqueue(contract.contract_id)

    await drain(deps)

    # Completed, not abandoned — a redelivered message would re-extract an
    # already-READY contract on every job execution.
    assert len(deps.queue) == 0


async def test_worker_abandons_the_lease_on_failure(deps):
    contract, _ = await _upload(deps)
    await deps.queue.enqueue(contract.contract_id)

    class Exploding:
        async def analyze(self, pdf_bytes):
            raise RuntimeError("Document Intelligence returned 503")

    deps.analyzer = Exploding()

    with pytest.raises(RuntimeError):
        await drain(deps)

    # Back on the queue for redelivery. Against Service Bus this is what
    # advances the delivery count toward the dead-letter threshold; dropping
    # the lease instead would strand the contract until the lock expired.
    assert len(deps.queue) == 1
    assert (await deps.contracts.get(contract.contract_id)).status is ContractStatus.FAILED


async def test_abandoned_message_is_redelivered_and_can_succeed(deps):
    contract, _ = await _upload(deps)
    await deps.queue.enqueue(contract.contract_id)

    calls = {"n": 0}
    working = deps.analyzer

    class FlakyOnce:
        async def analyze(self, pdf_bytes):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient 503")
            return await working.analyze(pdf_bytes)

    deps.analyzer = FlakyOnce()

    with pytest.raises(RuntimeError):
        await drain(deps)
    # Second run picks the redelivered message back up and succeeds — the
    # transient-failure path that abandon() exists to support.
    assert await drain(deps) == 1
    assert (await deps.contracts.get(contract.contract_id)).status is ContractStatus.READY


# --------------------------------------------------------------------- api


async def test_upload_endpoint_returns_202_and_queues(api_client):
    response = await api_client.post(
        "/api/v1/contracts",
        files={"file": ("msa.pdf", PDF, "application/pdf")},
        data={"title": "Meridian MSA"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "PENDING"
    # 202 means work was accepted, so something must actually be queued.
    assert len(api_client.deps.queue) == 1


async def test_duplicate_upload_returns_200_not_202(api_client):
    files = {"file": ("msa.pdf", PDF, "application/pdf")}
    assert (await api_client.post("/api/v1/contracts", files=files)).status_code == 202
    second = await api_client.post("/api/v1/contracts", files=files)

    # 200, not 409: a harmless retry should not look like an error, and there
    # is no new work to poll for.
    assert second.status_code == 200
    assert len(api_client.deps.queue) == 1


async def test_empty_upload_is_rejected(api_client):
    response = await api_client.post(
        "/api/v1/contracts", files={"file": ("empty.pdf", b"", "application/pdf")}
    )
    assert response.status_code == 400


async def test_non_pdf_upload_is_rejected(api_client):
    response = await api_client.post(
        "/api/v1/contracts", files={"file": ("notes.txt", b"hello", "text/plain")}
    )
    assert response.status_code == 415


async def test_clauses_endpoint_returns_them_after_ingest(api_client):
    upload = await api_client.post(
        "/api/v1/contracts", files={"file": ("msa.pdf", PDF, "application/pdf")}
    )
    contract_id = upload.json()["contract_id"]
    await drain(api_client.deps)

    response = await api_client.get(f"/api/v1/contracts/{contract_id}/clauses")

    assert response.status_code == 200
    clauses = response.json()
    assert len(clauses) > 1
    # Embeddings are stripped on the wire — a 1536-float array per clause
    # dwarfs the text it belongs to.
    assert all(c["embedding"] is None for c in clauses)


async def test_unknown_contract_is_404(api_client):
    assert (await api_client.get("/api/v1/contracts/does-not-exist")).status_code == 404
