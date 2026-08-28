"""Tests for the buffer-layer Document.

These exercise the synthetic CA65 corpus under
``tests/fixtures/test_repo/src``.  They validate the contract pinned in
``ca65_ls/types.py`` so the Indexer can rely on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ca65_ls.buffer import Document
from ca65_ls.types import SymbolKind

FIXTURES = Path(__file__).parent / "fixtures" / "test_repo" / "src"


def _load(name: str) -> Document:
    path = FIXTURES / name
    return Document(uri=path.as_uri(), text=path.read_text())


def _find(syms, name, kind=None):
    for s in syms:
        if s.name == name and (kind is None or s.kind == kind):
            return s
    return None


# --- top-level shape ----------------------------------------------------- #


def test_top_level_proc():
    doc = _load("main.s")
    procs = [s for s in doc.symbols if s.kind == SymbolKind.PROC]
    assert len(procs) == 1
    start = procs[0]
    assert start.name == "_start"
    assert start.scope_path == ()
    assert start.selection_range.start.line == 13  # 0-based: line 14 in the file
    # _start should contain at least one cheap local (@retry) plus
    # multiple anonymous labels.
    cheap = [c for c in start.children if c.kind == SymbolKind.CHEAP_LOCAL]
    assert any(c.name == "@retry" for c in cheap)


def test_nested_proc_in_scope():
    doc = _load("helpers.s")
    scope = _find(doc.symbols, "helpers", SymbolKind.SCOPE)
    assert scope is not None, "helpers scope must be present"
    assert scope.scope_path == ()

    # Both procs live inside the scope as children.
    nested = {c.name: c for c in scope.children if c.kind == SymbolKind.PROC}
    assert set(nested.keys()) == {"foo", "bar"}
    assert nested["foo"].scope_path == ("helpers",)
    assert nested["bar"].scope_path == ("helpers",)


def test_cheap_local_parent():
    doc = _load("helpers.s")
    flat = doc.flat_symbols()
    # @inner inside foo
    foo_inner = [
        s
        for s in flat
        if s.kind == SymbolKind.CHEAP_LOCAL
        and s.name == "@inner"
        and s.scope_path == ("helpers", "foo")
    ]
    assert len(foo_inner) == 1
    assert foo_inner[0].parent_label == "foo"

    # @inner inside bar must be distinct (different parent_label).
    bar_inner = [
        s
        for s in flat
        if s.kind == SymbolKind.CHEAP_LOCAL
        and s.name == "@inner"
        and s.scope_path == ("helpers", "bar")
    ]
    assert len(bar_inner) == 1
    assert bar_inner[0].parent_label == "bar"


def test_macro_definition():
    doc = _load("helpers.s")
    mac = _find(doc.symbols, "mac1", SymbolKind.MACRO)
    assert mac is not None
    # The body's only "label-shaped" thing is the macro arg, which the
    # parser intentionally does not emit as a child — but in case the
    # grammar surfaces anything spurious, at least confirm mac1 is not
    # also emitted as a plain LABEL.
    labels = [s for s in doc.flat_symbols() if s.kind == SymbolKind.LABEL]
    assert not any(s.name == "mac1" for s in labels)


def test_no_symbols_from_macro_invocation():
    """`mac1 $42` inside `helpers::foo` must NOT produce a label named ``mac1``."""
    doc = _load("helpers.s")
    flat = doc.flat_symbols()
    # No symbol should have name "mac1" except the MACRO definition itself.
    mac1s = [s for s in flat if s.name == "mac1"]
    assert len(mac1s) == 1
    assert mac1s[0].kind == SymbolKind.MACRO


def test_struct_with_fields():
    doc = _load("helpers.s")
    s = _find(doc.symbols, "S", SymbolKind.STRUCT)
    assert s is not None
    field_names = [c.name for c in s.children if c.kind == SymbolKind.FIELD]
    assert field_names == ["flags", "count"]
    for f in s.children:
        assert f.scope_path == ("S",), f"field {f.name} has scope_path {f.scope_path}"


def test_imports_exports_emitted():
    main = _load("main.s")
    helpers = _load("helpers.s")

    # main.s has five .import lines.
    imports = [s for s in main.symbols if s.kind == SymbolKind.IMPORT]
    assert {s.name for s in imports} == {
        "helpers_foo",
        "helpers_bar",
        "lib_export",
        "lib_buffer",
        "lib_const",
    }

    # helpers.s exports two aliases via `:= helpers::name` form.
    exports = [s for s in helpers.symbols if s.kind == SymbolKind.EXPORT]
    assert {s.name for s in exports} == {"helpers_foo", "helpers_bar"}


def test_anonymous_label_positions():
    doc = _load("main.s")
    flat = doc.flat_symbols()
    anons = [s for s in flat if s.kind == SymbolKind.ANON_LABEL]
    # main.s defines exactly two anonymous labels at lines 27 and 32
    # (0-based: 26 and 31). The test brief says three — but in the actual
    # corpus there are two. Verify the count matches the corpus exactly.
    # Inspect actual line numbers.
    lines = sorted({a.range.start.line for a in anons})
    expected_lines = []
    text = doc.text.splitlines()
    for i, line in enumerate(text):
        stripped = line.split(";", 1)[0].rstrip()
        bare = stripped.strip()
        # Only count bare-colon definitions, not :+/:- references.
        if (
            bare.startswith(":")
            and not bare.startswith((":+", ":-"))
            and (bare == ":" or bare.split()[0] == ":")
        ):
            expected_lines.append(i)
    assert len(anons) == len(expected_lines), (
        f"expected anon labels at lines {expected_lines}, got at {lines}"
    )
    # And the ranges must be distinct.
    assert len(set((a.range.start.line, a.range.start.character) for a in anons)) == len(anons)


def test_segments_landmark_main():
    doc = _load("main.s")
    segs = [s for s in doc.symbols if s.kind == SymbolKind.SEGMENT]
    assert any(s.name == "STARTUP" for s in segs)


def test_segments_landmark_lib():
    doc = _load("lib.s")
    seg_names = [s.name for s in doc.symbols if s.kind == SymbolKind.SEGMENT]
    assert "BSS" in seg_names
    assert "CODE" in seg_names


def test_constant_from_eq_and_assign():
    src = "FOO = $42\nBAR := $43\n"
    doc = Document(uri="file:///tmp/x.s", text=src)
    consts = [s for s in doc.symbols if s.kind == SymbolKind.CONSTANT]
    names = {c.name for c in consts}
    assert names == {"FOO", "BAR"}


def test_plain_label_top_level():
    doc = _load("lib.s")
    labels = [s for s in doc.symbols if s.kind == SymbolKind.LABEL]
    assert any(s.name == "lib_buffer" and s.scope_path == () for s in labels)


def test_exportzp_suppressed_when_defined_in_same_file():
    # zp.s both `.exportzp`s and defines ptr1/ptr2/tmp1, so the declarators are
    # redundant with the labels and are dropped; only the definitions remain.
    doc = _load("zp.s")
    exports = [s for s in doc.symbols if s.kind == SymbolKind.EXPORT]
    assert exports == []
    labels = {s.name for s in doc.symbols if s.kind == SymbolKind.LABEL}
    assert {"ptr1", "ptr2", "tmp1"} <= labels


def test_export_kept_when_target_not_defined_in_file():
    # A re-export of an imported symbol is the only thing this file says about
    # the name, so the declarator must survive.
    doc = Document(
        uri="file:///reexport.s",
        text=".import outside_sym\n.export outside_sym\n",
    )
    exports = [s for s in doc.symbols if s.kind == SymbolKind.EXPORT]
    assert [s.name for s in exports] == ["outside_sym"]


def test_export_suppression_dedups_proc():
    # `.export _start` + `.proc _start` is one entity, not two.
    doc = _load("main.s")
    matches = [s for s in doc.symbols if s.name == "_start"]
    assert len(matches) == 1
    assert matches[0].kind == SymbolKind.PROC


def test_references_for_lib_const():
    doc = _load("main.s")
    refs = doc.references_in("lib_const")
    # `.import lib_const` is a definition span and is excluded.
    # Two usages remain: `#<lib_const` and `#>lib_const`.
    assert len(refs) == 2
    for r in refs:
        assert r.name == "lib_const"


def test_references_exclude_macro_definition_name():
    doc = _load("helpers.s")
    refs = doc.references_in("mac1")
    # Only the invocation inside foo should remain (not the .macro
    # definition itself).
    assert len(refs) == 1
    assert refs[0].range.start.line == 26  # 0-based; ".endmacro" is line 19


def test_update_reparses():
    doc = Document(uri="file:///tmp/x.s", text=".proc a\n  rts\n.endproc\n")
    assert {s.name for s in doc.symbols} == {"a"}
    doc.update(".proc b\n  rts\n.endproc\n")
    assert {s.name for s in doc.symbols} == {"b"}


def test_flat_includes_children():
    doc = _load("helpers.s")
    flat = doc.flat_symbols()
    names = {s.name for s in flat}
    # helpers scope, both nested procs, struct + fields, macro, and the
    # cheap locals all show up.
    assert "helpers" in names
    assert "foo" in names
    assert "bar" in names
    assert "S" in names
    assert "flags" in names
    assert "count" in names
    assert "mac1" in names
    assert "@inner" in names


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
