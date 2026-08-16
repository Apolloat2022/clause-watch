"""Chunker tests.

The chunker is the highest-value thing to test in phase 2: it is pure, it is
where the citation guarantee is either earned or lost, and every downstream
stage inherits its mistakes. A clause boundary in the wrong place produces a
citation that points at the wrong text, which is worse than no citation at all.
"""

from __future__ import annotations

from app.ingest.chunker import (
    MAX_CLAUSE_CHARS,
    MIN_CLAUSE_CHARS,
    clause_marker,
    to_clauses,
)
from app.ingest.layout import BlockKind, BlockRole, BoundingBox, DocumentLayout, LayoutBlock
from tests.conftest import block


def test_numbered_markers_are_detected():
    assert clause_marker(block("1. DEFINITIONS. The term means...")) == "1"
    assert clause_marker(block("2.1 Supplier shall issue invoices...")) == "2.1"
    assert clause_marker(block("12.3.4 Further provisions apply.")) == "12.3.4"


def test_named_markers_are_detected():
    assert clause_marker(block("ARTICLE IV - INDEMNITY")) == "ARTICLE IV"
    assert clause_marker(block("SECTION 5 Termination")) == "SECTION 5"
    assert clause_marker(block("Schedule B - Fees")) == "SCHEDULE B"


def test_a_leading_year_is_not_a_clause_marker():
    # "1998. The parties..." would be caught by a naive ^\d+\. pattern and
    # silently start a bogus clause mid-sentence.
    assert clause_marker(block("1998. That was the year the original deal closed.")) is None


def test_lettered_sub_items_do_not_start_a_clause():
    # People cite "4.2(a)", but 4.2 is the unit that carries the meaning.
    # Splitting here would shred a payment clause into meaningless fragments.
    assert clause_marker(block("(a) the first condition; and")) is None
    assert clause_marker(block("(iv) the fourth condition.")) is None


def test_unnumbered_section_heading_is_a_boundary():
    heading = block("Confidentiality", role=BlockRole.SECTION_HEADING)
    assert clause_marker(heading) == "Confidentiality"


def test_table_never_starts_a_clause():
    # A table whose first cell reads "1. | x" is not a heading.
    table = block("1. | Amount\n2. | Date", kind=BlockKind.TABLE)
    assert clause_marker(table) is None


def test_running_header_and_footer_are_dropped(sample_layout):
    texts = " ".join(c.text for c in to_clauses(sample_layout))
    assert "Confidential - Page 1" not in texts
    assert "Confidential - Page 2" not in texts


def test_preamble_before_first_marker_is_kept(sample_layout):
    # Recitals routinely carry definitions and effective dates that later
    # obligations depend on, so discarding them loses real information.
    first = to_clauses(sample_layout)[0]
    assert "entered into as of 1 March 2026" in first.text


def test_clause_boundaries_follow_contract_numbering(sample_layout):
    headings = [c.heading for c in to_clauses(sample_layout) if c.heading]
    assert any(h.startswith("1.") for h in headings)
    assert any(h.startswith("2.") for h in headings)
    assert any(h.startswith("2.1") for h in headings)
    assert any(h.startswith("3.") for h in headings)
    assert "Confidentiality" in headings


def test_table_attaches_to_its_introducing_clause(sample_layout):
    clauses = to_clauses(sample_layout)
    with_table = [c for c in clauses if "Go-live" in c.text]
    assert len(with_table) == 1, "the payment schedule must live in exactly one clause"
    # It follows 2.1 (invoicing), which is the clause that introduces it.
    assert with_table[0].heading.startswith("2.1")
    # And it survives intact rather than being split across chunks.
    assert "Kickoff" in with_table[0].text and "Go-live" in with_table[0].text


def test_clause_carries_page_and_bounding_box(sample_layout):
    renewal = next(c for c in to_clauses(sample_layout) if c.heading.startswith("3."))
    assert renewal.page == 2
    assert renewal.bounding_box is not None
    assert renewal.bounding_box.page == 2


def test_fragments_below_the_minimum_are_dropped():
    layout = DocumentLayout(page_count=1, blocks=[block("4. OK.")])
    assert len(block("4. OK.").text) < MIN_CLAUSE_CHARS
    assert to_clauses(layout) == []


