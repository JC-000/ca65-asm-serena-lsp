"""RED tests from the adversarial review of 2026-09-02, synthetic layer.

Every test here reproduces a defect the reviewer found on the real corpus with
a minimal in-repo input, so it runs without the corpus. All are
``xfail(strict=True, raises=AssertionError)``: they fail today on an assertion,
XPASS (and so fail the gate) once fixed, and any other exception is a real
failure of the test itself. Corpus-scale versions live in tests/corpus/.
"""

from __future__ import annotations

from pathlib import Path

import lsprotocol.types as lsp
import pytest

from ca65_ls import server as srv
from ca65_ls.buffer.document import Document
from ca65_ls.index.workspace import WorkspaceIndex
from ca65_ls.server import Ca65LanguageServer, _path_to_uri
from ca65_ls.types import SymbolKind

red = pytest.mark.xfail(strict=True, raises=AssertionError)


def _doc(text: str) -> Document:
    return Document("file:///synthetic.s", text)


def _names(doc: Document) -> list[tuple[str, str]]:
    return [(s.name, s.kind.value) for s in doc.flat_symbols()]


def _ref_names(doc: Document) -> list[str]:
    return [r.name for r in doc.all_references()]


class Workspace:
    """A throwaway project on disk with a cache-free index and a server."""

    def __init__(self, root: Path, files: dict[str, str], gitignore: str | None = None):
        self.root = root
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        if gitignore is not None:
            (root / ".gitignore").write_text(gitignore)
        self.index = WorkspaceIndex(root, cache=False)
        self.index.reindex()
        self.server = Ca65LanguageServer()
        self.server.indexes[_path_to_uri(root)] = self.index
        self.server.workspace_roots.append(root)

    def uri(self, rel: str) -> str:
        return _path_to_uri(self.root / rel)

    def pos(self, rel: str, needle: str, occurrence: int = 0) -> lsp.Position:
        lines = (self.root / rel).read_text().splitlines()
        seen = 0
        for i, line in enumerate(lines):
            if needle in line:
                if seen == occurrence:
                    return lsp.Position(line=i, character=line.index(needle) + 1)
                seen += 1
        raise ValueError(needle)

    def definition(self, rel: str, needle: str, occurrence: int = 0):
        return (
            srv.on_definition(
                self.server,
                lsp.DefinitionParams(
                    text_document=lsp.TextDocumentIdentifier(uri=self.uri(rel)),
                    position=self.pos(rel, needle, occurrence),
                ),
            )
            or []
        )

    def references(self, rel: str, needle: str, occurrence: int = 0):
        params = lsp.ReferenceParams(
            text_document=lsp.TextDocumentIdentifier(uri=self.uri(rel)),
            position=self.pos(rel, needle, occurrence),
            context=lsp.ReferenceContext(include_declaration=False),
        )
        return srv.on_references(self.server, params) or []

    def rename(
        self, rel: str, needle: str, new_name: str, occurrence: int = 0
    ) -> dict[str, list[int]]:
        edit = srv.on_rename(
            self.server,
            lsp.RenameParams(
                text_document=lsp.TextDocumentIdentifier(uri=self.uri(rel)),
                position=self.pos(rel, needle, occurrence),
                new_name=new_name,
            ),
        )
        changes = (edit.changes if edit else None) or {}
        return {
            Path(srv._uri_to_path(u)).relative_to(self.root).as_posix(): sorted(
                e.range.start.line for e in edits
            )
            for u, edits in changes.items()
        }


PROC_WITH_LOCAL_LABEL = """.proc {name}
        ldx #0
done:
        dex
        bne done
        rts
.endproc
"""


# ---------------------------------------------------------------- F1 / index walk


@red
def test_worktree_copies_are_not_indexed_even_with_star_gitignore(tmp_path):
    """F1: `.claude/*` in .gitignore does not exclude files two levels down
    because the indexer asks pathspec about file paths only."""
    ws = Workspace(
        tmp_path,
        {
            "src/a.s": ".proc foo\n rts\n.endproc\n",
            ".claude/worktrees/agent-1/src/a.s": ".proc foo\n rts\n.endproc\n",
        },
        gitignore=".claude/*\n",
    )
    assert [
        Path(srv._uri_to_path(s.uri)).relative_to(tmp_path).as_posix()
        for s in ws.index.lookup("foo")
    ] == ["src/a.s"]


