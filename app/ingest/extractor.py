"""Clauses -> grounded obligations, via Claude on Foundry.

The model call is behind the `ObligationModel` port, so the two parts worth
testing hardest — prompt assembly and citation validation — are pure and run
offline. Only `FoundryObligationModel` touches the network.

**The load-bearing step is what happens after the model returns.** Every
`cited_clause_ids` entry is checked against the clauses actually supplied in
that request, and an obligation citing anything else is discarded, not stored
with a low score. A missed obligation is a known unknown; an invented payment
term is a confident lie someone may act on. Rejections are counted and logged
so a prompt regression shows up as a rejection-rate spike rather than as
quietly worse output.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from app.domain.models import (
    CitationError,
    Clause,
    ExtractedObligation,
    ExtractionRefusedError,
    Obligation,
)

logger = logging.getLogger(__name__)

# Clauses per request, bounded by characters rather than count because clause
# size varies hugely. Set well above a typical contract so most documents go in
# one request — see the note on cross-batch obligations in `_batch`.
MAX_BATCH_CHARS = 60_000
# Clauses repeated at the head of the next batch, so an obligation spanning a
# batch seam has a chance of being seen whole at least once.
BATCH_OVERLAP_CLAUSES = 3

SYSTEM_PROMPT = """\
You extract contractual obligations from clauses of an executed contract.

An obligation is a specific duty one party owes: a payment, a delivery, a \
report, a notice, a renewal or termination right, a service level. Recitals, \
definitions, and governing-law boilerplate are not obligations.

Rules that matter more than completeness:

- Ground every obligation in the clauses given to you. Cite the id of each \
clause you read it from. If you cannot support an obligation from the supplied \
clauses, omit it — do not cite a loosely related clause to make it fit.
- Do not compute dates. If timing is expressed relative to an event ("within \
30 days of invoice"), leave the due date empty and record the rule verbatim.
- One obligation per distinct duty. A clause imposing three separate duties \
yields three obligations; a single duty restated in two clauses yields one, \
citing both.
- Quote amounts, deadlines, and party names as the contract states them.

