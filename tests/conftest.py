"""Shared fixtures.

Everything here runs offline. Azure is faked at the port boundary
(`app/data/ports.py`) rather than at the SDK level, so the tests exercise real
application logic — the actual chunker, the actual pipeline, the actual route
handlers — instead of asserting that mocks were called.

The one thing this cannot cover is the Cosmos vector query, since
`VectorDistance()` has no local emulator. That needs an integration test
against live Azure when phase 4 lands.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.data.local import (
    InMemoryAuditLog,
    InMemoryClauseRepository,
    InMemoryContractRepository,
    InMemoryIngestQueue,
    InMemoryObligationRepository,
    LocalBlobStore,
)
from app.deps import Dependencies
from app.ingest.doc_intelligence import StaticLayoutAnalyzer
from app.ingest.embeddings import HashingEmbedder
from app.ingest.layout import BlockKind, BlockRole, BoundingBox, DocumentLayout, LayoutBlock


def block(
    text: str,
    *,
    page: int = 1,
    role: BlockRole = BlockRole.BODY,
    kind: BlockKind = BlockKind.PARAGRAPH,
    y: float = 0.1,
) -> LayoutBlock:
    return LayoutBlock(
        kind=kind,
        role=role,
        text=text,
        page=page,
        bounding_box=BoundingBox(page=page, x=0.1, y=y, width=0.8, height=0.05),
    )


@pytest.fixture
def sample_layout() -> DocumentLayout:
    """A small services agreement with the features that matter:
    running header/footer noise, a numbered hierarchy, an unnumbered section
    heading, and a payment-schedule table."""
    return DocumentLayout(
        page_count=2,
        blocks=[
            block("MERIDIAN SERVICES AGREEMENT", role=BlockRole.TITLE, y=0.05),
            block("Confidential - Page 1", role=BlockRole.PAGE_HEADER, y=0.01),
            block(
                "This Services Agreement is entered into as of 1 March 2026 between "
                "Meridian Retail Group ('Customer') and Northwind Systems Ltd "
                "('Supplier'). The parties agree as follows.",
                y=0.12,
            ),
            block(
                "1. DEFINITIONS. 'Services' means the managed hosting services "
                "described in Schedule A. 'Effective Date' means 1 March 2026.",
                y=0.2,
            ),
            block(
                "2. PAYMENT TERMS. Customer shall pay each invoice within thirty (30) "
                "days of receipt. Late amounts accrue interest at 1.5% per month.",
                y=0.3,
            ),
            block(
                "2.1 Supplier shall issue invoices quarterly in arrears, on the first "
                "business day following the end of each quarter.",
                y=0.4,
            ),
            block(
                "Milestone | Amount | Due\nKickoff | 25,000 USD | 2026-03-15\n"
                "Go-live | 75,000 USD | 2026-06-30",
                kind=BlockKind.TABLE,
                y=0.5,
            ),
            block("Confidential - Page 2", role=BlockRole.PAGE_FOOTER, page=2, y=0.97),
            block(
                "3. TERM AND RENEWAL. This Agreement runs for twenty-four (24) months "
                "from the Effective Date and renews automatically for successive "
                "twelve (12) month terms unless either party gives ninety (90) days "
                "written notice.",
                page=2,
                y=0.1,
            ),
            block("Confidentiality", role=BlockRole.SECTION_HEADING, page=2, y=0.25),
            block(
                "Each party shall keep the other's Confidential Information in "
                "confidence for five (5) years following termination.",
                page=2,
                y=0.3,
            ),
        ],
    )


@pytest_asyncio.fixture
async def deps(tmp_path, sample_layout) -> Dependencies:
    """A full local dependency container, blobs isolated per test."""
    return Dependencies(
        contracts=InMemoryContractRepository(),
        clauses=InMemoryClauseRepository(),
        blobs=LocalBlobStore(tmp_path / "blobs"),
        queue=InMemoryIngestQueue(),
        audit=InMemoryAuditLog(),
        analyzer=StaticLayoutAnalyzer(sample_layout),
        obligations=InMemoryObligationRepository(),
        # Tests that want extraction pass their own StaticObligationModel;
        # leaving it None here keeps the clause-only path the default.
        model=None,
        # Small dimension: these tests assert on ranking, and 1536 floats per
        # clause is a lot of arithmetic to do thousands of times for no gain.
        embedder=HashingEmbedder(512),
    )


@pytest_asyncio.fixture
async def api_client(deps):
    """The real app, real routes, local adapters."""
    import httpx

    from app.main import create_app

    app = create_app(dependencies=deps)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            client.deps = deps
            yield client