def test_long_clause_is_windowed_with_only_the_first_window_boxed():
    body = "The Supplier shall perform the Services with reasonable skill and care. " * 60
    layout = DocumentLayout(page_count=1, blocks=[block(f"7. OBLIGATIONS. {body}")])

    chunks = to_clauses(layout)
    assert len(chunks) > 1
    assert all(len(c.text) <= MAX_CLAUSE_CHARS for c in chunks)
    # Every window keeps the clause's heading, so a citation to any of them
    # still names the right section.
    assert all(c.heading.startswith("7.") for c in chunks)
    # Only the first window gets the box: the others are a chunking artifact
    # with no distinct region on the page.
    assert chunks[0].bounding_box is not None
    assert all(c.bounding_box is None for c in chunks[1:])


def test_ordinals_are_contiguous(sample_layout):
    chunks = to_clauses(sample_layout)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_boxes_on_different_pages_cannot_be_unioned():
    # Merging across a page break would produce a citation highlighting nothing.
    import pytest

    a = BoundingBox(page=1, x=0.1, y=0.1, width=0.2, height=0.1)
    b = BoundingBox(page=2, x=0.1, y=0.1, width=0.2, height=0.1)
    with pytest.raises(ValueError, match="different pages|pages 1 and 2"):
        a.union(b)


def test_coarse_single_block_page_still_splits_into_clauses():
    # Regression: any text-only extractor returns a whole page as one
    # paragraph. Detecting markers only at block starts collapsed the entire
    # document into one clause — found by the end-to-end smoke run, not by the
    # fixture-driven tests above, because the fixture pre-separates paragraphs.
    page = (
        "MERIDIAN SERVICES AGREEMENT\n"
        "This Services Agreement is entered into as of 1 March 2026.\n"
        "1. DEFINITIONS. 'Services' means the managed hosting services described in\n"
        "Schedule A. 'Effective Date' means 1 March 2026.\n"
        "2. PAYMENT TERMS. Customer shall pay each invoice within thirty (30) days.\n"
        "2.1 Supplier shall issue invoices quarterly in arrears.\n"
        "3. TERM AND RENEWAL. This Agreement runs for twenty-four (24) months.\n"
    )
    chunks = to_clauses(DocumentLayout(page_count=1, blocks=[block(page)]))
    headings = [c.heading for c in chunks if c.heading]

    assert any(h.startswith("1.") for h in headings)
    assert any(h.startswith("2.") for h in headings)
    assert any(h.startswith("2.1") for h in headings)
    assert any(h.startswith("3.") for h in headings)


def test_prose_beginning_with_a_named_marker_is_not_a_boundary():
    # "Schedule A. 'Effective Date' means..." is a wrapped continuation line,
    # not a heading. Splitting there would cut clause 1 mid-definition.
    page = (
        "1. DEFINITIONS. 'Services' means the managed hosting services described in\n"
        "Schedule A. 'Effective Date' means 1 March 2026 and nothing else.\n"
        "2. PAYMENT TERMS. Customer shall pay each invoice within thirty (30) days.\n"
    )
    chunks = to_clauses(DocumentLayout(page_count=1, blocks=[block(page)]))

    assert len(chunks) == 2
    assert "Effective Date" in chunks[0].text


def test_uppercase_named_heading_inside_a_block_is_a_boundary():
    page = (
        "1. DEFINITIONS. Terms used here have the meanings given below in full.\n"
        "SCHEDULE B - FEES\n"
        "The fees payable under this Agreement are set out in the table below.\n"
    )
    chunks = to_clauses(DocumentLayout(page_count=1, blocks=[block(page)]))

    assert len(chunks) == 2
    assert chunks[1].heading.startswith("SCHEDULE B")


def test_split_segments_inherit_the_parent_bounding_box():
    # A superset region is honest — "somewhere in here" — where claiming a
    # tighter box the analyzer never provided would not be.
    page = (
        "1. FIRST CLAUSE. This clause is long enough to survive the minimum.\n"
        "2. SECOND CLAUSE. This clause is also long enough to survive it.\n"
    )
    source = block(page)
    chunks = to_clauses(DocumentLayout(page_count=1, blocks=[source]))

    assert len(chunks) == 2
    assert all(c.bounding_box == source.bounding_box for c in chunks)


def test_empty_layout_yields_no_clauses():
    assert to_clauses(DocumentLayout(page_count=0, blocks=[])) == []


def test_layout_block_defaults_to_body_role():
    assert LayoutBlock(text="x", page=1).role is BlockRole.BODY
