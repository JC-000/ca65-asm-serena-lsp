"""Tests for the post-process pass that synthesizes body ranges and nests
cheap-locals / anonymous-labels under label-style routines.

This is the fix for M4 bug #1: c64-https and other ca65 codebases use
``label:`` + ``rts`` for routines rather than ``.proc ... .endproc``, and
without this pass our LSP returns single-line ranges for every "routine".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ca65_ls.buffer.document import Document
from ca65_ls.types import SymbolKind

FIXTURE = Path(__file__).parent / "fixtures" / "test_repo" / "src" / "label_style.s"


@pytest.fixture(scope="module")
def doc() -> Document:
    return Document(FIXTURE.as_uri(), FIXTURE.read_text(encoding="utf-8"))


def _by_name(symbols, name, kind=SymbolKind.LABEL):
    """Find a symbol by name (filtered to a specific kind, default LABEL).

    The fixture has both `.export extract_byte` (kind=EXPORT) and the
    `extract_byte:` label (kind=LABEL); these tests want the label.
    """
    return next(s for s in symbols if s.name == name and s.kind == kind)


def test_label_body_extends_to_next_sibling_label(doc):
    """`extract_byte`'s range should cover all its lines, ending before
    `extract_word:` begins."""
    extract_byte = _by_name(doc.symbols, "extract_byte")
    extract_word = _by_name(doc.symbols, "extract_word")

    # extract_byte starts at its declaration; ends BEFORE extract_word starts.
    assert extract_byte.range.end.line < extract_word.range.start.line, (
        f"extract_byte body should not overlap extract_word; got "
        f"{extract_byte.range.end.line=} >= {extract_word.range.start.line=}"
    )
    # It must span more than the declaration line itself.
    assert extract_byte.range.end.line > extract_byte.range.start.line, (
        "extract_byte should have a multi-line body now, not a single line"
    )


def test_cheap_locals_become_children_of_preceding_label(doc):
    """`@retry` and `@done` inside extract_word should be its children with
    parent_label set."""
    extract_word = _by_name(doc.symbols, "extract_word")
    child_names = [c.name for c in extract_word.children]
    assert "@retry" in child_names, f"expected @retry as child; got {child_names}"
    assert "@done" in child_names, f"expected @done as child; got {child_names}"

    for c in extract_word.children:
        if c.kind == SymbolKind.CHEAP_LOCAL:
            assert c.parent_label == "extract_word", (
                f"cheap local {c.name!r} parent should be 'extract_word', got {c.parent_label!r}"
            )


def test_cheap_locals_are_no_longer_top_level(doc):
    """`@retry` and `@done` should NOT appear at the top level — they live
    only as children of extract_word now."""
    top_names = {s.name for s in doc.symbols}
    assert "@retry" not in top_names, "cheap local leaked to top-level"
    assert "@done" not in top_names, "cheap local leaked to top-level"


def test_anonymous_labels_attach_to_preceding_label(doc):
    """The three `:` anonymous labels in extract_buffer should be children
    of extract_buffer, not siblings at the top level."""
    extract_buffer = _by_name(doc.symbols, "extract_buffer")
    anon_children = [c for c in extract_buffer.children if c.kind == SymbolKind.ANON_LABEL]
    assert len(anon_children) >= 2, (
        f"expected at least 2 anonymous labels inside extract_buffer; got {len(anon_children)}"
    )

    # None should still be at top level.
    top_anons = [s for s in doc.symbols if s.kind == SymbolKind.ANON_LABEL]
    assert top_anons == [], f"anonymous labels leaked to top-level: {top_anons}"


def test_data_label_in_bss_does_not_eat_subsequent_segments(doc):
    """`extract_scratch:` in BSS should NOT absorb extract_byte (which is
    in the next CODE segment)."""
    extract_scratch = _by_name(doc.symbols, "extract_scratch")
    extract_byte = _by_name(doc.symbols, "extract_byte")

    # The data label's range should end at or before the CODE segment.
    assert extract_scratch.range.end.line < extract_byte.range.start.line, (
        f"extract_scratch body must not overlap extract_byte; got "
        f"{extract_scratch.range.end.line=}, {extract_byte.range.start.line=}"
    )

    # extract_byte must NOT be a child of extract_scratch.
    scratch_child_names = {c.name for c in extract_scratch.children}
    assert "extract_byte" not in scratch_child_names


def test_sibling_labels_stay_siblings(doc):
    """extract_byte / extract_word / extract_buffer are three sibling
    routines, not nested. extract_word must not be inside extract_byte."""
    top_names = [s.name for s in doc.symbols if s.kind == SymbolKind.LABEL]
    # All three top-level routines visible
    assert "extract_byte" in top_names
    assert "extract_word" in top_names
    assert "extract_buffer" in top_names

    extract_byte = _by_name(doc.symbols, "extract_byte")
    child_names = [c.name for c in extract_byte.children]
    assert "extract_word" not in child_names, (
        "extract_byte should not have absorbed sibling extract_word"
    )


def test_references_still_work_after_body_extension(doc):
    """The reference-walker should still find use sites despite the label's
    range now spanning the routine body (which contains the routine name's
    own selection_range, but no other reference token to itself)."""
    refs = doc.references_in("ptr1")
    # We expect ptr1 to appear at least once in extract_byte's body.
    assert refs, "expected references to ptr1 in label_style.s"
