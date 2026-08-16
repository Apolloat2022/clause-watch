"""Core domain types.

`ExtractedObligation` is the schema handed to the model as a structured-output
constraint, so its field docstrings are prompt surface, not just documentation —
they are what the model reads to decide what goes in each field. Edit them with
that in mind.

The split between `ExtractedObligation` (what the model returns) and
`Obligation` (what gets stored) is deliberate: the model never supplies ids,
timestamps, or state. Those are assigned after citation validation passes, so
an ungrounded extraction cannot become a stored record.
"""

from __future__ import annotations

import enum
from datetime import date, datetime

from pydantic import BaseModel, Field


class ContractStatus(str, enum.Enum):
    PENDING = "PENDING"
    EXTRACTING = "EXTRACTING"
    READY = "READY"
    FAILED = "FAILED"


class ObligationType(str, enum.Enum):
    PAYMENT = "PAYMENT"
    DELIVERY = "DELIVERY"
    REPORTING = "REPORTING"
    NOTICE = "NOTICE"
    RENEWAL = "RENEWAL"
    TERMINATION_RIGHT = "TERMINATION_RIGHT"
    SERVICE_LEVEL = "SERVICE_LEVEL"
    OTHER = "OTHER"


class ObligationState(str, enum.Enum):
    """Assigned by the scanner job, never by the model."""

    OPEN = "OPEN"
    DUE_SOON = "DUE_SOON"
    OVERDUE = "OVERDUE"
    SATISFIED = "SATISFIED"
    WAIVED = "WAIVED"
    SUPERSEDED = "SUPERSEDED"


class BoundingBox(BaseModel):
    """Normalized (0-1) page coordinates from Document Intelligence — what makes
    an obligation clickable back to the region of the page it came from."""

    page: int
    x: float
    y: float
    width: float
    height: float


class Clause(BaseModel):
    id: str
    contract_id: str
    ordinal: int
    heading: str | None = None
    text: str
    page: int
    bounding_box: BoundingBox | None = None
    # Populated post-chunking; excluded from API responses (see api/ serializers)
    # because a 1536-float array in every clause payload is pure noise on the wire.
    embedding: list[float] | None = None


class ExtractedObligation(BaseModel):
    """What the model is asked to return, once per obligation it finds.

    Every field description below is read by the model. Keep them concrete and
    say what to do when the contract is silent — an unstated rule gets guessed.
    """

    description: str = Field(
        description=(
            "What must be done, in one sentence, in the contract's own terms. "
            "Do not paraphrase amounts, deadlines, or party names."
        )
    )
    obligor_party: str = Field(
        description=(
            "The party who owes this obligation, exactly as named in the "
            "contract. If the clause is mutual, name the party it binds in "
            "this instance."
        )
    )
    obligation_type: ObligationType
    due_date: date | None = Field(
        default=None,
        description=(
            "Absolute calendar date only. If the contract expresses timing as a "
            "rule relative to an event ('within 30 days of invoice'), leave this "
            "null and put the rule in `recurrence` — do not compute a date."
        ),
    )
    recurrence: str | None = Field(
        default=None,
        description=(
            "The timing rule verbatim when it is relative or repeating "
            "('quarterly', 'within 30 days of each invoice'). Null for one-off "
            "obligations with a fixed date."
        ),
    )
    amount: float | None = Field(
        default=None,
        description="Numeric amount if the obligation carries one. Null otherwise.",
    )
    currency: str | None = Field(
        default=None, description="ISO 4217 code, e.g. USD. Null if no amount."
    )
    cited_clause_ids: list[str] = Field(
        min_length=1,
        description=(
            "Ids of the clauses this obligation was read from — at least one. "
            "Only ids present in the supplied clause list are valid. If an "
            "obligation cannot be supported by a supplied clause, omit it "
            "entirely rather than citing a loosely related clause."
        ),
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "How certain the extraction is, 0-1. Low confidence is expected for "
            "obligations assembled across several clauses."
        ),
    )


class Obligation(BaseModel):
    """The stored record: an ExtractedObligation that passed citation validation,
    plus the fields the system owns."""

    id: str
    contract_id: str
    description: str
    obligor_party: str
    obligation_type: ObligationType
    due_date: date | None = None
    recurrence: str | None = None
    amount: float | None = None
    currency: str | None = None
    cited_clause_ids: list[str]
    confidence: float
    state: ObligationState = ObligationState.OPEN
    # Computed by the scanner, not the model: for a fixed-date obligation it
    # mirrors due_date, and for a calendar-periodic one it is the next
    # projected occurrence. None means nothing schedulable to judge against.
    next_due_date: date | None = None
    # Which model and prompt produced this, so a bad batch can be found and
    # re-run rather than quietly persisting forever.
    model_version: str
    prompt_version: str
    created_at: datetime
    updated_at: datetime


class Contract(BaseModel):
    id: str
    contract_id: str
    title: str
    counterparty: str | None = None
    blob_uri: str
    # SHA-256 of the uploaded bytes: re-ingesting identical content is a no-op.
    content_hash: str
    status: ContractStatus = ContractStatus.PENDING
    failure_reason: str | None = None
    effective_date: date | None = None
    term_end_date: date | None = None
    created_at: datetime
    updated_at: datetime


class CitationError(ValueError):
    """Raised when an extracted obligation cites a clause that wasn't supplied.

    Treated as a rejection, not a warning: an obligation that cannot be traced
    to source text is worse than a missing one, because someone may act on it.
    """


class ExtractionRefusedError(RuntimeError):
    """The model declined the request, or returned nothing usable.

    Distinct from CitationError: that one rejects a single bad obligation while
    the rest of the extraction stands. This one means the request produced no
    result at all, so the contract cannot be marked READY.
    """
