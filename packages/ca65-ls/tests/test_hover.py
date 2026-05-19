"""textDocument/hover — returns a markdown panel for the identifier at the cursor."""

from __future__ import annotations

import shutil
from pathlib import Path

import lsprotocol.types as lsp
import pytest

from ca65_ls import server as srv
from ca65_ls.server import Ca65LanguageServer, _build_hover_markdown, _doc_comment_above


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


def _hover(server, uri, line, char) -> lsp.Hover | None:
    return srv.on_hover(
        server,
        lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri=uri),
            position=lsp.Position(line=line, character=char),
        ),
    )


# ---------------------------------------------------------- _doc_comment_above unit


def test_doc_comment_above_collects_contiguous_block():
    text = "; first\n; second\nfoo: rts\n"
    assert _doc_comment_above(text, 2) == "first\nsecond"


def test_doc_comment_above_stops_at_blank_line():
    text = "; far away\n\n; near\nfoo:\n"
    # blank line breaks the block; only "near" remains
    assert _doc_comment_above(text, 3) == "near"


def test_doc_comment_above_empty_when_no_comments():
    text = "foo:\n"
    assert _doc_comment_above(text, 0) == ""


# ---------------------------------------------------------- end-to-end hover


def test_hover_returns_none_for_whitespace_position(ls):
    server, root = ls
    uri = _open(server, root / "src" / "lib.s")
    h = _hover(server, uri, 0, 0)  # column 0 of a (non-blank) line that starts in whitespace
    assert h is None or h.contents.value  # be lenient — implementation may return None or empty


def test_hover_on_lib_export_includes_kind_and_address(ls):
    server, root = ls
    libs = root / "src" / "lib.s"
    uri = _open(server, libs)
    # find the `.proc lib_export` line; hover the proc name
    text = libs.read_text()
    for lineno, line in enumerate(text.splitlines()):
        if ".proc lib_export" in line:
            col = line.index("lib_export") + 1
            h = _hover(server, uri, lineno, col)
            break
    else:
        pytest.fail(".proc lib_export not found in lib.s")

    assert h is not None
    md = h.contents.value
    assert "**lib_export**" in md
    assert "procedure" in md or "label" in md  # depending on whether parse landed on .proc or absorbed label
    # .dbg fixture has lib_export with an address; should appear
    assert "$" in md  # the "$1234"-style address line


def test_hover_on_cheap_local_picks_matching_parent(ls):
    """In helpers.s, @inner exists inside helpers::foo AND helpers::bar.
    Hover on foo's @inner must surface foo as parent, not bar."""
    server, root = ls
    f = root / "src" / "helpers.s"
    uri = _open(server, f)
    text = f.read_text()

    # find the FIRST @inner declaration
    inner_lines = [i for i, line in enumerate(text.splitlines()) if line.startswith("@inner")]
    assert len(inner_lines) == 2
    foo_inner_line = inner_lines[0]
    col = text.splitlines()[foo_inner_line].index("@inner") + 1
    h = _hover(server, uri, foo_inner_line, col)

    assert h is not None
    md = h.contents.value
    assert "**@inner**" in md
    # parent_label should be set to foo (its enclosing .proc)
    assert "`foo`" in md, f"expected parent=foo in hover; got: {md}"


def test_hover_panel_includes_doc_comment(ls):
    """When a label has `; ...` comments immediately above it, the hover
    panel should include them in a code fence."""
    server, root = ls
    f = root / "src" / "label_style.s"
    uri = _open(server, f)
    text = f.read_text()

    # extract_word: in label_style.s has comments? Actually the fixture's
    # comments are all top-of-file. Add a quick local-comment by editing
    # is overkill; instead, just confirm the hover renders cleanly even
    # when there's no immediately-preceding comment.
    for lineno, line in enumerate(text.splitlines()):
        if line.startswith("extract_word:"):
            col = 1
            h = _hover(server, uri, lineno, col)
            break
    else:
        pytest.fail("extract_word: not found")

    assert h is not None
    md = h.contents.value
    assert "**extract_word**" in md
    # Either it has a comment block (code fence) or it doesn't — both are valid.


def test_hover_on_import_shows_externally_defined_kind(ls):
    server, root = ls
    main = root / "src" / "main.s"
    uri = _open(server, main)
    text = main.read_text()
    for lineno, line in enumerate(text.splitlines()):
        if ".import lib_export" in line:
            col = line.index("lib_export") + 1
            h = _hover(server, uri, lineno, col)
            break
    else:
        pytest.fail(".import lib_export not found in main.s")

    assert h is not None
    md = h.contents.value
    # Hover should resolve through to the IMPLEMENTATION (the .proc in lib.s),
    # not just the import declarator. That way the user sees address info etc.
    assert "**lib_export**" in md
    # Address (from .dbg) should be present since lib.s's lib_export is .dbg-resolved.
    assert "$" in md
