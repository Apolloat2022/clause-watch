"""Extraction tests.

Citation validation is the thing this file exists to pin down. It is the only
mechanism standing between a plausible-sounding model output and a stored
obligation someone may act on, and it is pure — no network, no Foundry
resource, fully exercisable offline.
"""

from __future__ import annotations

import pytest

from app.domain.models import (
    CitationError,
    Clause,
    ExtractedObligation,
    ObligationType,
)
from app.ingest.extractor import (
    ExtractionResponse,
    StaticObligationModel,
    _batch,
    _dedupe_key,
    build_prompt,
    extract_obligations,
    validate_citations,
)


def clause(idx: int, text: str = "The Supplier shall do the thing.") -> Clause:
    return Clause(
        id=f"c:{idx}",
        contract_id="c",
        ordinal=idx,
        heading=f"{idx}. HEADING",
        text=text,
        page=1,
    )


def obligation(**overrides) -> ExtractedObligation:
    payload = {
        "description": "Customer shall pay each invoice within thirty (30) days.",
        "obligor_party": "Customer",
        "obligation_type": ObligationType.PAYMENT,
        "cited_clause_ids": ["c:0"],
        "confidence": 0.9,
    }
    payload.update(overrides)
    return ExtractedObligation(**payload)


# ------------------------------------------------------------ citations


def test_citation_to_a_supplied_clause_passes():
    validate_citations(obligation(cited_clause_ids=["c:0", "c:1"]), {"c:0", "c:1", "c:2"})


def test_citation_to_an_unsupplied_clause_is_rejected():
    with pytest.raises(CitationError, match="not supplied"):
        validate_citations(obligation(cited_clause_ids=["c:99"]), {"c:0", "c:1"})


def test_partially_grounded_obligation_is_rejected_whole():
    # One real citation does not launder an invented one — the obligation may
    # have been assembled from a clause that does not exist.
    with pytest.raises(CitationError, match="c:99"):
        validate_citations(obligation(cited_clause_ids=["c:0", "c:99"]), {"c:0"})


async def test_ungrounded_obligations_are_discarded_not_stored():
    clauses = [clause(0), clause(1)]
    model = StaticObligationModel(
        ExtractionResponse(
            obligations=[
                obligation(cited_clause_ids=["c:0"]),
                obligation(description="Invented duty", cited_clause_ids=["c:404"]),
            ]
        )
    )

    result = await extract_obligations(
        "c", clauses, model=model, model_version="m", prompt_version="v1"
    )

    assert len(result.obligations) == 1
    assert result.obligations[0].description.startswith("Customer shall pay")
    assert len(result.rejected) == 1
    assert "c:404" in result.rejected[0][1]
    assert result.rejection_rate == 0.5


async def test_stored_obligations_record_model_and_prompt_version():
    # Without these a bad batch cannot be found and re-run after a prompt fix.
    model = StaticObligationModel(ExtractionResponse(obligations=[obligation()]))
    result = await extract_obligations(
        "c", [clause(0)], model=model, model_version="claude-opus-5", prompt_version="v7"
    )

    stored = result.obligations[0]
    assert stored.model_version == "claude-opus-5"
    assert stored.prompt_version == "v7"
    assert stored.id == "c:ob:0"


async def test_obligation_ids_are_deterministic_across_runs():
    # Re-extraction must overwrite in place rather than accumulate duplicates.
    model = StaticObligationModel(
        ExtractionResponse(obligations=[obligation(), obligation(description="Second duty")])
    )
    first = await extract_obligations(
        "c", [clause(0)], model=model, model_version="m", prompt_version="v1"
    )
    second = await extract_obligations(
        "c", [clause(0)], model=model, model_version="m", prompt_version="v1"
    )

    assert [o.id for o in first.obligations] == [o.id for o in second.obligations]


async def test_empty_extraction_is_not_an_error():
    # A contract genuinely imposing no extractable obligations is unusual but
    # legitimate — an NDA amendment, say. It must not fail the ingest.
    model = StaticObligationModel(ExtractionResponse(obligations=[]))
    result = await extract_obligations(
        "c", [clause(0)], model=model, model_version="m", prompt_version="v1"
    )

    assert result.obligations == []
    assert result.rejection_rate == 0.0


# --------------------------------------------------------------- prompt


def test_prompt_places_the_id_immediately_before_its_text():
    # A citation format the model has to reconstruct from context is one it
    # will get wrong.
    prompt = build_prompt([clause(0, "Payment is due in 30 days.")])
    assert "[c:0]" in prompt
    assert prompt.index("[c:0]") < prompt.index("Payment is due in 30 days.")


def test_prompt_includes_headings_and_pages():
    prompt = build_prompt([clause(3)])
    assert "3. HEADING" in prompt
    assert "page 1" in prompt


# -------------------------------------------------------------- batching


def test_small_contract_is_a_single_batch():
    assert len(_batch([clause(i) for i in range(20)])) == 1


def test_large_contract_is_split_with_overlap():
    big = [clause(i, "x" * 5_000) for i in range(30)]
    batches = _batch(big, max_chars=20_000)

    assert len(batches) > 1
    # Consecutive batches share clauses, so an obligation spanning the seam has
    # a chance of being seen whole at least once.
    first_ids = {c.id for c in batches[0]}
    second_ids = {c.id for c in batches[1]}
    assert first_ids & second_ids


async def test_duplicates_from_overlapping_batches_are_collapsed():
    # Sized to exceed MAX_BATCH_CHARS (60k) so extract_obligations really does
    # issue multiple requests — with smaller clauses this passes vacuously.
    clauses = [clause(i, "x" * 15_000) for i in range(8)]
    # Same obligation returned for every batch, as overlap would cause.
    model = StaticObligationModel(ExtractionResponse(obligations=[obligation()]))

    result = await extract_obligations(
        "c", clauses, model=model, model_version="m", prompt_version="v1"
    )

    assert len(model.calls) > 1, "test needs multiple batches to be meaningful"
    assert len(result.obligations) == 1


def test_dedupe_ignores_whitespace_and_case():
    a = obligation(description="Customer  shall PAY  each invoice", obligor_party="Customer")
    b = obligation(description="customer shall pay each invoice", obligor_party="  customer ")
    assert _dedupe_key(a) == _dedupe_key(b)


def test_dedupe_distinguishes_different_obligors():
    # Mutual clauses bind both parties; those are two obligations, not one.
    a = obligation(obligor_party="Customer")
    b = obligation(obligor_party="Supplier")
    assert _dedupe_key(a) != _dedupe_key(b)


def test_empty_clause_list_makes_no_batches():
    assert _batch([]) == []
