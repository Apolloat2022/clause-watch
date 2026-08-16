"""Retrieval tests: embeddings, clause search, and the evidence trail.

`HashingEmbedder` has real lexical similarity, so these assert on ranking
behavior — a query about payment ranks the payment clause first — rather than
just checking that a list came back. What they cannot assert is anything
semantic: the local embedder does not know "remuneration" relates to "payment",
and Cosmos's `VectorDistance()` has no emulator. Both need a live subscription.
"""

from __future__ import annotations

import pytest

from app.domain.models import Clause
from app.ingest.embeddings import HashingEmbedder, cosine


def clause(idx: int, text: str, heading: str | None = None) -> Clause:
    return Clause(
        id=f"c:{idx}",
        contract_id="c",
        ordinal=idx,
        heading=heading,
        text=text,
        page=1,
    )


# ------------------------------------------------------------- embedder


async def test_embeddings_are_deterministic_across_instances():
    # Load-bearing: the worker embeds clauses and the API embeds queries, in
    # different processes. Python's builtin str hash is randomized per process,
    # so a builtin-hash implementation would make those two incomparable —
    # search would silently return noise.
    a = await HashingEmbedder(64).embed_batch(["payment terms net thirty"])
    b = await HashingEmbedder(64).embed_batch(["payment terms net thirty"])
    assert a == b


async def test_embeddings_are_unit_length():
    [vector] = await HashingEmbedder(128).embed_batch(["some clause text here"])
    assert pytest.approx(sum(v * v for v in vector), abs=1e-9) == 1.0


async def test_embedding_dimensions_are_respected():
    # Must match the Cosmos container's vector policy exactly or the index
    # rejects the write.
    [vector] = await HashingEmbedder(384).embed_batch(["text"])
    assert len(vector) == 384


async def test_empty_text_yields_a_zero_vector_not_a_crash():
    [vector] = await HashingEmbedder(32).embed_batch(["...  ,,,  "])
    assert all(v == 0.0 for v in vector)


async def test_similar_text_scores_higher_than_unrelated_text():
    embedder = HashingEmbedder(2048)
    query, payment, confidentiality = await embedder.embed_batch(
        [
            "invoice payment within thirty days",
            "Customer shall pay each invoice within thirty (30) days of receipt.",
            "Each party shall keep the other's Confidential Information in confidence.",
        ]
    )
    assert cosine(query, payment) > cosine(query, confidentiality)


async def test_morphological_variants_match_via_subword_features():
    # Whole-token hashing alone treats these as unrelated, which made an
    # obviously-relevant clause score exactly zero and let ranking fall back to
    # document order. Character n-grams are what fix it.
    embedder = HashingEmbedder(2048)
    query, related, unrelated = await embedder.embed_batch(
        [
            "when must invoices be paid",
            "Customer shall pay each invoice within thirty (30) days.",
            "This Agreement is entered into as of 1 March 2026.",
        ]
    )
    assert cosine(query, related) > 0.0
    assert cosine(query, related) > cosine(query, unrelated)


def test_cosine_handles_degenerate_input():
    assert cosine([], []) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0  # mismatched lengths


# --------------------------------------------------------------- search


@pytest.fixture
async def searchable(deps):
    """A contract whose clauses are stored with embeddings."""
    embedder = deps.embedder
    clauses = [
        clause(0, "This Agreement is entered into as of 1 March 2026.", "(preamble)"),
        clause(1, "Customer shall pay each invoice within thirty (30) days.", "2. PAYMENT"),
        clause(2, "Supplier shall keep Confidential Information in confidence.", "5. CONFIDENTIALITY"),
    ]
    vectors = await embedder.embed_batch([c.text for c in clauses])
    stored = [c.model_copy(update={"embedding": v}) for c, v in zip(clauses, vectors, strict=True)]
    await deps.clauses.replace_for_contract("c", stored)
    return deps


async def test_search_ranks_the_relevant_clause_first(searchable):
    embedder = searchable.embedder
    [query] = await embedder.embed_batch(["when must invoices be paid"])

    hits = await searchable.clauses.search("c", query, k=3)

    assert hits[0][0].id == "c:1"
    assert hits[0][1] > hits[-1][1]


async def test_search_respects_k(searchable):
    [query] = await searchable.embedder.embed_batch(["payment"])
    assert len(await searchable.clauses.search("c", query, k=2)) == 2


async def test_search_skips_clauses_without_embeddings(deps):
    # An un-embedded clause is missing data, not a poor match — scoring it as
    # zero would let it outrank a genuinely dissimilar but embedded clause.
    await deps.clauses.replace_for_contract("c", [clause(0, "no vector here")])
    [query] = await deps.embedder.embed_batch(["anything"])

    assert await deps.clauses.search("c", query, k=5) == []


async def test_search_on_an_unknown_contract_is_empty(deps):
    [query] = await deps.embedder.embed_batch(["payment"])
    assert await deps.clauses.search("does-not-exist", query, k=5) == []


# ------------------------------------------------------------------ api


async def test_search_endpoint_returns_ranked_hits_without_embeddings(api_client):
    from app.jobs.ingest_worker import drain

    upload = await api_client.post(
        "/api/v1/contracts", files={"file": ("msa.pdf", b"%PDF-1.7 x", "application/pdf")}
    )
    contract_id = upload.json()["contract_id"]
    await drain(api_client.deps)

    response = await api_client.get(
        f"/api/v1/contracts/{contract_id}/search", params={"q": "payment invoice", "k": 3}
    )

    assert response.status_code == 200
    hits = response.json()
    assert 1 <= len(hits) <= 3
    # The vector is an implementation detail and dwarfs the text it belongs to.
    assert all(h["clause"]["embedding"] is None for h in hits)
    assert hits == sorted(hits, key=lambda h: h["score"], reverse=True)


