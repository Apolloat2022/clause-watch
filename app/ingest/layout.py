"""Normalized document layout — the boundary between Document Intelligence and
everything downstream.

The chunker, the pipeline, and the tests all speak these types, never the
Azure SDK's response objects. Two reasons that matters: the SDK shape changes
between API versions and would otherwise leak into the chunking logic, and
chunking is the part worth testing hardest — it must be exercisable from a
literal fixture with no client, no credential, and no network.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, Field


class BlockKind(str, enum.Enum):
    PARAGRAPH = "PARAGRAPH"
    TABLE = "TABLE"


class BlockRole(str, enum.Enum):
    """Mirrors Document Intelligence's paragraph roles.

    This is most of what justifies using Document Intelligence over a text
    extractor: PAGE_HEADER / PAGE_FOOTER identify the running boilerplate that
    repeats on every page (and would otherwise be chunked as contract text),
    and SECTION_HEADING marks clause boundaries that numbering alone misses.
    """

    TITLE = "TITLE"
    SECTION_HEADING = "SECTION_HEADING"
    PAGE_HEADER = "PAGE_HEADER"
    PAGE_FOOTER = "PAGE_FOOTER"
    PAGE_NUMBER = "PAGE_NUMBER"
    FOOTNOTE = "FOOTNOTE"
    BODY = "BODY"


class BoundingBox(BaseModel):
    """Normalized (0-1) page coordinates.

    Kept normalized rather than in inches or pixels so a citation can be
    rendered over a page image at any zoom without carrying the source DPI
    around with it.
    """

    page: int
    x: float
    y: float
    width: float
    height: float

    def union(self, other: BoundingBox) -> BoundingBox:
        """Smallest box covering both. Raises if the boxes are on different
        pages — silently merging across a page break would produce a citation
        that highlights nothing."""
        if self.page != other.page:
            raise ValueError(f"cannot union boxes on pages {self.page} and {other.page}")
        left = min(self.x, other.x)
        top = min(self.y, other.y)
        right = max(self.x + self.width, other.x + other.width)
        bottom = max(self.y + self.height, other.y + other.height)
        return BoundingBox(
            page=self.page, x=left, y=top, width=right - left, height=bottom - top
        )


class LayoutBlock(BaseModel):
    """One paragraph or one table, in reading order."""

    kind: BlockKind = BlockKind.PARAGRAPH
    role: BlockRole = BlockRole.BODY
    text: str
    page: int
    bounding_box: BoundingBox | None = None


class DocumentLayout(BaseModel):
    """A whole analyzed document. `blocks` is in reading order across pages."""

    page_count: int
    blocks: list[LayoutBlock] = Field(default_factory=list)

    @property
    def body_blocks(self) -> list[LayoutBlock]:
        """Blocks that are actually contract text.

        Running headers, footers, page numbers, and footnotes are dropped: they
        repeat on every page and, left in, produce dozens of near-identical
        clauses that pollute both retrieval and extraction.
        """
        noise = {
            BlockRole.PAGE_HEADER,
            BlockRole.PAGE_FOOTER,
            BlockRole.PAGE_NUMBER,
            BlockRole.FOOTNOTE,
        }
        return [b for b in self.blocks if b.role not in noise]
