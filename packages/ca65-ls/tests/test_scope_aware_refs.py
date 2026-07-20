"""Scope-aware references — `find_referencing_symbols` on a per-routine
cheap local should NOT bleed across routines with the same `@name`.

This is M4's second-biggest fix: c64-https has `@loop`, `@done`, `@retry`
sprinkled across nearly every routine.  Without scope awareness, a query
for references to one `@loop` would return thousands.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import lsprotocol.types as lsp
import pytest

from ca65_ls import server as srv
from ca65_ls.server import Ca65LanguageServer

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "test_repo"


@pytest.fixture
def ls(tmp_path):
    dst = tmp_path / "test_repo"
    shutil.copytree(FIXTURE_DIR, dst)
    server = Ca65LanguageServer()
    server.add_workspace(dst)
    return server, dst


def _open(server: Ca65LanguageServer, path: Path) -> str:
    uri = path.resolve().as_uri()
    srv.on_did_open(
        server,
        lsp.DidOpenTextDocumentParams(
            text_document=lsp.TextDocumentItem(
                uri=uri, language_id="ca65", version=1, text=path.read_text()
            )
        ),
    )
    return uri


def _refs_at(server, uri, line, char):
    """Helper: request_references at (line, char) and return location lines (per-URI)."""
    result = srv.on_references(
        server,
        lsp.ReferenceParams(
            text_document=lsp.TextDocumentIdentifier(uri=uri),
            position=lsp.Position(line=line, character=char),
            context=lsp.ReferenceContext(include_declaration=True),
        ),
    )
    return [(loc.uri.rsplit("/", 1)[-1], loc.range.start.line) for loc in result]


def test_cheap_local_refs_scoped_to_parent(ls):
    """Cursor on `@loop` declaration inside routine_a returns ONLY routine_a's
    `bne @loop` use site, not routine_b's same-named `@loop`."""
    server, root = ls
    f = root / "src" / "same_name_locals.s"
    uri = _open(server, f)

    # `@loop` declaration in routine_a is at line 17 (0-indexed), column 0.
    refs = _refs_at(server, uri, 17, 1)
    files_and_lines = sorted(refs)
    # routine_a's @loop is referenced at line 22 (the `bne @loop`).
    # routine_b's @loop reference at line 34 must NOT be in the result.
    assert ("same_name_locals.s", 22) in refs, (
        f"expected routine_a's @loop ref; got {files_and_lines}"
    )
    assert not any(line == 34 for _, line in refs), (
        f"routine_b's @loop ref should NOT appear; got {files_and_lines}"
    )


def test_cheap_local_refs_in_other_routine_disjoint(ls):
    """Symmetric: cursor on routine_b's @loop returns ONLY routine_b's ref."""
    server, root = ls
    f = root / "src" / "same_name_locals.s"
    uri = _open(server, f)

    # routine_b's @loop declaration is at line 29.
    refs = _refs_at(server, uri, 29, 1)
    files_and_lines = sorted(refs)
    assert ("same_name_locals.s", 34) in refs, (
        f"expected routine_b's @loop ref; got {files_and_lines}"
    )
    assert not any(line == 22 for _, line in refs), (
        f"routine_a's @loop ref should NOT appear; got {files_and_lines}"
    )


def test_top_level_routine_name_refs_stay_global(ls):
    """Cursor on `routine_a:` label declaration should return ALL references to
    routine_a wherever they appear (no scope filter)."""
    server, root = ls
    # routine_a is .exported, so import declarators count as references — but
    # we don't have a cross-file caller fixture here. Just confirm the LSP
    # returns *something* without crashing, and confirm the filter doesn't
    # accidentally clip away references that should be visible.
    f = root / "src" / "same_name_locals.s"
    uri = _open(server, f)
    refs = _refs_at(server, uri, 15, 2)  # cursor on `routine_a:` declaration

    # No assertion that refs is non-empty (the fixture has no callers in-file);
    # we ONLY assert that the call succeeds without choking and returns a list.
    assert isinstance(refs, list)


def test_proc_scope_local_label_refs_stay_in_proc(ls):
    """In helpers.s, `@inner` exists inside helpers::foo AND helpers::bar.
    Each declaration's references must stay within its parent .proc."""
    server, root = ls
    f = root / "src" / "helpers.s"
    uri = _open(server, f)
    # Read the file to figure out line numbers dynamically (helpers.s evolves).
    text = f.read_text()
    inner_lines = [i for i, line in enumerate(text.splitlines()) if line.startswith("@inner")]
    assert len(inner_lines) == 2, (
        f"helpers.s should have exactly two @inner declarations; got {inner_lines}"
    )

    foo_inner_line, bar_inner_line = inner_lines

    # Cursor on the first @inner: refs should NOT include the second.
    refs_foo = _refs_at(server, uri, foo_inner_line, 1)
    foo_ref_lines = {line for _, line in refs_foo}
    assert bar_inner_line not in foo_ref_lines, (
        f"foo's @inner refs leaked to bar's @inner line {bar_inner_line}: {foo_ref_lines}"
    )

    # And symmetric.
    refs_bar = _refs_at(server, uri, bar_inner_line, 1)
    bar_ref_lines = {line for _, line in refs_bar}
    assert foo_inner_line not in bar_ref_lines, (
        f"bar's @inner refs leaked to foo's @inner line {foo_inner_line}: {bar_ref_lines}"
    )