async def test_search_on_unknown_contract_is_404(api_client):
    response = await api_client.get("/api/v1/contracts/nope/search", params={"q": "payment"})
    assert response.status_code == 404


async def test_search_rejects_an_empty_query(api_client):
    upload = await api_client.post(
        "/api/v1/contracts", files={"file": ("msa.pdf", b"%PDF-1.7 y", "application/pdf")}
    )
    contract_id = upload.json()["contract_id"]
    response = await api_client.get(f"/api/v1/contracts/{contract_id}/search", params={"q": ""})
    assert response.status_code == 422


async def test_search_is_unavailable_without_an_embedder(api_client):
    api_client.deps.embedder = None
    upload = await api_client.post(
        "/api/v1/contracts", files={"file": ("msa.pdf", b"%PDF-1.7 z", "application/pdf")}
    )
    contract_id = upload.json()["contract_id"]

    response = await api_client.get(f"/api/v1/contracts/{contract_id}/search", params={"q": "x"})

    # 503, not 500: search being off is a configuration state, not a bug.
    assert response.status_code == 503


# ------------------------------------------------------------- evidence


async def test_evidence_resolves_cited_clauses_to_text_and_page(api_client):
    from app.ingest.extractor import ExtractionResponse, StaticObligationModel
    from app.jobs.ingest_worker import drain
    from tests.test_extractor import obligation

    upload = await api_client.post(
        "/api/v1/contracts", files={"file": ("msa.pdf", b"%PDF-1.7 e", "application/pdf")}
    )
    contract_id = upload.json()["contract_id"]
    api_client.deps.model = StaticObligationModel(
        ExtractionResponse(obligations=[obligation(cited_clause_ids=[f"{contract_id}:1"])])
    )
    await drain(api_client.deps)

    obligations = (await api_client.get(f"/api/v1/contracts/{contract_id}/obligations")).json()
    response = await api_client.get(f"/api/v1/obligations/{obligations[0]['id']}/evidence")

    assert response.status_code == 200
    body = response.json()
    assert body["has_missing_citations"] is False
    assert len(body["cited_clauses"]) == 1
    cited = body["cited_clauses"][0]
    assert cited["id"] == f"{contract_id}:1"
    assert cited["text"]
    assert cited["page"] >= 1


async def test_evidence_flags_a_dangling_citation(api_client):
    from app.ingest.extractor import ExtractionResponse, StaticObligationModel
    from app.jobs.ingest_worker import drain
    from tests.test_extractor import obligation

    upload = await api_client.post(
        "/api/v1/contracts", files={"file": ("msa.pdf", b"%PDF-1.7 d", "application/pdf")}
    )
    contract_id = upload.json()["contract_id"]
    api_client.deps.model = StaticObligationModel(
        ExtractionResponse(obligations=[obligation(cited_clause_ids=[f"{contract_id}:1"])])
    )
    await drain(api_client.deps)
    obligations = (await api_client.get(f"/api/v1/contracts/{contract_id}/obligations")).json()

    # Replace the clause set without re-extracting — exactly the state that
    # leaves a validated citation dangling.
    await api_client.deps.clauses.replace_for_contract(contract_id, [])

    response = await api_client.get(f"/api/v1/obligations/{obligations[0]['id']}/evidence")

    # Still 200: the obligation exists and is worth showing. But the caller is
    # told the citation no longer resolves rather than shown a short list.
    assert response.status_code == 200
    assert response.json()["has_missing_citations"] is True


async def test_evidence_for_unknown_obligation_is_404(api_client):
    assert (await api_client.get("/api/v1/obligations/nope:ob:0/evidence")).status_code == 404


# ------------------------------------------------------------- register


async def test_register_requires_a_filter(api_client):
    # An unfiltered cross-partition scan of every obligation ever stored is not
    # something to expose by accident.
    assert (await api_client.get("/api/v1/obligations")).status_code == 400


async def test_register_filters_by_contract_and_type(api_client):
    from app.ingest.extractor import ExtractionResponse, StaticObligationModel
    from app.jobs.ingest_worker import drain
    from tests.test_extractor import obligation

    upload = await api_client.post(
        "/api/v1/contracts", files={"file": ("msa.pdf", b"%PDF-1.7 r", "application/pdf")}
    )
    contract_id = upload.json()["contract_id"]
    api_client.deps.model = StaticObligationModel(
        ExtractionResponse(
            obligations=[
                obligation(cited_clause_ids=[f"{contract_id}:1"]),
                obligation(
                    description="Supplier shall deliver the quarterly report.",
                    obligor_party="Supplier",
                    obligation_type="REPORTING",
                    cited_clause_ids=[f"{contract_id}:1"],
                ),
            ]
        )
    )
    await drain(api_client.deps)

    everything = await api_client.get("/api/v1/obligations", params={"contract_id": contract_id})
    assert len(everything.json()) == 2

    payments = await api_client.get(
        "/api/v1/obligations",
        params={"contract_id": contract_id, "obligation_type": "PAYMENT"},
    )
    assert len(payments.json()) == 1
    assert payments.json()[0]["obligation_type"] == "PAYMENT"
