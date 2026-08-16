"""Schema tests for the extraction contract.

The cheapest guard against the extraction prompt silently drifting away from
what the pipeline can actually store.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.models import ExtractedObligation, ObligationType


def _valid(**overrides) -> dict:
    payload = {
        "description": "Supplier shall deliver the quarterly compliance report.",
        "obligor_party": "Supplier",
        "obligation_type": ObligationType.REPORTING,
        "cited_clause_ids": ["c-12"],
        "confidence": 0.8,
    }
    payload.update(overrides)
    return payload


def test_obligation_requires_at_least_one_citation():
    # The whole grounding guarantee rests on this constraint.
    with pytest.raises(ValidationError):
        ExtractedObligation(**_valid(cited_clause_ids=[]))


def test_confidence_is_bounded():
    with pytest.raises(ValidationError):
        ExtractedObligation(**_valid(confidence=1.4))


def test_relative_timing_leaves_due_date_null():
    # "within 30 days of invoice" is a rule, not a date - the model is told not
    # to compute one, and the schema must allow that.
    obligation = ExtractedObligation(
        **_valid(due_date=None, recurrence="within 30 days of each invoice")
    )
    assert obligation.due_date is None
    assert obligation.recurrence
