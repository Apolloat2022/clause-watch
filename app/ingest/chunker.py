"""Layout -> clauses.

The chunk boundary is the contract's own numbering, not a fixed character
window. A clause is the unit a lawyer cites ("Section 4.2"), so it is the unit
stored, embedded, and cited back — a citation that lands mid-sentence because
a 512-character window happened to end there is not a citation anyone can use.

Two decisions worth knowing before reading the code:

**Lettered sub-items are not boundaries.** `(a)`, `(b)`, `(i)` stay inside
their parent clause. People cite "Section 4.2(a)", but the enclosing unit that
carries the meaning is 4.2 — splitting on every sub-item shreds a payment
clause into fragments that are individually meaningless, and the retriever
then has to reassemble what the chunker took apart.

**Tables are never split.** A payment schedule split across two chunks loses
the row/column relationship that makes its numbers mean anything, which is the
entire reason for using a layout-aware extractor in the first place.
"""

from __future__ import annotations

import re

from app.ingest.layout import BlockKind, BlockRole, BoundingBox, DocumentLayout, LayoutBlock

# Long clauses are windowed rather than stored whole, so a single 8,000-char
# clause doesn't dominate an embedding. Overlap keeps a sentence that straddles
# the split retrievable from either side.
MAX_CLAUSE_CHARS = 2400
OVERLAP_CHARS = 300
# Below this, a "clause" is almost always a stray fragment — a signature line, a
# stranded page artifact — not something worth embedding and citing.
MIN_CLAUSE_CHARS = 40

# "1.", "1.1", "4.2.1 " — segments capped at 3 digits so a bare year ("1998.")
# at the start of a sentence isn't mistaken for a clause number.
_NUMBERED = re.compile(r"^(?P<marker>\d{1,3}(?:\.\d{1,3}){0,3})\.?\s+(?=\S)")

# "ARTICLE IV", "SECTION 5", "SCHEDULE B", "EXHIBIT A", "ANNEX 2"
_NAMED = re.compile(
    r"^(?P<marker>(?:ARTICLE|SECTION|SCHEDULE|EXHIBIT|ANNEX|APPENDIX)\s+"
    r"(?:[IVXLCDM]{1,7}|\d{1,3}|[A-Z]))\b",
    re.IGNORECASE,
)

_HEADING_ROLES = {BlockRole.TITLE, BlockRole.SECTION_HEADING}


class ClauseChunk:
    """A chunk before it becomes a persisted Clause.

    Deliberately not the domain `Clause`: that one carries a contract id and an
    embedding, neither of which exist yet at chunking time.
    """

    __slots__ = ("bounding_box", "heading", "ordinal", "page", "text")

    def __init__(
        self,
        *,
        ordinal: int,
        heading: str | None,
        text: str,
        page: int,
        bounding_box: BoundingBox | None,
    ):
        self.ordinal = ordinal
        self.heading = heading
        self.text = text
        self.page = page
        self.bounding_box = bounding_box

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ClauseChunk(ordinal={self.ordinal}, heading={self.heading!r}, page={self.page})"


def clause_marker(block: LayoutBlock) -> str | None:
    """The clause marker this block opens with, or None if it opens no clause.

    Tables never open a clause — they belong to whatever clause introduced
    them, and a table whose first cell happens to read "1." is not a heading.
    """
    if block.kind is BlockKind.TABLE:
        return None

    text = block.text.strip()
    if not text:
        return None

    named = _NAMED.match(text)
    if named:
        return named.group("marker").upper()

    numbered = _NUMBERED.match(text)
    if numbered:
        return numbered.group("marker")

    # A layout-detected heading is a boundary even when it carries no number —
    # "Confidentiality" as a standalone section title is real and common.
    if block.role in _HEADING_ROLES:
        return text[:60]

    return None


def _line_starts_clause(line: str) -> bool:
    """Whether a line *inside* a block opens a new clause.

    Deliberately stricter than block-level detection. Splitting within a block
    is a recovery path for analyzers that hand back coarse paragraphs, and a
    false boundary there cuts a clause mid-sentence — worse than missing one.

    So numbered markers count (they are unambiguous at a line start), but a
    named marker only counts when the line reads like a heading. That keeps
    "SCHEDULE B - FEES" as a boundary while leaving prose like "Schedule A.
    'Effective Date' means 1 March 2026." alone.
    """
    stripped = line.strip()
    if not stripped:
        return False
    if _NUMBERED.match(stripped):
        return True
    return bool(_NAMED.match(stripped)) and stripped == stripped.upper()


