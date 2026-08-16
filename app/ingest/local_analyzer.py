"""A text-only layout analyzer for local development.

Document Intelligence has no offline equivalent, which would otherwise make the
local loop test-only — you could upload a PDF but never see a clause. This
closes that gap using pypdf's text extraction so `uvicorn app.main:app` plus a
POST is a real end-to-end run on a laptop.

**What it loses, and why that matters.** pypdf returns a page's text as a flat
string. There are no bounding boxes, no paragraph roles, and no table
structure. So compared to a real Document Intelligence run:

* Citations have a page number but no highlightable region.
* Running headers and footers are *not* identified, so they end up in clause
  text as noise.
* Tables are flattened to whatever reading order the PDF's content stream
  happens to have — the row/column relationship is gone.

That last one is the whole reason the pipeline uses Document Intelligence, so
treat clause output from this analyzer as directionally right and structurally
wrong. It is a development convenience, never a fallback: `build_dependencies`
selects it only when ENVIRONMENT=local.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re

from app.ingest.layout import BlockKind, BlockRole, DocumentLayout, LayoutBlock

logger = logging.getLogger(__name__)

# Two-or-more newlines is the only paragraph signal available without layout
# analysis.
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+")


class LocalTextAnalyzer:
    """Extracts flat text per page with pypdf. Development only."""

    def __init__(self) -> None:
        # Imported here so the module is importable without the optional
        # [local] extra installed.
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise RuntimeError(
                "LocalTextAnalyzer needs pypdf. Install it with: pip install -e '.[local]'"
            ) from exc
        self._reader_cls = PdfReader

    async def analyze(self, pdf_bytes: bytes) -> DocumentLayout:
        # pypdf is synchronous and CPU-bound; keep it off the event loop so an
        # ingest run doesn't stall the API in a single-process local setup.
        return await asyncio.to_thread(self._analyze_sync, pdf_bytes)

    def _analyze_sync(self, pdf_bytes: bytes) -> DocumentLayout:
        reader = self._reader_cls(io.BytesIO(pdf_bytes))
        blocks: list[LayoutBlock] = []

        for page_index, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            for raw in _PARAGRAPH_SPLIT.split(text):
                paragraph = raw.strip()
                if not paragraph:
                    continue
                blocks.append(
                    LayoutBlock(
                        kind=BlockKind.PARAGRAPH,
                        # Everything is BODY: without layout analysis there is
                        # no way to tell a heading from a paragraph, so the
                        # chunker falls back to numbering alone.
                        role=BlockRole.BODY,
                        text=paragraph,
                        page=page_index,
                        bounding_box=None,
                    )
                )

        page_count = len(reader.pages)
        if not blocks:
            logger.warning(
                "no text extracted; the PDF is probably scanned images with no text layer"
            )
        return DocumentLayout(page_count=page_count, blocks=blocks)
