"""Tests for ca65_ls.index.workspace.WorkspaceIndex.

These tests use the `BufferShim` to stand in for the still-being-built
Parser layer. They verify the indexer's own logic — discovery, caching,
.dbg enrichment, scope-aware lookups — independent of any tree-sitter
parse correctness.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from ca65_ls.index.workspace import BufferShim, WorkspaceIndex
from ca65_ls.types import (
    BufferSymbol,
    Position,
    Range,
    SymbolKind,
)

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "test_repo"


def _rg(line: int, col: int, end_col: int) -> Range:
    return Range(Position(line, col), Position(line, end_col))


# ---------------------------------------------------------------- shim corpus
#
# What we expect the real Parser engineer's `Document` to extract for each
# file in tests/fixtures/test_repo/src/. These positions don't have to be
# byte-perfect (positions are the Parser's contract, not the Indexer's);
# they just have to be self-consistent.


def _shim_symbols() -> dict[str, list[BufferSymbol]]:
    return {
        "zp.s": [
            BufferSymbol(
                name="ptr1",
                kind=SymbolKind.LABEL,
                range=_rg(6, 0, 4),
                selection_range=_rg(6, 0, 4),
                scope_path=(),
                parent_label=None,
            ),
            BufferSymbol(
                name="ptr2",
                kind=SymbolKind.LABEL,
                range=_rg(7, 0, 4),
                selection_range=_rg(7, 0, 4),
                scope_path=(),
                parent_label=None,
            ),
            BufferSymbol(
                name="tmp1",
                kind=SymbolKind.LABEL,
                range=_rg(8, 0, 4),
                selection_range=_rg(8, 0, 4),
                scope_path=(),
                parent_label=None,
            ),
        ],
        "lib.s": [
            BufferSymbol(
                name="lib_export",
                kind=SymbolKind.PROC,
                range=_rg(13, 0, 18),
                selection_range=_rg(13, 6, 16),
                scope_path=(),
                parent_label=None,
            ),
            BufferSymbol(
                name="lib_buffer",
                kind=SymbolKind.LABEL,
                range=_rg(9, 0, 10),
                selection_range=_rg(9, 0, 10),
                scope_path=(),
                parent_label=None,
            ),
            BufferSymbol(
                name="lib_const",
                kind=SymbolKind.CONSTANT,
                range=_rg(6, 8, 17),
                selection_range=_rg(6, 8, 17),
                scope_path=(),
                parent_label=None,
            ),
        ],
        "helpers.s": [
            BufferSymbol(
                name="helpers",
                kind=SymbolKind.SCOPE,
                range=_rg(23, 0, 41),
                selection_range=_rg(23, 7, 14),
                scope_path=(),
                parent_label=None,
                children=(
                    BufferSymbol(
                        name="foo",
                        kind=SymbolKind.PROC,
                        range=_rg(25, 0, 33),
                        selection_range=_rg(25, 6, 9),
                        scope_path=("helpers",),
                        parent_label=None,
                        children=(
                            BufferSymbol(
                                name="@inner",
                                kind=SymbolKind.CHEAP_LOCAL,
                                range=_rg(28, 0, 6),
                                selection_range=_rg(28, 0, 6),
                                scope_path=("helpers", "foo"),
                                parent_label="foo",
                            ),
                        ),
                    ),
                    BufferSymbol(
                        name="bar",
                        kind=SymbolKind.PROC,
                        range=_rg(34, 0, 40),
                        selection_range=_rg(34, 6, 9),
                        scope_path=("helpers",),
                        parent_label=None,
                        children=(
                            BufferSymbol(
                                name="@inner",
                                kind=SymbolKind.CHEAP_LOCAL,
                                range=_rg(35, 0, 6),
                                selection_range=_rg(35, 0, 6),
                                scope_path=("helpers", "bar"),
                                parent_label="bar",
                            ),
                        ),
                    ),
                ),
            ),
            BufferSymbol(
                name="S",
                kind=SymbolKind.STRUCT,
                range=_rg(11, 0, 14),
                selection_range=_rg(11, 8, 9),
                scope_path=(),
                parent_label=None,
                children=(
                    BufferSymbol(
                        name="flags",
                        kind=SymbolKind.FIELD,
                        range=_rg(12, 8, 13),
                        selection_range=_rg(12, 8, 13),
                        scope_path=("S",),
                        parent_label=None,
                    ),
                    BufferSymbol(
                        name="count",
                        kind=SymbolKind.FIELD,
                        range=_rg(13, 8, 13),
                        selection_range=_rg(13, 8, 13),
                        scope_path=("S",),
                        parent_label=None,
                    ),
                ),
            ),
            BufferSymbol(
                name="mac1",
                kind=SymbolKind.MACRO,
                range=_rg(16, 0, 19),
                selection_range=_rg(16, 7, 11),
                scope_path=(),
                parent_label=None,
            ),
            # .export declarators (re-export under aliased names)
            BufferSymbol(
                name="helpers_foo",
                kind=SymbolKind.EXPORT,
                range=_rg(45, 0, 36),
                selection_range=_rg(45, 8, 19),
                scope_path=(),
                parent_label=None,
            ),
            BufferSymbol(
                name="helpers_bar",
                kind=SymbolKind.EXPORT,
                range=_rg(46, 0, 36),
                selection_range=_rg(46, 8, 19),
                scope_path=(),
                parent_label=None,
            ),
        ],
        "main.s": [
            # .import declarators -- the indexer is expected to surface
            # these as separate WorkspaceSymbols alongside the definitions
            # in lib.s / helpers.s.
            BufferSymbol(
                name="lib_export",
                kind=SymbolKind.IMPORT,
                range=_rg(6, 0, 18),
                selection_range=_rg(6, 8, 18),
                scope_path=(),
                parent_label=None,
            ),
            BufferSymbol(
                name="helpers_foo",
                kind=SymbolKind.IMPORT,
                range=_rg(4, 0, 19),
                selection_range=_rg(4, 8, 19),
                scope_path=(),
                parent_label=None,
            ),
            BufferSymbol(
                name="_start",
                kind=SymbolKind.PROC,
                range=_rg(13, 0, 32),
                selection_range=_rg(13, 6, 12),
                scope_path=(),
                parent_label=None,
            ),
        ],
        "zp.inc": [
            # `.globalzp ptr1, ptr2, tmp1` -- represented as IMPORT-ish
            # placeholder; the real Parser may model it differently.
        ],
    }


def _shim_references() -> dict[str, list[tuple[str, Range, tuple[str, ...]]]]:
    return {
        "main.s": [
            ("lib_export", _rg(14, 16, 26), ("_start",)),
            ("helpers_foo", _rg(15, 16, 27), ("_start",)),
            ("lib_const", _rg(16, 17, 26), ("_start",)),
            ("ptr1", _rg(17, 16, 20), ("_start",)),
            ("lib_buffer", _rg(22, 12, 22), ("_start",)),
            ("helpers_bar", _rg(30, 16, 27), ("_start",)),
        ],
        "helpers.s": [
            ("bar", _rg(27, 16, 19), ("helpers", "foo")),
            ("ptr1", _rg(18, 8, 12), ("",)),  # macro body
            # cross-scope cheap-local reference (only resolves inside foo)
            ("@inner", _rg(30, 12, 18), ("helpers", "foo")),
        ],
    }


# --------------------------------------------------------------- fixtures


@pytest.fixture
def shim_parser():
    return BufferShim(_shim_symbols(), _shim_references())


@pytest.fixture
def fresh_repo(tmp_path: Path) -> Path:
    """Copy the synthetic test_repo into a tmp dir so tests can mutate it
    (and so its .ca65-ls/cache/ doesn't pollute the source tree)."""
    dst = tmp_path / "test_repo"
    shutil.copytree(FIXTURE_REPO, dst)
    return dst


# ----------------------------------------------------------------- tests


def test_indexes_synthetic_corpus(fresh_repo: Path, shim_parser):
    idx = WorkspaceIndex(fresh_repo, parser=shim_parser, cache=False)
    idx.reindex()

    # Top-level exports resolve to their defining files.
    lib_export = idx.lookup("lib_export")
    # We expect both the definition (lib.s) and the import declarator (main.s)
    assert any(s.uri.endswith("lib.s") for s in lib_export), (
        f"lib_export should be defined in lib.s; got {[s.uri for s in lib_export]}"
    )
    assert any(s.uri.endswith("main.s") for s in lib_export), (
        "lib_export should appear as an import in main.s"
    )

    # Scoped lookup: helpers::foo
    foos = idx.lookup("foo")
    helpers_foo = [s for s in foos if s.scope_path == ("helpers",)]
    assert len(helpers_foo) == 1
    assert helpers_foo[0].uri.endswith("helpers.s")
    assert helpers_foo[0].kind == SymbolKind.PROC

    # _start lives in main.s
    starts = idx.lookup("_start")
    assert len(starts) == 1
    assert starts[0].uri.endswith("main.s")

    # ptr1 lives in zp.s
    ptr1s = idx.lookup("ptr1")
    assert any(s.uri.endswith("zp.s") for s in ptr1s)


def test_enrichment_from_dbg(fresh_repo: Path, shim_parser):
    assert (fresh_repo / "build" / "test_repo.dbg").is_file()
    idx = WorkspaceIndex(fresh_repo, parser=shim_parser, cache=False)
    idx.reindex()

    [lib_export_def] = [s for s in idx.lookup("lib_export") if s.uri.endswith("lib.s")]
    assert lib_export_def.address is not None, "address should be populated from .dbg"
    assert lib_export_def.address == 0x822
    assert lib_export_def.segment == "CODE"
    assert lib_export_def.size == 5

    # helpers::foo also enriched
    helpers_foo = [s for s in idx.lookup("foo") if s.scope_path == ("helpers",)][0]
    assert helpers_foo.segment == "CODE"
    assert helpers_foo.address == 0x827


def test_no_dbg_fallback(fresh_repo: Path, shim_parser):
    # Rename build/ out of the way so no .dbg can be found.
    (fresh_repo / "build").rename(fresh_repo / "build_renamed")
    idx = WorkspaceIndex(fresh_repo, parser=shim_parser, cache=False)
    idx.reindex()  # should not raise

    lib_export = [s for s in idx.lookup("lib_export") if s.uri.endswith("lib.s")][0]
    assert lib_export.address is None
    assert lib_export.segment is None
    # ...but the symbol itself is still in the index
    assert lib_export.kind == SymbolKind.PROC


def test_cache_skips_unchanged_files(fresh_repo: Path, shim_parser):
    # First, cold reindex
    idx1 = WorkspaceIndex(fresh_repo, parser=shim_parser, cache=True)
    idx1.reindex()
    cold_misses = idx1.stats["cache_misses"]
    cold_hits = idx1.stats["cache_hits"]
    assert cold_misses > 0
    assert cold_hits == 0
    n_files = idx1.stats["files"]

    # Second reindex on a fresh instance: every file should hit cache.
    idx2 = WorkspaceIndex(fresh_repo, parser=shim_parser, cache=True)
    idx2.reindex()
    assert idx2.stats["cache_hits"] == cold_misses, (
        f"warm reindex should hit cache for every previously-parsed file; "
        f"got hits={idx2.stats['cache_hits']} misses={idx2.stats['cache_misses']}"
    )
    assert idx2.stats["cache_misses"] == 0
    assert idx2.stats["files"] == n_files

    # Now touch one file. Sleep just long enough to bump mtime_ns,
    # then write the same bytes back.
    one = fresh_repo / "src" / "main.s"
    text = one.read_text()
    time.sleep(0.01)
    one.write_text(text + "\n; touched\n")

    idx3 = WorkspaceIndex(fresh_repo, parser=shim_parser, cache=True)
    idx3.reindex()
    # All but the touched file should still be cache hits.
    assert idx3.stats["cache_misses"] == 1
    assert idx3.stats["cache_hits"] == cold_misses - 1


def test_cross_file_imports(fresh_repo: Path, shim_parser):
    """`lookup("lib_export")` should surface BOTH the definition (lib.s) and
    the .import declarator (main.s) so a Goto-Definition request can offer
    the user either site."""
    idx = WorkspaceIndex(fresh_repo, parser=shim_parser, cache=False)
    idx.reindex()
    recs = idx.lookup("lib_export")
    uris = sorted(s.uri for s in recs)
    assert any(u.endswith("lib.s") for u in uris)
    assert any(u.endswith("main.s") for u in uris)
    # Kinds distinguish definition vs import declarator.
    kinds = {s.kind for s in recs}
    assert SymbolKind.PROC in kinds
    assert SymbolKind.IMPORT in kinds


def test_search_substring(fresh_repo: Path, shim_parser):
    idx = WorkspaceIndex(fresh_repo, parser=shim_parser, cache=False)
    idx.reindex()
    results = idx.search("helpers")
    names = [s.name for s in results]
    # Both the scope itself and its re-exported aliases should appear
    assert "helpers" in names
    assert "helpers_foo" in names
    assert "helpers_bar" in names


def test_references_query(fresh_repo: Path, shim_parser):
    idx = WorkspaceIndex(fresh_repo, parser=shim_parser, cache=False)
    idx.reindex()

    # lib_export is referenced from inside _start in main.s
    refs = idx.references("lib_export")
    assert len(refs) >= 1
    assert any(r.uri.endswith("main.s") for r in refs)

    # ptr1 is referenced from multiple places
    ptr1_refs = idx.references("ptr1")
    assert len(ptr1_refs) >= 1


def test_references_scope_filtering(fresh_repo: Path, shim_parser):
    idx = WorkspaceIndex(fresh_repo, parser=shim_parser, cache=False)
    idx.reindex()

    # @inner is referenced in helpers::foo scope. Asking for references
    # inside helpers::foo should return the in-scope reference.
    refs_in_foo = idx.references("@inner", scope_path=("helpers", "foo"))
    assert len(refs_in_foo) == 1

    # Asking for references inside helpers::bar should return none
    # (the foo @inner reference is in foo's scope, not bar's).
    refs_in_bar = idx.references("@inner", scope_path=("helpers", "bar"))
    assert len(refs_in_bar) == 0


def test_reindex_file_replaces_old(fresh_repo: Path, shim_parser):
    idx = WorkspaceIndex(fresh_repo, parser=shim_parser, cache=False)
    idx.reindex()
    initial_count = len(idx.lookup("_start"))
    assert initial_count == 1

    # Reindex the same file twice -- should not duplicate symbols
    idx.reindex_file(fresh_repo / "src" / "main.s")
    idx.reindex_file(fresh_repo / "src" / "main.s")
    assert len(idx.lookup("_start")) == 1


def test_stats_populated(fresh_repo: Path, shim_parser):
    idx = WorkspaceIndex(fresh_repo, parser=shim_parser, cache=False)
    idx.reindex()
    s = idx.stats
    assert s["files"] >= 4  # zp.s lib.s helpers.s main.s (+ maybe zp.inc)
    assert s["symbols"] > 0
    assert s["references"] > 0
    assert s["last_reindex_seconds"] >= 0


def test_all_symbols_iterates(fresh_repo: Path, shim_parser):
    idx = WorkspaceIndex(fresh_repo, parser=shim_parser, cache=False)
    idx.reindex()
    everything = list(idx.all_symbols())
    assert len(everything) == idx.stats["symbols"]


def test_gitignore_excludes(tmp_path: Path, shim_parser):
    """A file in a .gitignore'd directory should not be indexed."""
    root = tmp_path / "tiny"
    (root / "src").mkdir(parents=True)
    (root / "obj").mkdir()
    (root / ".gitignore").write_text("obj/\n")
    (root / "src" / "real.s").write_text("; real\n")
    (root / "obj" / "leaked.s").write_text("; should not be indexed\n")

    # Empty shim -- we only care that the walker visits the right files.
    visited: list[str] = []

    def trace_parser(path: Path, text: str):
        visited.append(path.name)
        from ca65_ls.index.workspace import _BufferViewImpl

        return _BufferViewImpl(_symbols=[], _references=[])

    idx = WorkspaceIndex(root, parser=trace_parser, cache=False)
    idx.reindex()
    assert "real.s" in visited
    assert "leaked.s" not in visited


def test_cache_opt_out_does_not_create_dir(fresh_repo: Path, shim_parser):
    idx = WorkspaceIndex(fresh_repo, parser=shim_parser, cache=False)
    idx.reindex()
    assert not (fresh_repo / ".ca65-ls" / "cache").exists()


# ------------------------------------------------- review fixes 2026-09-02
#
# Green regression guards for the index defects found by the 2026-09-02 review
# (synthetic reproducers; the corpus versions live in tests/corpus/).


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _rel(idx: WorkspaceIndex, name: str) -> list[str]:
    from ca65_ls.server import _uri_to_path

    return sorted(
        _uri_to_path(s.uri).relative_to(idx.project_root).as_posix() for s in idx.lookup(name)
    )


def test_nested_shim_tree_is_stored_once(fresh_repo: Path, shim_parser):
    """The shim hands the indexer a nested tree; Document hands it a flat
    list whose records still carry children.  Both shapes must yield each
    record exactly once."""
    idx = WorkspaceIndex(fresh_repo, parser=shim_parser, cache=False)
    idx.reindex()
    assert len([s for s in idx.lookup("foo") if s.scope_path == ("helpers",)]) == 1
    assert len([s for s in idx.lookup("@inner") if s.parent_label == "foo"]) == 1


def test_real_parser_stores_each_nested_symbol_once(tmp_path: Path):
    _write(tmp_path, {"a.s": ".proc p\nloop:\n@inner:\n dex\n bne @inner\n bne loop\n.endproc\n"})
    idx = WorkspaceIndex(tmp_path, cache=False)
    idx.reindex()
    assert len(idx.lookup("p")) == 1
    assert len(idx.lookup("loop")) == 1
    assert len(idx.lookup("@inner")) == 1
    assert idx.stats["symbols"] == 3


def test_walk_fallback_prunes_ignored_ancestors_and_tool_dirs(tmp_path: Path):
    """No git here, so the os.walk fallback runs: a `.claude/*` pattern must
    exclude files any depth below, and `.claude/` / `.serena/` are excluded
    even without a .gitignore."""
    _write(
        tmp_path,
        {
            "src/a.s": ".proc foo\n rts\n.endproc\n",
            "vendor/x/y.s": ".proc foo\n rts\n.endproc\n",
            ".claude/worktrees/agent-1/src/a.s": ".proc foo\n rts\n.endproc\n",
            ".serena/memories/a.s": ".proc foo\n rts\n.endproc\n",
            ".gitignore": "vendor/*\n",
        },
    )
    idx = WorkspaceIndex(tmp_path, cache=False)
    idx.reindex()
    assert _rel(idx, "foo") == ["src/a.s"]
    assert idx.stats["files"] == 1


def _git(root: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def git_project(tmp_path: Path) -> Path:
    import shutil

    if shutil.which("git") is None:
        pytest.skip("git not installed")
    _git(tmp_path, "init", "-q")
    return tmp_path


def test_git_enumeration_honours_every_exclude_source(git_project: Path):
    """Inside a checkout the indexer asks git, so nested checkouts (agent
    worktrees), `.git/info/exclude` and the configured excludes file are all
    honoured -- none of which the pathspec walk could see."""
    root = git_project
    _write(
        root,
        {
            "src/a.s": ".proc foo\n rts\n.endproc\n",
            "src/untracked.s": ".proc bar\n rts\n.endproc\n",
            "gen/out.s": ".proc foo\n rts\n.endproc\n",
            "scratch/tmp.s": ".proc foo\n rts\n.endproc\n",
            ".claude/worktrees/agent-1/src/a.s": ".proc foo\n rts\n.endproc\n",
            "excludes.txt": "scratch/\n",
        },
    )
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "exclude").write_text("gen/\n")
    _git(root, "config", "core.excludesFile", str(root / "excludes.txt"))
    _git(root / ".claude" / "worktrees" / "agent-1", "init", "-q")  # a nested checkout
    _git(root, "add", "src/a.s")

    idx = WorkspaceIndex(root, cache=False)
    idx.reindex()
    assert _rel(idx, "foo") == ["src/a.s"]
    assert _rel(idx, "bar") == ["src/untracked.s"], "untracked files are still indexed"


def test_git_enumeration_is_sorted_and_skips_deleted_tracked_files(git_project: Path):
    root = git_project
    _write(root, {"src/b.s": "b: rts\n", "src/a.s": "a_lbl: rts\n", "src/gone.s": "gone: rts\n"})
    _git(root, "add", "src")
    (root / "src" / "gone.s").unlink()
    from ca65_ls.index.workspace import _iter_source_files

    files = [p.relative_to(root).as_posix() for p in _iter_source_files(root)]
    assert files == ["src/a.s", "src/b.s"]


def test_refresh_tracks_created_modified_and_deleted_files(tmp_path: Path):
    _write(tmp_path, {"a.s": ".proc foo\n rts\n.endproc\n"})
    idx = WorkspaceIndex(tmp_path, cache=False)
    idx.reindex()
    assert idx.refresh() is False, "nothing changed"

    _write(tmp_path, {"b.s": ".proc bar\n jsr foo\n.endproc\n"})
    assert idx.refresh() is True
    assert _rel(idx, "bar") == ["b.s"]
    assert [r.uri for r in idx.references("foo")] == [(tmp_path / "b.s").resolve().as_uri()]
    assert idx.stats["files"] == 2

    (tmp_path / "a.s").write_text(".proc foo\n rts\n.endproc\n.proc baz\n rts\n.endproc\n")
    assert idx.refresh() is True
    assert _rel(idx, "baz") == ["a.s"]

    (tmp_path / "b.s").unlink()
    assert idx.refresh() is True
    assert idx.lookup("bar") == []
    assert idx.references("foo") == []
    assert idx.stats["files"] == 1
    assert idx.stats["symbols"] == 2


def test_refresh_max_age_throttles_the_scan(tmp_path: Path):
    _write(tmp_path, {"a.s": ".proc foo\n rts\n.endproc\n"})
    idx = WorkspaceIndex(tmp_path, cache=False)
    idx.reindex()
    _write(tmp_path, {"b.s": ".proc bar\n rts\n.endproc\n"})
    assert idx.refresh(max_age=60.0) is False
    assert idx.lookup("bar") == []
    assert idx.refresh(max_age=0.0) is True
    assert _rel(idx, "bar") == ["b.s"]


def test_reindex_file_from_buffer_text_is_authoritative_until_disk_reindex(tmp_path: Path):
    _write(tmp_path, {"a.s": ".proc foo\n rts\n.endproc\n"})
    idx = WorkspaceIndex(tmp_path, cache=False)
    idx.reindex()
    path = tmp_path / "a.s"

    idx.reindex_file(path, text=".proc foo\n rts\n.endproc\n.proc from_buffer\n rts\n.endproc\n")
    assert _rel(idx, "from_buffer") == ["a.s"]
    # The disk file is unchanged; a refresh must not clobber the buffer's view.
    assert idx.refresh() is False
    assert _rel(idx, "from_buffer") == ["a.s"]
    # Editing the file on disk while a buffer is open does not either.
    path.write_text(".proc foo\n rts\n.endproc\n; touched\n")
    idx.refresh()
    assert _rel(idx, "from_buffer") == ["a.s"]
    # Reindexing from disk (didClose) makes the file authoritative again.
    idx.reindex_file(path)
    assert idx.lookup("from_buffer") == []
    assert _rel(idx, "foo") == ["a.s"]


def test_reindex_file_from_buffer_text_never_writes_the_cache(tmp_path: Path):
    _write(tmp_path, {"a.s": ".proc foo\n rts\n.endproc\n"})
    idx = WorkspaceIndex(tmp_path, cache=True)
    idx.reindex()
    cache_files = sorted(p.name for p in (tmp_path / ".ca65-ls" / "cache").glob("*.pkl"))
    (tmp_path / "b.s").write_text("b: rts\n")
    idx.reindex_file(tmp_path / "b.s", text=".proc from_buffer\n rts\n.endproc\n")
    assert sorted(p.name for p in (tmp_path / ".ca65-ls" / "cache").glob("*.pkl")) == cache_files
    # And a later disk reindex does not pick a stale entry up either.
    idx.reindex_file(tmp_path / "b.s")
    assert _rel(idx, "b") == ["b.s"]


def test_reindex_file_of_a_deleted_path_drops_symbols_and_references(tmp_path: Path):
    _write(tmp_path, {"a.s": ".proc foo\n rts\n.endproc\n", "b.s": "bar:\n jsr foo\n"})
    idx = WorkspaceIndex(tmp_path, cache=False)
    idx.reindex()
    assert idx.references("foo")
    (tmp_path / "b.s").unlink()
    idx.reindex_file(tmp_path / "b.s")
    assert idx.lookup("bar") == []
    assert idx.references("foo") == []
    assert idx.stats["files"] == 1


def test_refresh_reloads_dbg_when_it_changes(fresh_repo: Path):
    idx = WorkspaceIndex(fresh_repo, cache=False)
    idx.reindex()
    dbg = fresh_repo / "build" / "test_repo.dbg"
    [lib_export] = [s for s in idx.lookup("lib_export") if s.uri.endswith("lib.s")]
    assert lib_export.address == 0x822

    saved = dbg.read_bytes()
    dbg.unlink()
    assert idx.refresh() is True
    [lib_export] = [s for s in idx.lookup("lib_export") if s.uri.endswith("lib.s")]
    assert lib_export.address is None

    dbg.write_bytes(saved)
    assert idx.refresh() is True
    [lib_export] = [s for s in idx.lookup("lib_export") if s.uri.endswith("lib.s")]
    assert lib_export.address == 0x822
    # Re-enrichment keeps the name index and the per-file lists consistent.
    assert any(s is lib_export for s in idx.all_symbols())


def test_exports_of_reads_every_export_directive(tmp_path: Path):
    _write(
        tmp_path,
        {
            "a.s": (
                ".export foo, bar\n"
                ".exportzp zp1 ; comment\n"
                ".GLOBAL g1\n"
                ".export alias := $1234\n"
                ".proc foo\n rts\n.endproc\n"
            )
        },
    )
    idx = WorkspaceIndex(tmp_path, cache=False)
    idx.reindex()
    uri = (tmp_path / "a.s").resolve().as_uri()
    assert idx.exports_of(uri) == {"foo", "bar", "zp1", "g1", "alias"}
    assert idx.exports_of("file:///nowhere.s") == frozenset()


def test_exports_survive_the_cache_round_trip(tmp_path: Path):
    _write(tmp_path, {"a.s": ".export foo\n.proc foo\n rts\n.endproc\n"})
    WorkspaceIndex(tmp_path, cache=True).reindex()
    idx = WorkspaceIndex(tmp_path, cache=True)
    idx.reindex()
    assert idx.stats["cache_hits"] == 1
    assert idx.exports_of((tmp_path / "a.s").resolve().as_uri()) == {"foo"}


def test_changed_on_disk(tmp_path: Path):
    _write(tmp_path, {"a.s": ".proc foo\n rts\n.endproc\n"})
    idx = WorkspaceIndex(tmp_path, cache=False)
    idx.reindex()
    a = tmp_path / "a.s"
    assert idx.changed_on_disk(a) is False
    assert idx.changed_on_disk(tmp_path / "unknown.s") is True
    a.write_text(".proc foo\n rts\n.endproc\n; edited\n")
    assert idx.changed_on_disk(a) is True
    idx.reindex_file(a, text="from_buffer: rts\n")
    assert idx.changed_on_disk(a) is False, "buffer-backed files are never stale"
    idx.reindex_file(a)
    assert idx.changed_on_disk(a) is False