@red
def test_dot_claude_is_ignored_by_default(tmp_path):
    """F1: c64-mlkem and c64-ChaCha20-Poly1305 do not gitignore .claude at all."""
    ws = Workspace(
        tmp_path,
        {
            "src/a.s": ".proc foo\n rts\n.endproc\n",
            ".claude/worktrees/agent-1/src/a.s": ".proc foo\n rts\n.endproc\n",
        },
    )
    assert len(ws.index.lookup("foo")) == 1


# ---------------------------------------------------------------- F2 / double store


@red
def test_nested_symbols_are_stored_once(tmp_path):
    """F2: _ingest_file re-flattens an already flat list; a cheap local under
    a label under a proc is stored three times."""
    ws = Workspace(
        tmp_path, {"a.s": ".proc p\nloop:\n@inner:\n dex\n bne @inner\n bne loop\n rts\n.endproc\n"}
    )
    assert len(ws.index.lookup("loop")) == 1
    assert len(ws.index.lookup("@inner")) == 1


@red
def test_rename_emits_one_edit_per_site(tmp_path):
    ws = Workspace(tmp_path, {"a.s": ".proc p\nloop:\n dex\n bne loop\n rts\n.endproc\n"})
    edits = ws.rename("a.s", "loop:", "again")
    assert edits == {"a.s": [1, 3]}


# ---------------------------------------------------------------- F3 / F13 proc-local labels


@red
def test_label_before_endproc_stops_at_endproc():
    doc = _doc(".proc p\n lda #0\ndone:\n rts\n.endproc\n\n.proc q\n rts\n.endproc\n")
    done = next(s for s in doc.flat_symbols() if s.name == "done")
    assert done.range.end.line <= 4, f"label body runs to line {done.range.end.line}"


@red
def test_references_of_proc_local_label_stay_in_the_proc(tmp_path):
    """F3: the overrunning body defeats _range_strictly_inside, so
    references (and rename) for `done:` go project-wide. c64-mlkem
    sponge.s:235 `done` -> 65 references across 4 files."""
    ws = Workspace(
        tmp_path,
        {
            "a.s": PROC_WITH_LOCAL_LABEL.format(name="a"),
            "b.s": PROC_WITH_LOCAL_LABEL.format(name="b"),
        },
    )
    refs = ws.references("a.s", "done:")
    assert {srv._uri_to_path(r.uri).name for r in refs} == {"a.s"}, [
        (srv._uri_to_path(r.uri).name, r.range.start.line) for r in refs
    ]


@red
def test_rename_of_proc_local_label_is_confined_to_its_proc(tmp_path):
    """F3, the destructive half: renaming `done` in one proc must not touch
    any other proc's `done`."""
    ws = Workspace(
        tmp_path,
        {
            "a.s": PROC_WITH_LOCAL_LABEL.format(name="a"),
            "b.s": PROC_WITH_LOCAL_LABEL.format(name="b"),
        },
    )
    assert ws.rename("a.s", "done:", "finish") == {"a.s": [2, 4]}


@red
def test_definition_of_proc_local_label_is_scope_filtered(tmp_path):
    """F13: on_definition has no scope filtering at all."""
    ws = Workspace(
        tmp_path,
        {
            "a.s": PROC_WITH_LOCAL_LABEL.format(name="a"),
            "b.s": PROC_WITH_LOCAL_LABEL.format(name="b"),
        },
    )
    locs = ws.definition("b.s", "bne done")
    assert [(srv._uri_to_path(loc.uri).name, loc.range.start.line) for loc in locs] == [("b.s", 2)]


# ---------------------------------------------------------------- F4 / export declarator


@red
def test_rename_also_edits_the_export_declarator(tmp_path):
    """F4: the 2026-08-28 export-suppression fix dropped the declarator from
    the references too, so a rename leaves `.export old_name` behind and the
    build breaks. 3885 such declarators across the corpus."""
    ws = Workspace(
        tmp_path,
        {"lib.s": ".export foo\n.proc foo\n rts\n.endproc\n", "main.s": ".import foo\n jsr foo\n"},
    )
    edits = ws.rename("lib.s", ".proc foo", "bar")
    assert edits.get("lib.s") == [0, 1], edits
    assert edits.get("main.s") == [0, 1], edits


@red
def test_export_declarator_is_a_reference_of_its_target():
    doc = _doc(".export foo\n.proc foo\n rts\n.endproc\n")
    assert "foo" in _ref_names(doc)


# ---------------------------------------------------------------- F5 / macro arguments


