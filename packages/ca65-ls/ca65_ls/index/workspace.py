"""
workspace -- the project-wide CA65 symbol index.

Builds two indexes from all `*.s|*.asm|*.inc` files under a project root:

    symbols_by_name      {name: [WorkspaceSymbol, ...]}
    references_by_name   {name: [SymbolReference, ...]}

Pipeline per file:

    raw text  ->  Document (Parser engineer's `ca65_ls.buffer.document`)
              ->  flat_symbols() / references_in()
              ->  promote BufferSymbol -> WorkspaceSymbol (attach URI)
              ->  enrich with .dbg (address, segment, size) if build/*.dbg exists
              ->  cache pickled per-file result under <root>/.ca65-ls/cache/

The Parser layer is still being built in parallel. We import its `Document`
lazily and tolerate `ImportError`; tests and any in-progress consumer can
inject a `BufferShim` callable that returns a `BufferView` describing what
the real parser will eventually produce.

The cache key is a sha256 over (absolute path bytes, mtime_ns, size). On
second indexing, files whose key is unchanged are loaded from disk; this
yields the ~10x cold-vs-warm ratio the M2 budget asks for.
"""

from __future__ import annotations

import hashlib
import os
import pickle
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ca65_ls.types import (
    BufferSymbol,
    Position,
    Range,
    SymbolReference,
    WorkspaceSymbol,
)

# Parser layer is being built in parallel. Tolerate its absence; tests and
# external consumers can supply a `parser` callable to WorkspaceIndex.
try:  # pragma: no cover - exercised in both states across the team
    from ca65_ls.buffer.document import Document  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    Document = None  # type: ignore[assignment]

try:
    import pathspec  # noqa: F401

    _HAS_PATHSPEC = True
except ImportError:  # pragma: no cover
    _HAS_PATHSPEC = False


# --------------------------------------------------------------------- shapes


SOURCE_SUFFIXES = (".s", ".asm", ".inc")
# Directories never indexed, gitignore or not.  Matched against every path
# component, so `foo/build/x.s` is skipped as well as `build/x.s`.  `.claude/`
# holds worktree copies of whole repos left by agents; `.serena/` is Serena's
# own state.
_DEFAULT_IGNORES = (
    "build/",
    "obj/",
    ".venv/",
    "__pycache__/",
    ".git/",
    ".ca65-ls/",
    ".claude/",
    ".serena/",
)
CACHE_FORMAT_VERSION = 6  # bump if the on-disk cache shape *or* the symbols it stores change
# v6 (2026-09-02): review fixes.  Nested symbols were stored 2-3x per file (the
# cache pickled the duplicated list), the per-file entry now carries the names
# the file `.export`s (used to rank go-to-definition candidates), and the
# parser clips label bodies to `.endproc` / re-adds `.export` declarators as
# references, so cached ranges and reference lists are all stale.
# v5 (2026-08-28): `.export`/`.exportzp`/`.global` declarators whose target is
# also defined in the same file are no longer emitted as separate symbols (they
# duplicated the definition -- ~25% of all symbols on c64-https).  The cache key
# is only (path, mtime_ns, size), so caches written before this change would keep
# serving the phantom symbols indefinitely; invalidate them.
# v4 (2026-05-19): references are now collected via a single tree walk per
# file (Document.all_references) rather than one walk per name; the cached
# reference list is strictly more comprehensive (includes refs to names not
# defined in the same file).  Older v3 caches are correct but incomplete;
# invalidate so cold reindex repopulates with the richer data.
# v3 (2026-05-19): cheap-local names now retain the leading "@" prefix.
# v2 (initial): legacy layout.


class BufferView(Protocol):
    """Minimum interface the indexer needs from a parsed document.

    The Parser engineer's `Document` class satisfies this Protocol.
    Two methods are required:

      - flat_symbols() -> list[BufferSymbol]
            All symbols declared in this document, in declaration order
            (children flattened — the indexer re-walks via _flatten anyway,
            so a non-flat tree is also fine).

      - all_references() -> list[SymbolReference] | list[tuple[name, Range, scope_path]]
            All use-sites in this document. Two shapes are accepted to
            keep the indexer decoupled from the exact return type the
            Parser layer settles on:
              (a) a SymbolReference list (production path — the real
                  `Document.references_in(name)` method gathers these
                  internally, but the workspace indexer wants ALL refs
                  in one shot so it pulls from a `_walk_references`-style
                  aggregator).
              (b) a list of (name, Range, scope_path) tuples (test path —
                  hand-built by the shim).
    """

    def flat_symbols(self) -> list[BufferSymbol]: ...
    def all_references(self) -> list:  # SymbolReference | tuple
        ...