Omitting a doubtful obligation is correct. Inventing one is not.\
"""


class ExtractionResponse(BaseModel):
    """Schema handed to the model. A root object is required — a bare array is
    not a valid JSON Schema root for structured outputs."""

    obligations: list[ExtractedObligation]


@runtime_checkable
class ObligationModel(Protocol):
    async def extract(self, *, system: str, prompt: str) -> ExtractionResponse: ...


@dataclass(slots=True)
class ExtractionResult:
    obligations: list[Obligation] = field(default_factory=list)
    # (what the model returned, why it was discarded) — kept rather than only
    # counted, so a rejection spike can be diagnosed without re-running.
    rejected: list[tuple[ExtractedObligation, str]] = field(default_factory=list)

    @property
    def rejection_rate(self) -> float:
        total = len(self.obligations) + len(self.rejected)
        return len(self.rejected) / total if total else 0.0


def build_prompt(clauses: list[Clause]) -> str:
    """Render clauses with their ids.

    The id is what the model must cite, so it is placed immediately before the
    text rather than in a trailing metadata block — a citation format the model
    has to reconstruct from context is one it will get wrong.
    """
    parts = ["Clauses from the contract:", ""]
    for clause in clauses:
        heading = f" — {clause.heading}" if clause.heading else ""
        parts.append(f"[{clause.id}]{heading} (page {clause.page})")
        parts.append(clause.text)
        parts.append("")
    parts.append(
        "Extract every obligation these clauses impose. Cite the clause id(s) "
        "each one comes from."
    )
    return "\n".join(parts)


def _batch(clauses: list[Clause], max_chars: int = MAX_BATCH_CHARS) -> list[list[Clause]]:
    """Split clauses into request-sized batches with overlap.

    **Known gap:** an obligation grounded in two clauses that land in different
    batches cannot be extracted — neither request sees both, and citation
    validation would reject it if one were guessed. The overlap narrows the
    seam but does not close it. Most contracts fit in a single batch, so this
    only bites on very long documents; closing it properly needs a second pass
    over cross-batch candidates, which is not built.
    """
    if not clauses:
        return []

    batches: list[list[Clause]] = []
    current: list[Clause] = []
    size = 0
    for clause in clauses:
        clause_size = len(clause.text) + len(clause.id) + 32
        if current and size + clause_size > max_chars:
            batches.append(current)
            current = current[-BATCH_OVERLAP_CLAUSES:]
            size = sum(len(c.text) for c in current)
        current.append(clause)
        size += clause_size
    if current:
        batches.append(current)
    return batches


def validate_citations(extracted: ExtractedObligation, valid_ids: set[str]) -> None:
    """Raise CitationError unless every cited clause was actually supplied."""
    unknown = [cid for cid in extracted.cited_clause_ids if cid not in valid_ids]
    if unknown:
        raise CitationError(
            f"cites clause(s) not supplied in this request: {', '.join(sorted(unknown))}"
        )


def _to_obligation(
    extracted: ExtractedObligation,
    *,
    contract_id: str,
    index: int,
    model_version: str,
    prompt_version: str,
) -> Obligation:
    now = datetime.now(UTC)
    return Obligation(
        # Deterministic, so re-extracting a contract overwrites in place rather
        # than accumulating duplicates on every run.
        id=f"{contract_id}:ob:{index}",
        contract_id=contract_id,
        description=extracted.description,
        obligor_party=extracted.obligor_party,
        obligation_type=extracted.obligation_type,
        due_date=extracted.due_date,
        recurrence=extracted.recurrence,
        amount=extracted.amount,
        currency=extracted.currency,
        cited_clause_ids=extracted.cited_clause_ids,
        confidence=extracted.confidence,
        model_version=model_version,
        prompt_version=prompt_version,
        created_at=now,
        updated_at=now,
    )


def _dedupe_key(extracted: ExtractedObligation) -> tuple:
    """Identity for cross-batch duplicates.

    Overlapping batches re-show the same clauses, so the same duty can be
    returned twice. Keyed on party plus a normalized description prefix rather
    than cited ids, because the two extractions may cite different subsets of
    the same clause group.
    """
    return (
        extracted.obligor_party.strip().lower(),
        " ".join(extracted.description.lower().split())[:80],
    )


async def extract_obligations(
    contract_id: str,
    clauses: list[Clause],
    *,
    model: ObligationModel,
    model_version: str,
    prompt_version: str,
) -> ExtractionResult:
    """Extract every obligation in `clauses`, discarding ungrounded ones."""
    result = ExtractionResult()
    seen: set[tuple] = set()
    index = 0

    for batch in _batch(clauses):
        valid_ids = {clause.id for clause in batch}
        response = await model.extract(system=SYSTEM_PROMPT, prompt=build_prompt(batch))

        for extracted in response.obligations:
            try:
                validate_citations(extracted, valid_ids)
            except CitationError as exc:
                logger.warning(
                    "rejected ungrounded obligation",
                    extra={"contract_id": contract_id, "reason": str(exc)},
                )
                result.rejected.append((extracted, str(exc)))
                continue

            key = _dedupe_key(extracted)
            if key in seen:
                continue
            seen.add(key)

            result.obligations.append(
                _to_obligation(
                    extracted,
                    contract_id=contract_id,
                    index=index,
                    model_version=model_version,
                    prompt_version=prompt_version,
                )
            )
            index += 1

    if result.rejected:
        logger.warning(
            "extraction rejected %d of %d obligations (%.0f%%)",
            len(result.rejected),
            len(result.rejected) + len(result.obligations),
            result.rejection_rate * 100,
            extra={"contract_id": contract_id},
        )
    return result


class FoundryObligationModel:
    """Claude on Microsoft Foundry.

    Authenticates with Managed Identity via `azure_ad_token_provider`, matching
    every other Azure client here — the Foundry SDK also accepts a raw API key,
    but using one would put the only long-lived secret in the system back into
    a service that does not need it.
    """

    def __init__(
        self,
        resource: str,
        model: str,
        *,
        max_tokens: int = 16_000,
        api_key: str | None = None,
    ):
        from anthropic import AsyncAnthropicFoundry

        self._model = model
        self._max_tokens = max_tokens

        if api_key:
            self._credential = None
            self._client = AsyncAnthropicFoundry(resource=resource, api_key=api_key)
        else:
            from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

            self._credential = DefaultAzureCredential()
            self._client = AsyncAnthropicFoundry(
                resource=resource,
                azure_ad_token_provider=get_bearer_token_provider(
                    self._credential, "https://cognitiveservices.azure.com/.default"
                ),
            )

    async def extract(self, *, system: str, prompt: str) -> ExtractionResponse:
        response = await self._client.messages.parse(
            model=self._model,
            # Generous, because on current models max_tokens caps thinking and
            # response text together — a tight budget truncates the JSON body
            # rather than the reasoning.
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_format=ExtractionResponse,
        )

        # Must be checked before reading content: a refusal returns HTTP 200
        # with empty or partial content, so indexing straight into the parsed
        # output would fail confusingly instead of saying what happened.
        if response.stop_reason == "refusal":
            category = getattr(getattr(response, "stop_details", None), "category", None)
            raise ExtractionRefusedError(f"model declined the request (category={category})")

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise ExtractionRefusedError(
                f"no parsed output (stop_reason={response.stop_reason}); "
                "most likely max_tokens truncated the response"
            )
        return parsed

    async def aclose(self) -> None:
        await self._client.close()
        if self._credential is not None:
            await self._credential.close()


class StaticObligationModel:
    """Returns canned responses. Tests and local development only.

    Accepts either one response reused for every batch, or a list consumed in
    order so multi-batch behavior can be exercised.
    """

    def __init__(self, responses: ExtractionResponse | list[ExtractionResponse]):
        self._responses = responses if isinstance(responses, list) else [responses]
        self.calls: list[str] = []

    async def extract(self, *, system: str, prompt: str) -> ExtractionResponse:
        self.calls.append(prompt)
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[index]