def _explode_block(block: LayoutBlock) -> list[LayoutBlock]:
    """Split one block at line-initial clause markers.

    Document Intelligence usually returns one paragraph per clause, in which
    case this is a no-op. It earns its keep when the analyzer returns coarse
    blocks — a whole page as one paragraph, which is what any text-only
    extractor does — where every clause boundary would otherwise be invisible
    and the entire document would collapse into a single clause.

    Split segments inherit the parent's bounding box. That box is a superset of
    each segment's true region, which is honest for highlighting ("somewhere in
    here") without claiming a precision the analyzer never provided.
    """
    if block.kind is BlockKind.TABLE:
        return [block]

    lines = block.text.splitlines()
    if len(lines) < 2:
        return [block]

    segments: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if current and _line_starts_clause(line):
            segments.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        segments.append(current)

    if len(segments) < 2:
        return [block]

    return [
        LayoutBlock(
            kind=block.kind,
            # Only the first segment can still be a heading; the rest are body
            # text that happened to share a paragraph with it.
            role=block.role if index == 0 else BlockRole.BODY,
            text="\n".join(segment).strip(),
            page=block.page,
            bounding_box=block.bounding_box,
        )
        for index, segment in enumerate(segments)
        if "\n".join(segment).strip()
    ]


def _heading_for(block: LayoutBlock, marker: str) -> str:
    """First line of the opening block, which is the human-readable heading."""
    first_line = block.text.strip().splitlines()[0].strip()
    return first_line[:120] if first_line else marker


def _merge_boxes(blocks: list[LayoutBlock]) -> BoundingBox | None:
    """Union the boxes of the blocks sharing the first block's page.

    Scoped to one page on purpose: a clause spanning a page break gets a
    citation highlighting its opening page, which is where a reader would go
    looking. A box spanning two pages would highlight nothing at all.
    """
    boxed = [b.bounding_box for b in blocks if b.bounding_box is not None]
    if not boxed:
        return None
    first_page = boxed[0].page
    merged = None
    for box in boxed:
        if box.page != first_page:
            continue
        merged = box if merged is None else merged.union(box)
    return merged


def _split_long(text: str) -> list[str]:
    """Window an over-long clause with overlap, preferring sentence breaks.

    The window is advanced to a sentence boundary where one exists near the
    cut, so a chunk rarely starts mid-sentence.
    """
    if len(text) <= MAX_CLAUSE_CHARS:
        return [text]

    windows: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + MAX_CLAUSE_CHARS, len(text))
        if end < len(text):
            # Look for a sentence end in the last quarter of the window.
            window_tail_start = end - (MAX_CLAUSE_CHARS // 4)
            sentence_end = text.rfind(". ", window_tail_start, end)
            if sentence_end > start:
                end = sentence_end + 1
        windows.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - OVERLAP_CHARS, start + 1)
    return [w for w in windows if w]


def to_clauses(layout: DocumentLayout) -> list[ClauseChunk]:
    """Split an analyzed document into clause chunks, in document order.

    Text before the first recognized marker (cover page, preamble, recitals) is
    kept as clause 0 rather than discarded — recitals routinely carry the
    definitions and effective dates that later obligations depend on.
    """
    groups: list[tuple[str | None, list[LayoutBlock]]] = []
    current_marker: str | None = None
    current: list[LayoutBlock] = []

    # Exploded first, so a coarse analyzer that returns a whole page as one
    # paragraph still yields per-clause boundaries.
    exploded = [part for block in layout.body_blocks for part in _explode_block(block)]

    for block in exploded:
        if not block.text.strip():
            continue
        marker = clause_marker(block)
        if marker is not None and current:
            groups.append((current_marker, current))
            current_marker, current = marker, [block]
        elif marker is not None:
            current_marker, current = marker, [block]
        else:
            current.append(block)

    if current:
        groups.append((current_marker, current))

    chunks: list[ClauseChunk] = []
    ordinal = 0
    for marker, blocks in groups:
        text = "\n\n".join(b.text.strip() for b in blocks if b.text.strip())
        if len(text) < MIN_CLAUSE_CHARS:
            continue
        heading = _heading_for(blocks[0], marker) if marker else None
        box = _merge_boxes(blocks)
        page = blocks[0].page

        for window_index, window in enumerate(_split_long(text)):
            chunks.append(
                ClauseChunk(
                    ordinal=ordinal,
                    heading=heading,
                    text=window,
                    page=page,
                    # Only the first window of a split clause carries the box.
                    # Later windows are a chunking artifact with no distinct
                    # region on the page; giving them all the same box would
                    # imply a precision that isn't there.
                    bounding_box=box if window_index == 0 else None,
                )
            )
            ordinal += 1

    return chunks