@dataclass
class _BufferViewImpl:
    """Concrete `BufferView` used by tests and by the production code path
    once the Parser engineer's Document delivers data via these methods."""

    _symbols: list[BufferSymbol]
    _references: list  # list[SymbolReference] OR list[(name, Range, scope_path)]

    def flat_symbols(self) -> list[BufferSymbol]:
        return list(self._symbols)

    def all_references(self) -> list:
        return list(self._references)


# A `parser` callable: (absolute_path, text) -> BufferView. Tests inject a
# BufferShim; production passes None and we fall back to Document().
Parser = Callable[[Path, str], BufferView | None]


# --------------------------------------------------------------------- helpers


def _default_parser(path: Path, text: str) -> BufferView | None:
    """Real-Document path. Returns None if the Parser layer hasn't landed yet.

    Uses ``Document.all_references()`` for a single tree walk per file
    rather than one walk per name (which was O(symbols × file_size) and
    dominated cold-reindex time on real codebases).
    """
    if Document is None:
        return None
    doc = Document(uri=path.as_uri(), text=text)  # type: ignore[call-arg]
    symbols = list(doc.flat_symbols())  # type: ignore[attr-defined]
    try:
        refs = list(doc.all_references())  # type: ignore[attr-defined]
    except Exception:
        refs = []
    return _BufferViewImpl(_symbols=symbols, _references=refs)


def _file_cache_key(path: Path) -> str:
    """SHA-256 over absolute path + mtime_ns + size. Cheap, deterministic,
    and changes the instant the file is touched (no content read needed)."""
    st = path.stat()
    h = hashlib.sha256()
    h.update(str(path.resolve()).encode("utf-8"))
    h.update(b"\x00")
    h.update(str(st.st_mtime_ns).encode("ascii"))
    h.update(b"\x00")
    h.update(str(st.st_size).encode("ascii"))
    return h.hexdigest()


def _flatten(symbols: list[BufferSymbol]) -> Iterator[BufferSymbol]:
    """Walk nested .children trees, yielding every BufferSymbol exactly once.

    Accepts both shapes a `BufferView` may hand us: a nested tree (the test
    shim) and an already flat list whose records still carry their
    `.children` (`Document.flat_symbols()`).  In the second shape every nested
    symbol is reachable twice -- as a list element and as its parent's child --
    so records are deduplicated by identity.  Before this, every proc-local
    label and cheap local was stored two or three times (c64-x25519: 197 of
    885 records).
    """
    seen: set[int] = set()

    def walk(items: list[BufferSymbol]) -> Iterator[BufferSymbol]:
        for sym in items:
            if id(sym) in seen:
                continue
            seen.add(id(sym))
            yield sym
            if sym.children:
                yield from walk(list(sym.children))

    yield from walk(symbols)


def _load_gitignore(project_root: Path):  # -> pathspec.PathSpec | None
    """Read .gitignore at project root into a PathSpec object, or None."""
    gi = project_root / ".gitignore"
    if not gi.is_file() or not _HAS_PATHSPEC:
        return None
    try:
        with gi.open() as f:
            # Prefer the modern "gitignore" pattern factory when available
            # (pathspec >= 0.10); fall back to the legacy name for older releases.
            try:
                return pathspec.PathSpec.from_lines("gitignore", f)
            except Exception:
                f.seek(0)
                return pathspec.PathSpec.from_lines("gitwildmatch", f)
    except Exception:  # pragma: no cover - corrupt .gitignore
        return None


def _is_default_ignored(rel_posix: str) -> bool:
    """True if any path component is one of our built-in excluded directories."""
    for pat in _DEFAULT_IGNORES:
        if rel_posix.startswith(pat) or f"/{pat}" in f"/{rel_posix}":
            return True
    return False


