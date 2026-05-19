"""textDocument/rename — scope-aware label renaming.

Tests cover:
  * cheap-local rename stays inside the parent routine
  * cheap-local rename auto-prefixes @ if the user omits it
  * top-level label rename hits .import + .export declarators + call sites
  * anonymous labels refuse rename
  * cross-kind prefix mismatch (adding @ to a plain label) refuses
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
            text_document=lsp.TextDocumentItem(uri=uri, language_id="ca65", version=1, text=path.read_text())
        ),
    )
    return uri


def _rename(server, uri, line, char, new_name):
    return srv.on_rename(
        server,
        lsp.RenameParams(
            text_document=lsp.TextDocumentIdentifier(uri=uri),
            position=lsp.Position(line=line, character=char),
            new_name=new_name,
        ),
    )


def _prepare(server, uri, line, char):
    return srv.on_prepare_rename(
        server,
        lsp.PrepareRenameParams(
            text_document=lsp.TextDocumentIdentifier(uri=uri),
            position=lsp.Position(line=line, character=char),
        ),
    )


# ---------------------------------------------------------------- prepare


def test_prepare_rename_returns_range_for_identifier(ls):
    server, root = ls
    f = root / "src" / "same_name_locals.s"
    uri = _open(server, f)
    text = f.read_text()
    inner_lines = [i for i, line in enumerate(text.splitlines()) if line.startswith("@loop:")]
    pr = _prepare(server, uri, inner_lines[0], 1)
    assert pr is not None
    assert pr.placeholder == "@loop"


def test_prepare_rename_refuses_anonymous_label(ls):
    server, root = ls
    f = root / "src" / "main.s"
    uri = _open(server, f)
    # Find the line starting with `:` (anonymous label) in main.s
    text = f.read_text()
    for lineno, line in enumerate(text.splitlines()):
        if line.lstrip().startswith(":") and not line.lstrip().startswith(":="):
            pr = _prepare(server, uri, lineno, 0)
            assert pr is None
            return
    pytest.skip("No anonymous label in main.s fixture")


# ---------------------------------------------------------------- rename


def test_rename_cheap_local_scoped_to_parent(ls):
    """Renaming routine_a's @loop must NOT touch routine_b's @loop."""
    server, root = ls
    f = root / "src" / "same_name_locals.s"
    uri = _open(server, f)
    text = f.read_text()
    inner_lines = [i for i, line in enumerate(text.splitlines()) if line.startswith("@loop:")]
    assert len(inner_lines) == 2
    a_loop_line = inner_lines[0]

    edit = _rename(server, uri, a_loop_line, 1, "@walker")
    assert edit is not None
    changes = edit.changes
    assert len(changes) == 1, f"expected 1 file edited, got {list(changes)}"
    edits = next(iter(changes.values()))
    # All edits must fall inside routine_a's body (which starts at routine_a:
    # and ends just before routine_b:). The b_loop_line and any reference at
    # routine_b's range MUST NOT appear.
    b_loop_line = inner_lines[1]
    assert all(e.range.start.line < b_loop_line for e in edits), (
        f"rename leaked into routine_b's @loop at line {b_loop_line}: "
        f"{[e.range.start.line for e in edits]}"
    )
    # And every edit must replace with the new name.
    assert {e.new_text for e in edits} == {"@walker"}


def test_rename_cheap_local_auto_prefixes_at(ls):
    """User types `walker`, we silently make it `@walker` to match the kind."""
    server, root = ls
    f = root / "src" / "same_name_locals.s"
    uri = _open(server, f)
    text = f.read_text()
    a_loop_line = next(i for i, line in enumerate(text.splitlines()) if line.startswith("@loop:"))
    edit = _rename(server, uri, a_loop_line, 1, "walker")  # no leading @
    assert edit is not None
    edits = next(iter(edit.changes.values()))
    assert {e.new_text for e in edits} == {"@walker"}


def test_rename_top_level_label_global(ls):
    """Renaming `lib_export` hits its .proc def, its .export declarator, and
    main.s's .import + jsr call site."""
    server, root = ls
    libs = root / "src" / "lib.s"
    uri = _open(server, libs)
    _open(server, root / "src" / "main.s")  # also open main so refs index is hot

    # Cursor on `.proc lib_export` -> the proc-name token.
    text = libs.read_text()
    for lineno, line in enumerate(text.splitlines()):
        if ".proc lib_export" in line:
            col = line.index("lib_export") + 1
            break
    else:
        pytest.fail(".proc lib_export not found")

    edit = _rename(server, uri, lineno, col, "renamed_export")
    assert edit is not None
    # Both lib.s and main.s should be in the edits dict.
    files_with_edits = {u.rsplit("/", 1)[-1] for u in edit.changes}
    assert "lib.s" in files_with_edits, f"got {files_with_edits}"
    assert "main.s" in files_with_edits, f"got {files_with_edits}"

    # All edits must use the new name.
    all_edits = [e for edits in edit.changes.values() for e in edits]
    assert {e.new_text for e in all_edits} == {"renamed_export"}


def test_rename_refuses_adding_at_prefix_to_plain_label(ls):
    """Renaming `lib_export` (a plain label) to `@lib_export` would change
    its kind; we refuse rather than silently strip the @."""
    server, root = ls
    libs = root / "src" / "lib.s"
    uri = _open(server, libs)
    text = libs.read_text()
    lineno, col = next(
        ((i, line.index("lib_export") + 1) for i, line in enumerate(text.splitlines())
         if ".proc lib_export" in line),
        (None, None),
    )
    assert lineno is not None
    edit = _rename(server, uri, lineno, col, "@cheap_now")
    assert edit is None


def test_rename_anonymous_label_returns_none(ls):
    """Anonymous labels (`:`) can't be renamed."""
    server, root = ls
    main = root / "src" / "main.s"
    uri = _open(server, main)
    text = main.read_text()
    for lineno, line in enumerate(text.splitlines()):
        stripped = line.lstrip()
        if stripped.startswith(":") and not stripped.startswith(":="):
            # cursor at the `:`
            col = len(line) - len(stripped)
            assert _rename(server, uri, lineno, col, "no_op") is None
            return
    pytest.skip("No anonymous label in main.s fixture")


def test_rename_no_identifier_at_cursor_returns_none(ls):
    server, root = ls
    f = root / "src" / "lib.s"
    uri = _open(server, f)
    # column 0 of an indented code line: whitespace, no identifier
    edit = _rename(server, uri, 0, 0, "anything")
    assert edit is None
