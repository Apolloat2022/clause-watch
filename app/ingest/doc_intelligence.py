"""Azure Document Intelligence: PDF -> DocumentLayout.

The SDK import is deliberately deferred into the adapter's constructor. The
pipeline, the chunker, and the whole test suite import this module, and none of
them should require `azure-ai-documentintelligence` to be installed — only the
code path that actually calls Azure does.

Coordinates: Document Intelligence returns polygons in inches. They are
converted to normalized (0-1) page coordinates here, at the boundary, so no
downstream code has to know the page dimensions or the source unit.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.ingest.layout import BlockKind, BlockRole, BoundingBox, DocumentLayout, LayoutBlock

# Document Intelligence's paragraph `role` strings -> our enum. Anything not
# listed (including a null role, which is the common case for body text) falls
# through to BODY.
_ROLE_MAP = {
    "title": BlockRole.TITLE,
    "sectionHeading": BlockRole.SECTION_HEADING,
    "pageHeader": BlockRole.PAGE_HEADER,
    "pageFooter": BlockRole.PAGE_FOOTER,
    "pageNumber": BlockRole.PAGE_NUMBER,
    "footnote": BlockRole.FOOTNOTE,
}


@runtime_checkable
class LayoutAnalyzer(Protocol):
    async def analyze(self, pdf_bytes: bytes) -> DocumentLayout: ...


def polygon_to_box(polygon, page: int, page_width: float, page_height: float) -> BoundingBox | None:
    """Flat [x1, y1, x2, y2, ...] point list -> a normalized bounding box.

    Returns None rather than raising on degenerate input: a missing box costs a
    citation its highlight, which is a cosmetic loss, while raising would fail
    the whole document over one malformed region.
    """
    if not polygon or len(polygon) < 4 or not page_width or not page_height:
        return None
    xs = [float(v) for v in polygon[0::2]]
    ys = [float(v) for v in polygon[1::2]]
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    return BoundingBox(
        page=page,
        x=left / page_width,
        y=top / page_height,
        width=(right - left) / page_width,
        height=(bottom - top) / page_height,
    )


def _table_to_text(table) -> str:
    """Flatten a table to pipe-delimited rows.

    Rows and columns are preserved as structure — the point of layout analysis
    is that a payment schedule keeps its shape. Prose flattening would destroy
    exactly the relationship that makes the numbers mean anything.
    """
    rows: dict[int, dict[int, str]] = {}
    for cell in getattr(table, "cells", []) or []:
        rows.setdefault(cell.row_index, {})[cell.column_index] = (cell.content or "").strip()
    lines = []
    for row_index in sorted(rows):
        cells = rows[row_index]
        lines.append(" | ".join(cells.get(c, "") for c in sorted(cells)))
    return "\n".join(lines)


class AzureDocumentIntelligenceAnalyzer:
    """Real adapter. Authenticates with Managed Identity, never a key."""

    def __init__(self, endpoint: str, model_id: str = "prebuilt-layout"):
        # Imported here, not at module scope: see the module docstring.
        from azure.ai.documentintelligence.aio import DocumentIntelligenceClient
        from azure.identity.aio import DefaultAzureCredential

        self._model_id = model_id
        self._credential = DefaultAzureCredential()
        self._client = DocumentIntelligenceClient(endpoint=endpoint, credential=self._credential)

    async def analyze(self, pdf_bytes: bytes) -> DocumentLayout:
        poller = await self._client.begin_analyze_document(self._model_id, body=pdf_bytes)
        result = await poller.result()
        return self._to_layout(result)

    @staticmethod
    def _to_layout(result) -> DocumentLayout:
        pages = {p.page_number: p for p in (result.pages or [])}

        def box_for(regions, default_page: int = 1) -> tuple[int, BoundingBox | None]:
            if not regions:
                return default_page, None
            region = regions[0]
            page_no = region.page_number
            page = pages.get(page_no)
            if page is None:
                return page_no, None
            return page_no, polygon_to_box(region.polygon, page_no, page.width, page.height)

        blocks: list[LayoutBlock] = []
        for paragraph in result.paragraphs or []:
            page_no, box = box_for(paragraph.bounding_regions)
            blocks.append(
                LayoutBlock(
                    kind=BlockKind.PARAGRAPH,
                    role=_ROLE_MAP.get(paragraph.role or "", BlockRole.BODY),
                    text=paragraph.content or "",
                    page=page_no,
                    bounding_box=box,
                )
            )

        for table in result.tables or []:
            page_no, box = box_for(table.bounding_regions)
            blocks.append(
                LayoutBlock(
                    kind=BlockKind.TABLE,
                    role=BlockRole.BODY,
                    text=_table_to_text(table),
                    page=page_no,
                    bounding_box=box,
                )
            )

        # Paragraphs and tables come back as separate collections; interleave
        # them by page so the chunker sees something close to reading order. A
        # table is attached to the clause it follows, so getting this wrong
        # would attach schedules to the wrong section.
        blocks.sort(key=lambda b: (b.page, b.bounding_box.y if b.bounding_box else 0.0))

        return DocumentLayout(page_count=len(pages) or 1, blocks=blocks)

    async def aclose(self) -> None:
        await self._client.close()
        await self._credential.close()


class StaticLayoutAnalyzer:
    """Returns a layout it was handed. Dev and test only."""

    def __init__(self, layout: DocumentLayout):
        self._layout = layout

    async def analyze(self, pdf_bytes: bytes) -> DocumentLayout:
        return self._layout