def _is_ignored(rel_posix: str, spec) -> bool:
    """Combine .gitignore (if any) with our default build-ish exclusions.

    `rel_posix` is a path relative to the project root; a trailing "/" marks
    a directory.  Every ancestor directory is tested against the gitignore
    spec too, because pathspec answers only for the exact path it is given:
    `.claude/*` matches `.claude/worktrees` but not
    `.claude/worktrees/agent-1/src/a.s`, whereas git ignores everything below
    an ignored directory.
    """
    if _is_default_ignored(rel_posix):
        return True
    if spec is None:
        return False
    if spec.match_file(rel_posix):
        return True
    parts = rel_posix.rstrip("/").split("/")
    for depth in range(1, len(parts)):
        ancestor = "/".join(parts[:depth])
        if spec.match_file(ancestor) or spec.match_file(ancestor + "/"):
            return True
    return False


def _git_submodules(project_root: Path) -> list[str]:
    """Relative paths of this level's submodules (index mode 160000).

    `git ls-files -co` reports a submodule as a single gitlink entry and never
    descends, and `--recurse-submodules` is incompatible with `-o`, so each
    one has to be enumerated in its own right.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "ls-files", "-s", "-z"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out: list[str] = []
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        # "<mode> <sha> <stage>\t<path>"
        meta, _, rel = os.fsdecode(raw).partition("\t")
        if rel and meta.startswith("160000"):
            out.append(rel)
    return out


def _git_source_files(project_root: Path, _seen: set[str] | None = None) -> list[Path] | None:
    """Enumerate candidate sources with git, or None if git cannot answer.

    `git ls-files -co --exclude-standard` lists tracked plus untracked files
    the way git itself sees them: `.gitignore` at every level, `.git/info/
    exclude` and the user's global excludes file are all honoured, and nested
    checkouts (agent worktrees under `.claude/worktrees/`) are not descended
    into.  Paths come back relative to `project_root` even when the root is a
    subdirectory of the work tree.

    Submodules are listed separately and recursed into, because git reports
    each as one gitlink entry.  Without that, c64-https lost its `ip65` and
    `libs/` submodules -- 247 files down to 67.  An uninitialised submodule
    (empty directory, no `.git`) simply contributes nothing.
    """
    seen = set() if _seen is None else _seen
    try:
        key = str(project_root.resolve())
    except OSError:
        key = str(project_root)
    if key in seen:
        return []
    seen.add(key)

    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out: list[Path] = []
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        rel = os.fsdecode(raw)
        if not rel.lower().endswith(SOURCE_SUFFIXES):
            continue
        if _is_default_ignored(rel):
            continue
        path = project_root / rel
        # Tracked files can be deleted from the work tree without being
        # removed from the index; git still lists them.
        if not path.is_file():
            continue
        out.append(path)

    for rel in _git_submodules(project_root):
        if _is_default_ignored(rel + "/"):
            continue
        sub_root = project_root / rel
        if not (sub_root / ".git").exists():
            continue  # not initialised
        sub_files = _git_source_files(sub_root, seen)
        if sub_files:
            out.extend(sub_files)
    return sorted(out)


def _walk_source_files(project_root: Path) -> list[Path]:
    """Filesystem walk used when git is unavailable or the root is not a
    checkout.  Ignored directories are pruned, so a worktree copy under
    `.claude/` is never even entered.  Sorted for determinism.

    Directory symlinks are deliberately not followed (and `git ls-files`
    lists a symlink as one entry, so git mode does not descend either).  A
    link into the project adds nothing the walk does not already reach; a
    link out of it (c64-wireguard's `ip65 -> ../c64-https/ip65`) would index
    another project's files under URIs the server resolves to paths outside
    this workspace root, breaking `index_for` and every same-file check.
    """
    spec = _load_gitignore(project_root)
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        rel_dir = Path(dirpath).relative_to(project_root).as_posix()
        keep: list[str] = []
        for d in sorted(dirnames):
            rel = f"{rel_dir}/{d}" if rel_dir != "." else d
            if not _is_ignored(rel + "/", spec):
                keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            if not f.lower().endswith(SOURCE_SUFFIXES):
                continue
            rel = f"{rel_dir}/{f}" if rel_dir != "." else f
            if _is_ignored(rel, spec):
                continue
            path = Path(dirpath) / f
            if path.is_file():
                out.append(path)
    return sorted(out)


def _iter_source_files(project_root: Path) -> Iterator[Path]:
    """Yield candidate `.s/.asm/.inc` files, in sorted order, respecting
    .gitignore + defaults.  Uses git when the root is inside a work tree."""
    files = _git_source_files(project_root)
    if files is None:
        files = _walk_source_files(project_root)
    yield from files


_EXPORT_LINE_RE = re.compile(r"^\s*\.(?:export|exportzp|global|globalzp)\s+(.*?)\s*(?:;.*)?$", re.I)
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _exported_names(text: str) -> frozenset[str]:
    """Names a file makes visible to other translation units via
    `.export` / `.exportzp` / `.global` / `.globalzp`.

    The parser suppresses `.export foo` declarators when `foo` is defined in
    the same file (they duplicated the definition), so the symbol list cannot
    tell us which file exports a name.  A line-level scan is enough here: the
    result only ranks go-to-definition candidates, it never creates symbols.
    """
    names: set[str] = set()
    for line in text.splitlines():
        m = _EXPORT_LINE_RE.match(line)
        if not m:
            continue
        for item in m.group(1).split(","):
            ident = _IDENT_RE.match(item.strip())
            if ident:
                names.add(ident.group(0))
    return frozenset(names)


def _scope_matches(reference_scope: tuple[str, ...], symbol_scope: tuple[str, ...]) -> bool:
    """A reference resolves to a symbol if the reference's scope path is
    inside or equal to the symbol's scope path (prefix match from the
    outside in). This is a simple v1 disambiguator; the LSP server can
    further narrow when a precise scope hint is given."""
    if not symbol_scope:
        return True
    return reference_scope[: len(symbol_scope)] == symbol_scope


def _position_in_range(pos: Position, r: Range) -> bool:
    """Half-open LSP-style containment: pos is in r iff
    r.start <= pos < r.end (lexicographic by (line, character))."""
    if (pos.line, pos.character) < (r.start.line, r.start.character):
        return False
    return not (pos.line, pos.character) >= (r.end.line, r.end.character)


# ---------------------------------------------------------- dbg-enrichment


def _find_dbg_files(project_root: Path) -> list[Path]:
    """Look for `build/*.dbg` and `obj/*.dbg` under the project root."""
    out: list[Path] = []
    for sub in ("build", "obj"):
        d = project_root / sub
        if d.is_dir():
            out.extend(sorted(d.glob("*.dbg")))
    return out


@dataclass
class _DbgLookup:
    """Wraps one or more parsed .dbg files into a name+scope_path -> record table."""

    by_key: dict[tuple[str, tuple[str, ...]], object] = field(default_factory=dict)
    by_name: dict[str, list[object]] = field(default_factory=dict)

    def add_index(self, idx) -> None:
        for name, recs in idx.symbols_by_name.items():
            for r in recs:
                if r.kind == "import":
                    continue  # imports don't have addresses
                key = (name, tuple(r.scope_path))
                self.by_key.setdefault(key, r)
                self.by_name.setdefault(name, []).append(r)

    def lookup(self, name: str, scope_path: tuple[str, ...]):
        # 1) try exact (name, scope_path)
        r = self.by_key.get((name, scope_path))
        if r is not None:
            return r
        # 2) try (name, ()): scope-less re-export case (e.g. `.export helpers_foo := helpers::foo`)
        r = self.by_key.get((name, ()))
        if r is not None:
            return r
        # 3) fall back to any record with this name (best-effort enrichment)
        recs = self.by_name.get(name, [])
        if recs:
            return recs[0]
        return None


# ------------------------------------------------------------- WorkspaceIndex


@dataclass
class _CachedFileEntry:
    """What we pickle per file: symbols, references, and the cache key."""

    key: str
    symbols: list[BufferSymbol]
    references: list[tuple[str, Range, tuple[str, ...]]]
    format_version: int = CACHE_FORMAT_VERSION
    exports: frozenset[str] = frozenset()


#: (mtime_ns, size) of a file as last ingested; None when the entry came from
#: an editor buffer rather than from disk.
_Signature = tuple[int, int] | None


def _signature(path: Path) -> tuple[int, int]:
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)


class WorkspaceIndex:
    """Project-wide CA65 symbol and reference index.

    Lifetimes:
      - Construct once per project_root.
      - `reindex()` builds (or rebuilds) the full index.
      - `reindex_file(path)` re-runs one file (e.g. on save).
      - `lookup`/`references`/`search`/`all_symbols` answer queries.

    Caching:
      - On disk under `<project_root>/.ca65-ls/cache/<sha-prefix>.pkl`.
      - Key: sha256(abspath, mtime_ns, size). Mtime catches almost all
        meaningful edits; size makes the rare same-mtime-different-size
        write (e.g. a truncating sed) reload cleanly.
      - Opt-out via `WorkspaceIndex(project_root, cache=False)`.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        cache: bool = True,
        parser: Parser | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self._cache_enabled = bool(cache)
        self._parser: Parser = parser or _default_parser

        self._symbols_by_name: dict[str, list[WorkspaceSymbol]] = {}
        self._references_by_name: dict[str, list[SymbolReference]] = {}
        # Track which file produced which symbols/refs so reindex_file() can
        # surgically remove a file's entries before re-adding.
        self._symbols_by_uri: dict[str, list[WorkspaceSymbol]] = {}
        self._references_by_uri: dict[str, list[SymbolReference]] = {}
        # Names each file `.export`s, for ranking definition candidates.
        self._exports_by_uri: dict[str, frozenset[str]] = {}
        # On-disk (mtime_ns, size) each file was ingested at, or None when the
        # entry reflects an editor buffer and must not be refreshed from disk.
        self._signature_by_uri: dict[str, _Signature] = {}
        self._dbg_signature: tuple[tuple[str, int, int], ...] = ()
        self._last_refresh: float = 0.0

        self._stats: dict[str, int | float] = {
            "files": 0,
            "symbols": 0,
            "references": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "dbg_files": 0,
            "last_reindex_seconds": 0.0,
        }

        self._dbg = _DbgLookup()

    # ------------------------------------------------------------- public API

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    def reindex(self) -> None:
        """Full project scan. Discovers all source files, parses each, and
        rebuilds the in-memory indexes. Existing on-disk cache is consulted
        for files whose key is unchanged."""
        t0 = time.perf_counter()
        self._symbols_by_name.clear()
        self._references_by_name.clear()
        self._symbols_by_uri.clear()
        self._references_by_uri.clear()
        self._exports_by_uri.clear()
        self._signature_by_uri.clear()
        self._stats["cache_hits"] = 0
        self._stats["cache_misses"] = 0

        # Load .dbg enrichers first so per-file ingest can decorate
        # WorkspaceSymbol records inline.
        self._reload_dbg()

        for path in _iter_source_files(self.project_root):
            self._ingest_file(path)

        self._recount()
        self._last_refresh = time.monotonic()
        self._stats["last_reindex_seconds"] = time.perf_counter() - t0

    def reindex_file(self, path: Path, text: str | None = None) -> None:
        """Re-parse a single file.  Cheap path for the didOpen / didChange /
        didSave / didChangeWatchedFiles handlers.

        :param text: the editor's buffer contents.  When given, the file is
            parsed from this text rather than from disk, nothing is written
            to the cache, and `refresh()` leaves the entry alone until the
            file is reindexed from disk again (the didClose handler does
            that).  When omitted and the file no longer exists, its symbols
            and references are simply dropped.
        """
        path = Path(path).resolve()
        uri = path.as_uri()
        self._drop_file(uri)
        if text is not None:
            self._ingest_text(path, text)
        elif path.is_file():
            self._ingest_file(path)
        self._recount()

    def refresh(self, max_age: float | None = None) -> bool:
        """Bring the index in line with the files on disk without a full
        rebuild: pick up created files, reparse modified ones, drop deleted
        ones, and reload the `.dbg` enrichers if they changed.

        Files currently backed by an editor buffer (see `reindex_file(text=)`)
        are left untouched.  Returns True if anything changed.

        :param max_age: skip the scan entirely if the last one ran fewer than
            this many seconds ago.  Lets request handlers call this freely.
        """
        now = time.monotonic()
        if max_age is not None and now - self._last_refresh < max_age:
            return False
        self._last_refresh = now
        changed = False

        current: dict[str, Path] = {p.as_uri(): p for p in _iter_source_files(self.project_root)}
        for uri in list(self._signature_by_uri):
            if uri in current or self._signature_by_uri[uri] is None:
                continue
            self._drop_file(uri)
            changed = True
        for uri, path in current.items():
            old = self._signature_by_uri.get(uri, ())
            if old is None:
                continue  # buffer-backed
            try:
                sig = _signature(path)
            except OSError:
                continue
            if old == sig:
                continue
            self._drop_file(uri)
            self._ingest_file(path)
            changed = True

        if self._dbg_signature != self._current_dbg_signature():
            self.reload_dbg()
            changed = True

        if changed:
            self._recount()
        return changed

    def reload_dbg(self) -> None:
        """Re-read the `.dbg` files and re-enrich every stored symbol with
        the new addresses/segments/sizes.  Symbols themselves are unchanged."""
        self._reload_dbg()
        for uri, syms in self._symbols_by_uri.items():
            fresh = [self._promote(s, uri) for s in syms]
            self._symbols_by_uri[uri] = fresh
        self._symbols_by_name.clear()
        for syms in self._symbols_by_uri.values():
            for ws in syms:
                self._symbols_by_name.setdefault(ws.name, []).append(ws)

    def changed_on_disk(self, path: Path) -> bool:
        """True if `path` is unknown to the index or its on-disk stat differs
        from the one it was ingested at.  A file the editor has just opened
        with new contents is a strong hint that the project changed on disk
        behind our back (an agent wrote several files), so callers use it to
        force a `refresh()` past the throttle.  Buffer-backed files are never
        stale."""
        uri = Path(path).resolve().as_uri()
        if uri not in self._signature_by_uri:
            return True
        old = self._signature_by_uri[uri]
        if old is None:
            return False
        try:
            return _signature(Path(path).resolve()) != old
        except OSError:
            return True

    def exports_of(self, uri: str) -> frozenset[str]:
        """Names the file at `uri` exports (`.export`/`.exportzp`/`.global`)."""
        return self._exports_by_uri.get(uri, frozenset())

    def lookup(self, name: str) -> list[WorkspaceSymbol]:
        """All definition-site WorkspaceSymbols with this exact name."""
        return list(self._symbols_by_name.get(name, []))

    def references(
        self,
        name: str,
        scope_path: tuple[str, ...] | None = None,
        body_filter: tuple[str, Range] | None = None,
    ) -> list[SymbolReference]:
        """All references to ``name``.

        Two filters, AND'd together when both are given:

        :param scope_path: narrow to references whose own ``scope_path`` is
            inside or equal to this scope (used for ``.proc`` / ``.scope``
            nested labels).
        :param body_filter: ``(uri, Range)`` — narrow to references whose
            position is inside the given range, in the given file.  Used by
            the LSP server for scope-aware lookups in ``label:``-style code
            where cheap locals share names across many parent routines.
        """
        refs = self._references_by_name.get(name, [])
        out: list[SymbolReference] = list(refs)
        if scope_path is not None:
            out = [r for r in out if _scope_matches(r.scope_path, scope_path)]
        if body_filter is not None:
            f_uri, f_range = body_filter
            out = [r for r in out if r.uri == f_uri and _position_in_range(r.range.start, f_range)]
        return out

    def search(self, query: str, limit: int = 50) -> list[WorkspaceSymbol]:
        """Fuzzy/substring search across symbol names (case-insensitive).

        v1 ranks by (1) exact match, (2) prefix match, (3) substring match,
        (4) subsequence (fuzzy) match, with stable ordering inside each band.
        Good enough to back LSP `workspace/symbol`."""
        q = query.lower()
        if not q:
            return []
        exact: list[WorkspaceSymbol] = []
        prefix: list[WorkspaceSymbol] = []
        substr: list[WorkspaceSymbol] = []
        fuzzy: list[WorkspaceSymbol] = []
        for name, recs in self._symbols_by_name.items():
            lname = name.lower()
            for s in recs:
                if lname == q:
                    exact.append(s)
                elif lname.startswith(q):
                    prefix.append(s)
                elif q in lname:
                    substr.append(s)
                elif _subseq(q, lname):
                    fuzzy.append(s)
        out: list[WorkspaceSymbol] = []
        for band in (exact, prefix, substr, fuzzy):
            out.extend(band)
            if len(out) >= limit:
                return out[:limit]
        return out[:limit]

    def all_symbols(self) -> Iterator[WorkspaceSymbol]:
        for recs in self._symbols_by_name.values():
            yield from recs

    # --------------------------------------------------------- internals

    def _reload_dbg(self) -> None:
        from ca65_ls.index.dbg_oracle import load as load_dbg  # local import; cheap

        self._dbg = _DbgLookup()
        files = _find_dbg_files(self.project_root)
        self._stats["dbg_files"] = len(files)
        self._dbg_signature = self._current_dbg_signature(files)
        for f in files:
            try:
                idx = load_dbg(f)
            except Exception:
                continue  # broken .dbg shouldn't poison the whole project index
            self._dbg.add_index(idx)

    def _current_dbg_signature(
        self, files: list[Path] | None = None
    ) -> tuple[tuple[str, int, int], ...]:
        out: list[tuple[str, int, int]] = []
        for f in _find_dbg_files(self.project_root) if files is None else files:
            try:
                st = f.stat()
            except OSError:
                continue
            out.append((str(f), st.st_mtime_ns, st.st_size))
        return tuple(out)

    def _drop_file(self, uri: str) -> None:
        """Remove every symbol and reference the file at `uri` contributed."""
        for sym in self._symbols_by_uri.pop(uri, []):
            bucket = self._symbols_by_name.get(sym.name, [])
            remaining = [s for s in bucket if s.uri != uri]
            if remaining:
                self._symbols_by_name[sym.name] = remaining
            else:
                self._symbols_by_name.pop(sym.name, None)
        for ref in self._references_by_uri.pop(uri, []):
            bucket = self._references_by_name.get(ref.name, [])
            remaining_refs = [r for r in bucket if r.uri != uri]
            if remaining_refs:
                self._references_by_name[ref.name] = remaining_refs
            else:
                self._references_by_name.pop(ref.name, None)
        self._exports_by_uri.pop(uri, None)
        self._signature_by_uri.pop(uri, None)

    def _recount(self) -> None:
        self._stats["files"] = len(self._signature_by_uri)
        self._stats["symbols"] = sum(len(v) for v in self._symbols_by_uri.values())
        self._stats["references"] = sum(len(v) for v in self._references_by_uri.values())

    def _cache_dir(self) -> Path:
        return self.project_root / ".ca65-ls" / "cache"

    def _cache_path_for(self, path: Path) -> Path:
        # Hash absolute path into the filename so all projects' caches
        # are namespaced inside their own .ca65-ls/cache/ dir.
        h = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
        return self._cache_dir() / f"{h[:16]}.pkl"

    def _load_from_cache(self, path: Path, key: str) -> _CachedFileEntry | None:
        if not self._cache_enabled:
            return None
        cp = self._cache_path_for(path)
        if not cp.is_file():
            return None
        try:
            with cp.open("rb") as f:
                entry = pickle.load(f)
        except Exception:
            return None
        if not isinstance(entry, _CachedFileEntry):
            return None
        if entry.format_version != CACHE_FORMAT_VERSION:
            return None
        if entry.key != key:
            return None
        return entry

    def _save_to_cache(self, path: Path, entry: _CachedFileEntry) -> None:
        if not self._cache_enabled:
            return
        try:
            cp = self._cache_path_for(path)
            cp.parent.mkdir(parents=True, exist_ok=True)
            with cp.open("wb") as f:
                pickle.dump(entry, f, protocol=pickle.HIGHEST_PROTOCOL)
        except OSError:
            pass  # cache is best-effort

    def _ingest_file(self, path: Path) -> None:
        """Parse one file from disk (cache-aware) and merge its entries into
        the indexes."""
        try:
            key = _file_cache_key(path)
            sig = _signature(path)
        except OSError:
            return

        cached = self._load_from_cache(path, key)
        if cached is not None:
            self._stats["cache_hits"] = int(self._stats["cache_hits"]) + 1
            buffer_symbols = cached.symbols
            references = cached.references
            exports = cached.exports
        else:
            self._stats["cache_misses"] = int(self._stats["cache_misses"]) + 1
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return
            view = self._parser(path, text)
            if view is None:
                # Parser layer not ready and no shim was provided. Record the
                # file in stats but contribute no symbols; we'll pick it up on
                # the next reindex once Document lands.
                self._signature_by_uri[path.as_uri()] = sig
                return
            buffer_symbols = view.flat_symbols()
            references = view.all_references()
            exports = _exported_names(text)
            self._save_to_cache(
                path,
                _CachedFileEntry(
                    key=key,
                    symbols=list(buffer_symbols),
                    references=list(references),
                    exports=exports,
                ),
            )

        self._merge(path.as_uri(), buffer_symbols, references, exports, sig)

    def _ingest_text(self, path: Path, text: str) -> None:
        """Parse one file from editor-buffer text.  Never touches the cache:
        the cache key is the on-disk stat, which this text need not match."""
        view = self._parser(path, text)
        if view is None:
            self._signature_by_uri[path.as_uri()] = None
            return
        self._merge(
            path.as_uri(), view.flat_symbols(), view.all_references(), _exported_names(text), None
        )

    def _merge(
        self,
        uri: str,
        buffer_symbols: list[BufferSymbol],
        references: list,
        exports: frozenset[str],
        sig: _Signature,
    ) -> None:
        """Promote one file's parse result into the name-keyed indexes."""
        self._signature_by_uri[uri] = sig
        self._exports_by_uri[uri] = exports

        ws_symbols: list[WorkspaceSymbol] = []
        for bs in _flatten(buffer_symbols):
            ws = self._promote(bs, uri)
            ws_symbols.append(ws)
            self._symbols_by_name.setdefault(ws.name, []).append(ws)
        self._symbols_by_uri[uri] = ws_symbols

        ref_records: list[SymbolReference] = []
        for item in references:
            if isinstance(item, SymbolReference):
                # Real-parser path. The Document already filled in its own
                # `uri`; re-wrap so the URI matches what the Indexer is
                # actually scanning (file:// vs whatever the doc was opened
                # with).
                ref = SymbolReference(
                    name=item.name,
                    uri=uri,
                    range=item.range,
                    scope_path=item.scope_path,
                )
            else:
                # Test/shim path: (name, Range, scope_path) tuple.
                ref_name, ref_range, ref_scope = item
                ref = SymbolReference(name=ref_name, uri=uri, range=ref_range, scope_path=ref_scope)
            ref_records.append(ref)
            self._references_by_name.setdefault(ref.name, []).append(ref)
        self._references_by_uri[uri] = ref_records

    def _promote(self, bs: BufferSymbol | WorkspaceSymbol, uri: str) -> WorkspaceSymbol:
        """Lift a BufferSymbol to a WorkspaceSymbol, enriching with .dbg if
        available.  Also accepts an existing WorkspaceSymbol (re-enrichment
        after a `.dbg` reload)."""
        rec = self._dbg.lookup(bs.name, bs.scope_path)
        address = getattr(rec, "addr", None) if rec is not None else None
        segment = getattr(rec, "segment", None) if rec is not None else None
        size = getattr(rec, "size", None) if rec is not None else None
        return WorkspaceSymbol(
            name=bs.name,
            kind=bs.kind,
            uri=uri,
            range=bs.range,
            selection_range=bs.selection_range,
            scope_path=bs.scope_path,
            parent_label=bs.parent_label,
            address=address,
            segment=segment,
            size=size,
        )


