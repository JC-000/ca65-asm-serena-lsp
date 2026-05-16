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
                name="ptr1", kind=SymbolKind.LABEL,
                range=_rg(6, 0, 4), selection_range=_rg(6, 0, 4),
                scope_path=(), parent_label=None,
            ),
            BufferSymbol(
                name="ptr2", kind=SymbolKind.LABEL,
                range=_rg(7, 0, 4), selection_range=_rg(7, 0, 4),
                scope_path=(), parent_label=None,
            ),
            BufferSymbol(
                name="tmp1", kind=SymbolKind.LABEL,
                range=_rg(8, 0, 4), selection_range=_rg(8, 0, 4),
                scope_path=(), parent_label=None,
            ),
        ],
        "lib.s": [
            BufferSymbol(
                name="lib_export", kind=SymbolKind.PROC,
                range=_rg(13, 0, 18), selection_range=_rg(13, 6, 16),
                scope_path=(), parent_label=None,
            ),
            BufferSymbol(
                name="lib_buffer", kind=SymbolKind.LABEL,
                range=_rg(9, 0, 10), selection_range=_rg(9, 0, 10),
                scope_path=(), parent_label=None,
            ),
            BufferSymbol(
                name="lib_const", kind=SymbolKind.CONSTANT,
                range=_rg(6, 8, 17), selection_range=_rg(6, 8, 17),
                scope_path=(), parent_label=None,
            ),
        ],
        "helpers.s": [
            BufferSymbol(
                name="helpers", kind=SymbolKind.SCOPE,
                range=_rg(23, 0, 41), selection_range=_rg(23, 7, 14),
                scope_path=(), parent_label=None,
                children=(
                    BufferSymbol(
                        name="foo", kind=SymbolKind.PROC,
                        range=_rg(25, 0, 33), selection_range=_rg(25, 6, 9),
                        scope_path=("helpers",), parent_label=None,
                        children=(
                            BufferSymbol(
                                name="@inner", kind=SymbolKind.CHEAP_LOCAL,
                                range=_rg(28, 0, 6), selection_range=_rg(28, 0, 6),
                                scope_path=("helpers", "foo"),
                                parent_label="foo",
                            ),
                        ),
                    ),
                    BufferSymbol(
                        name="bar", kind=SymbolKind.PROC,
                        range=_rg(34, 0, 40), selection_range=_rg(34, 6, 9),
                        scope_path=("helpers",), parent_label=None,
                        children=(
                            BufferSymbol(
                                name="@inner", kind=SymbolKind.CHEAP_LOCAL,
                                range=_rg(35, 0, 6), selection_range=_rg(35, 0, 6),
                                scope_path=("helpers", "bar"),
                                parent_label="bar",
                            ),
                        ),
                    ),
                ),
            ),
            BufferSymbol(
                name="S", kind=SymbolKind.STRUCT,
                range=_rg(11, 0, 14), selection_range=_rg(11, 8, 9),
                scope_path=(), parent_label=None,
                children=(
                    BufferSymbol(
                        name="flags", kind=SymbolKind.FIELD,
                        range=_rg(12, 8, 13), selection_range=_rg(12, 8, 13),
                        scope_path=("S",), parent_label=None,
                    ),
                    BufferSymbol(
                        name="count", kind=SymbolKind.FIELD,
                        range=_rg(13, 8, 13), selection_range=_rg(13, 8, 13),
                        scope_path=("S",), parent_label=None,
                    ),
                ),
            ),
            BufferSymbol(
                name="mac1", kind=SymbolKind.MACRO,
                range=_rg(16, 0, 19), selection_range=_rg(16, 7, 11),
                scope_path=(), parent_label=None,
            ),
            # .export declarators (re-export under aliased names)
            BufferSymbol(
                name="helpers_foo", kind=SymbolKind.EXPORT,
                range=_rg(45, 0, 36), selection_range=_rg(45, 8, 19),
                scope_path=(), parent_label=None,
            ),
            BufferSymbol(
                name="helpers_bar", kind=SymbolKind.EXPORT,
                range=_rg(46, 0, 36), selection_range=_rg(46, 8, 19),
                scope_path=(), parent_label=None,
            ),
        ],
        "main.s": [
            # .import declarators -- the indexer is expected to surface
            # these as separate WorkspaceSymbols alongside the definitions
            # in lib.s / helpers.s.
            BufferSymbol(
                name="lib_export", kind=SymbolKind.IMPORT,
                range=_rg(6, 0, 18), selection_range=_rg(6, 8, 18),
                scope_path=(), parent_label=None,
            ),
            BufferSymbol(
                name="helpers_foo", kind=SymbolKind.IMPORT,
                range=_rg(4, 0, 19), selection_range=_rg(4, 8, 19),
                scope_path=(), parent_label=None,
            ),
            BufferSymbol(
                name="_start", kind=SymbolKind.PROC,
                range=_rg(13, 0, 32), selection_range=_rg(13, 6, 12),
                scope_path=(), parent_label=None,
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
