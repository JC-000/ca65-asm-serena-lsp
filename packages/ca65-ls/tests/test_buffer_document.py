"""Tests for the buffer-layer Document.

These exercise the synthetic CA65 corpus under
``tests/fixtures/test_repo/src``.  They validate the contract pinned in
``ca65_ls/types.py`` so the Indexer can rely on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ca65_ls.buffer import Document
from ca65_ls.types import Position, Range, SymbolKind

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


# --- regression guards for the 2026-09-02 review fixes -------------------- #
#
# Each of these pins a parser fix with a synthetic input.  The red tests that
# found the defects live in tests/test_review_red.py; these stay green.


def _doc(text: str) -> Document:
    return Document(uri="file:///synthetic.s", text=text)


def _spelled(text: str, rng) -> str:
    """The source text a Range addresses, in code points."""
    lines = text.split("\n")
    assert rng.start.line == rng.end.line
    return lines[rng.start.line][rng.start.character : rng.end.character]


def _refs(doc: Document, name: str):
    return [r for r in doc.all_references() if r.name == name]


# 1. label bodies stop at the enclosing container


def test_label_body_stops_before_endproc():
    doc = _doc(".proc p\n lda #0\ndone:\n rts\n.endproc\n\n.proc q\n rts\n.endproc\n")
    p = _find(doc.symbols, "p")
    done = _find(p.children, "done", SymbolKind.LABEL)
    assert done.range.end == Position(line=3, character=4)
    assert (done.range.end.line, done.range.end.character) <= (
        p.range.end.line,
        p.range.end.character,
    )


def test_label_body_stops_before_endscope_and_endmacro():
    doc = _doc(".scope s\nl1:\n nop\n.endscope\n.macro m\nl2:\n nop\n.endmacro\nrest:\n rts\n")
    l1 = _find(_find(doc.symbols, "s").children, "l1")
    l2 = _find(_find(doc.symbols, "m").children, "l2")
    assert l1.range.end.line == 2
    assert l2.range.end.line == 6
    # File-level labels still run to EOF.
    assert _find(doc.symbols, "rest").range.end.line == 9


def test_label_body_inside_proc_still_absorbs_cheap_locals():
    doc = _doc(".proc p\nloop:\n@a:\n dex\n bne @a\n rts\n.endproc\n")
    loop = _find(_find(doc.symbols, "p").children, "loop")
    assert [c.name for c in loop.children] == ["@a"]
    assert loop.range.end.line == 5


# 2. columns are code points


def test_columns_are_code_points_on_multibyte_lines():
    text = '        .byte "日本語", >target\nX = 1 ; ünïcödé\n  lda X ; 日本 X\ntarget: rts\n'
    doc = _doc(text)
    for r in doc.all_references():
        assert _spelled(text, r.range) == r.name, r
    for s in doc.flat_symbols():
        assert _spelled(text, s.selection_range) == s.name, s
    ref = _refs(doc, "target")[0]
    assert ref.range.start.character == text.split("\n")[0].index("target")


def test_crlf_file_positions_are_line_accurate():
    text = "foo:\r\n jsr bar\r\n.proc bar\r\n rts\r\n.endproc\r\n"
    doc = _doc(text)
    assert [
        (r.name, r.range.start.line, r.range.start.character) for r in doc.all_references()
    ] == [("bar", 1, 5)]
    bar = _find(doc.symbols, "bar", SymbolKind.PROC)
    assert bar.selection_range == Range(Position(2, 6), Position(2, 9))
    assert _find(doc.symbols, "foo").range.end.line == 1


# 3. export declarators of in-file definitions are references


@pytest.mark.parametrize("directive", [".export", ".exportzp", ".global", ".globalzp"])
def test_export_declarator_of_local_definition_is_a_reference(directive):
    text = f"{directive} foo, bar\n.proc foo\n rts\n.endproc\nbar = 1\n"
    doc = _doc(text)
    assert [s.kind for s in doc.symbols if s.kind == SymbolKind.EXPORT] == []
    for name in ("foo", "bar"):
        (ref,) = _refs(doc, name)
        assert ref.range.start.line == 0
        assert _spelled(text, ref.range) == name


def test_reexport_of_foreign_symbol_is_a_symbol_not_a_reference():
    # Only one record per token, or rename would apply two edits to one span.
    doc = _doc(".import outside\n.export outside\n.global other\n")
    assert [(s.name, s.kind) for s in doc.symbols] == [
        ("outside", SymbolKind.IMPORT),
        ("outside", SymbolKind.EXPORT),
        ("other", SymbolKind.EXPORT),
    ]
    assert doc.all_references() == []


def test_export_with_assignment_keeps_rhs_references():
    doc = _doc(".export alias := helpers::foo\n.proc alias\n rts\n.endproc\n")
    names = sorted(r.name for r in doc.all_references())
    assert names == ["alias", "foo", "helpers"]


# 4. macro-invocation arguments


def test_macro_arguments_yield_references():
    text = (
        ".macro stax arg\n sta arg\n.endmacro\n"
        " stax tcp_callback\n"
        ' ldax #eth_name, "str", $ff, .sizeof(blk), x, foo::bar ; not_this\n'
        " lda ptr+off ; comment\n"
    )
    doc = _doc(text)
    names = [r.name for r in doc.all_references()]
    assert names.count("tcp_callback") == 1
    assert names.count("eth_name") == 1
    for wanted in ("blk", "foo", "bar", "ptr", "off"):
        assert wanted in names, wanted
    for unwanted in ("str", "ff", "sizeof", "x", "not_this", "comment"):
        assert unwanted not in names, unwanted
    for r in doc.all_references():
        assert _spelled(text, r.range) == r.name


def test_macro_argument_references_carry_the_enclosing_scope():
    doc = _doc(".proc p\n stax foo\n.endproc\n")
    assert [(r.name, r.scope_path) for r in doc.all_references()] == [
        ("stax", ("p",)),
        ("foo", ("p",)),
    ]


# 5. leading ``::`` global-scope operator


def test_global_scope_operator_in_if_and_operands():
    text = ".if ::FLAG\nX = 1\n.else\nX = 2\n.endif\n lda ::foo\n jsr foo::bar\n"
    doc = _doc(text)
    xs = [s for s in doc.flat_symbols() if s.name == "X"]
    assert [s.selection_range.start.line for s in xs] == [1, 3]
    assert all(s.kind == SymbolKind.CONSTANT for s in xs)
    refs = {(r.name, r.range.start.line, r.range.start.character) for r in doc.all_references()}
    assert refs == {("FLAG", 0, 6), ("foo", 5, 7), ("foo", 6, 5), ("bar", 6, 10)}


# 6. ``.define`` / ``.set``


def test_define_and_set_symbols():
    text = ".define FOO 1\n.define MAC(v) v+1\nBAR .set 2\n lda FOO\n"
    doc = _doc(text)
    kinds = {(s.name, s.kind) for s in doc.symbols}
    assert kinds == {
        ("FOO", SymbolKind.CONSTANT),
        ("MAC", SymbolKind.MACRO),
        ("BAR", SymbolKind.CONSTANT),
    }
    for s in doc.symbols:
        assert _spelled(text, s.selection_range) == s.name
    # The definition tokens and the parameter name are not references.
    assert [(r.name, r.range.start.line) for r in doc.all_references()] == [("FOO", 3)]


# 7. address-size prefixes


def test_address_size_prefixes_are_not_labels():
    text = "foo: lda a:bar\n sta z:baz\n jmp f:qux\n lda (z:ptr),y\n"
    doc = _doc(text)
    assert [(s.name, s.kind) for s in doc.symbols] == [("foo", SymbolKind.LABEL)]
    names = sorted(r.name for r in doc.all_references())
    assert names == ["bar", "baz", "ptr", "qux"]
    for r in doc.all_references():
        assert _spelled(text, r.range) == r.name


def test_indented_single_letter_label_is_still_a_label():
    doc = _doc("  z: rts\n  f: nop\n")
    assert [s.name for s in doc.symbols] == ["z", "f"]


# 8. ``.feature labels_without_colons``


def test_labels_without_colons_yield_labels_and_keep_operand_references():
    text = (
        ".feature labels_without_colons\n"
        ".macro mymac arg\n lda arg\n.endmacro\n"
        "InitChar\n lda #0\n rts\n"
        "NextChar lda count ; comment\n rts\n"
        "count = 3\n"
        "Data .byte 1\n"
        "lda #1\n"
        "mymac target\n"
        "@loc\n"
    )
    doc = _doc(text)
    labels = {s.name: s for s in doc.flat_symbols() if s.kind == SymbolKind.LABEL}
    assert set(labels) == {"InitChar", "NextChar", "Data"}
    for s in labels.values():
        assert _spelled(text, s.selection_range) == s.name
    assert labels["InitChar"].range.end.line == 6
    assert _find(doc.symbols, "count", SymbolKind.CONSTANT) is not None
    assert _find(doc.symbols, "mymac", SymbolKind.MACRO) is not None
    assert "@loc" in {s.name for s in doc.flat_symbols() if s.kind == SymbolKind.CHEAP_LOCAL}
    names = [r.name for r in doc.all_references()]
    assert "count" in names and "target" in names
    assert "InitChar" not in names and "NextChar" not in names


def test_without_the_feature_bare_identifiers_stay_macro_calls():
    doc = _doc("InitChar\n lda #0\nNextChar lda #1\n")
    assert doc.symbols == []
    assert sorted(r.name for r in doc.all_references()) == ["InitChar", "NextChar"]


# 9. ACME dialect


def test_acme_dialect_file_is_skipped():
    doc = _doc("!zone fp_add {\nfp_add:\n rts\n}\n!byte 1, 2\n")
    assert doc.is_acme
    assert doc.symbols == []
    assert doc.all_references() == []


def test_ca65_file_with_bang_in_a_string_is_not_acme():
    doc = _doc('.segment "CODE"\nmsg: .byte "!byte is not a directive", 0\n')
    assert not doc.is_acme
    assert _find(doc.symbols, "msg", SymbolKind.LABEL) is not None


def test_update_recomputes_dialect_and_columns():
    doc = _doc("!zone x\n")
    assert doc.is_acme
    doc.update('  .byte "é", <target\ntarget: rts\n')
    assert not doc.is_acme
    (ref,) = _refs(doc, "target")
    assert ref.range.start.character == doc.text.split("\n")[0].index("target")