def _subseq(needle: str, haystack: str) -> bool:
    """Is `needle` a (non-contiguous) subsequence of `haystack`?"""
    it = iter(haystack)
    return all(c in it for c in needle)


# ------------------------------------------------------------------- BufferShim


def _r(line: int, col: int, end_col: int | None = None) -> Range:
    """Tiny helper for tests: build a single-line Range."""
    end_col = end_col if end_col is not None else col + 1
    return Range(Position(line, col), Position(line, end_col))


def BufferShim(  # noqa: N802 - factory named like a class for caller ergonomics
    file_symbols: dict[str, list[BufferSymbol]] | None = None,
    file_references: dict[str, list[tuple[str, Range, tuple[str, ...]]]] | None = None,
) -> Parser:
    """Build a `parser` callable that returns hardcoded BufferSymbols per
    file basename.

    Once the Parser engineer's `Document` ships, tests can drop the shim and
    use the default parser; production code already does. The shim's
    contract is:

        parser(abs_path: Path, text: str) -> BufferView | None

    matching the real `_default_parser`. The shim keys lookups on the
    file *basename* (e.g. `"helpers.s"`) rather than absolute path so the
    same dict works regardless of where the fixture happens to live.
    """
    file_symbols = file_symbols or {}
    file_references = file_references or {}

    def parse(path: Path, text: str) -> BufferView | None:
        key = path.name
        return _BufferViewImpl(
            _symbols=list(file_symbols.get(key, [])),
            _references=list(file_references.get(key, [])),
        )

    return parse


__all__ = [
    "WorkspaceIndex",
    "BufferShim",
    "BufferView",
    "Parser",
    "SOURCE_SUFFIXES",
]