@red
def test_symbols_passed_as_macro_arguments_are_references():
    """F5: 1390 of 1390 macro-argument uses across the corpus have no
    reference record; ip65 code is almost entirely such macros."""
    doc = _doc(".macro ldax a\n lda a\n.endmacro\n ldax foo_table\nfoo_table: .byte 0\n")
    assert "foo_table" in _ref_names(doc)


# ---------------------------------------------------------------- F6 / labels_without_colons


@red
def test_labels_without_colons_feature_still_yields_labels():
    """F6: with the feature enabled the whole file becomes one ERROR node and
    656 labels vanish from c64-https' three vt100 drivers."""
    doc = _doc(".feature labels_without_colons\nInitChar\n lda #0\n rts\nNextChar\n rts\n")
    assert {"InitChar", "NextChar"} <= {n for n, _ in _names(doc)}


# ---------------------------------------------------------------- F7 / F8 constants


@red
def test_if_global_scope_flag_does_not_swallow_the_next_constant():
    doc = _doc(".if ::FLAG\nX = 1\n.else\nX = 2\n.endif\n")
    lines = sorted(s.selection_range.start.line for s in doc.flat_symbols() if s.name == "X")
    assert lines == [1, 3]


@red
def test_global_scope_reference_is_recorded():
    doc = _doc(".if ::FLAG\nX = 1\n.endif\n")
    assert "FLAG" in _ref_names(doc)


@red
@pytest.mark.parametrize("text,name", [(".define FOO 1\n", "FOO"), ("BAR .set 2\n", "BAR")])
def test_define_and_set_produce_symbols(text, name):
    assert name in {n for n, _ in _names(_doc(text))}


# ---------------------------------------------------------------- F9 / ACME dialect


@red
def test_acme_dialect_file_contributes_no_symbols(tmp_path):
    ws = Workspace(
        tmp_path,
        {
            "mod.s": ".proc fp_add\n rts\n.endproc\n",
            "mod.asm": "!zone fp_add {\nfp_add:\n rts\n}\n",
        },
    )
    assert [srv._uri_to_path(s.uri).name for s in ws.index.lookup("fp_add")] == ["mod.s"]


# ---------------------------------------------------------------- F10 / definition preference


@red
def test_same_file_label_wins_over_foreign_proc(tmp_path):
    """F10: on_definition prefers any PROC over the LABEL that actually
    defines the name for this caller (c64-wireguard print_string)."""
    ws = Workspace(
        tmp_path,
        {
            "boot.s": "print_string:\n rts\n jsr print_string\n",
            "other/main.s": ".proc print_string\n rts\n.endproc\n",
        },
    )
    locs = ws.definition("boot.s", "jsr print_string")
    assert [srv._uri_to_path(loc.uri).name for loc in locs] == ["boot.s"]


# ---------------------------------------------------------------- F11 / F12 parser oddities


@red
def test_address_size_prefix_is_not_an_anonymous_label():
    doc = _doc("foo: lda a:bar\n")
    assert SymbolKind.ANON_LABEL not in {s.kind for s in doc.flat_symbols()}
    assert "bar" in _ref_names(doc)


def test_macro_body_labels_are_children_of_the_macro():
    """F12 did not reproduce: the label is nested under the macro in the
    symbol tree, not at file scope. Pinned green."""
    doc = _doc(".macro m\n.local lbl\nlbl: rts\n.endmacro\n")
    top = {s.name for s in doc.symbols}
    assert "lbl" not in top
    assert "lbl" in {s.name for s in doc.flat_symbols()}


# ---------------------------------------------------------------- Serena review F03 / columns


@red
def test_reference_columns_are_code_points_not_bytes():
    """Serena review F03: columns are UTF-8 byte offsets, so a rename on a
    line with non-ASCII text before the identifier mangles the line
    ('.byte "日本語", >target' became '>targettgtper')."""
    line = '        .byte "日本語", >target'
    doc = _doc(line + "\ntarget: rts\n")
    ref = next(r for r in doc.all_references() if r.name == "target")
    assert ref.range.start.character == line.index("target")


@red
def test_rename_on_non_ascii_line_keeps_the_line_intact(tmp_path):
    ws = Workspace(tmp_path, {"a.s": '        .byte "ü", <target\ntarget: rts\n'})
    edits = ws.rename("a.s", "target: rts", "tgt")
    assert edits == {"a.s": [0, 1]}
    text = (tmp_path / "a.s").read_text().splitlines()
    ref = next(
        r for r in Document("file:///a.s", "\n".join(text)).all_references() if r.name == "target"
    )
    assert text[0][ref.range.start.character : ref.range.end.character] == "target"
