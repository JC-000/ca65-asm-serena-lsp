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
import pickle
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

from ca65_ls.types import (
    BufferSymbol,
    Position,
    Range,
    SymbolKind,
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
_DEFAULT_IGNORES = (
    "build/",
    "obj/",
    ".venv/",
    "__pycache__/",
    ".git/",
    ".ca65-ls/",
)
CACHE_FORMAT_VERSION = 2  # bump if the on-disk cache shape changes


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
Parser = Callable[[Path, str], Optional[BufferView]]


# --------------------------------------------------------------------- helpers


def _default_parser(path: Path, text: str) -> BufferView | None:
    """Real-Document path. Returns None if the Parser layer hasn't landed yet.

    The real `Document.references_in` is keyed by name (returns one symbol's
    use sites). To gather everything we need for the workspace index we
    union over all unique names mentioned in the document — flat symbol
    names plus any name returned by `references_in` for those symbols.
    For workspace lookups, what matters is that every reference to a
    name that appears as a symbol somewhere in the workspace gets
    aggregated; references to names that nobody defines are dead-weight
    we can defer to M4 (hover-on-unknown).
    """
    if Document is None:
        return None
    doc = Document(uri=path.as_uri(), text=text)  # type: ignore[call-arg]
    symbols = list(doc.flat_symbols())  # type: ignore[attr-defined]
    refs: list = []
    # The real Document has a private `_walk_references` style helper, but
    # we restrict to the public API: ask for refs of every name that
    # appears somewhere in flat_symbols (which catches imports, exports,
    # and definitions). This is enough for cross-file linkage in v1.
    seen_names: set[str] = set()

    def _add_names(syms: list[BufferSymbol]) -> None:
        for s in syms:
            seen_names.add(s.name)
            if s.children:
                _add_names(list(s.children))

    _add_names(symbols)
    for name in seen_names:
        try:
            refs.extend(doc.references_in(name))  # type: ignore[attr-defined]
        except Exception:
            continue
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
    """Walk nested .children trees, yielding every BufferSymbol once."""
    for sym in symbols:
        yield sym
        if sym.children:
            yield from _flatten(list(sym.children))


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


def _is_ignored(rel_posix: str, spec) -> bool:
    """Combine .gitignore (if any) with our default build-ish exclusions."""
    if spec is not None and spec.match_file(rel_posix):
        return True
    for pat in _DEFAULT_IGNORES:
        if rel_posix.startswith(pat) or f"/{pat}" in f"/{rel_posix}":
            return True
    return False


def _iter_source_files(project_root: Path) -> Iterator[Path]:
    """Yield candidate `.s/.asm/.inc` files, respecting .gitignore + defaults."""
    spec = _load_gitignore(project_root)
    for path in project_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        try:
            rel = path.relative_to(project_root).as_posix()
        except ValueError:  # pragma: no cover
            continue
        if _is_ignored(rel, spec):
            continue
        yield path


def _scope_matches(reference_scope: tuple[str, ...],
                   symbol_scope: tuple[str, ...]) -> bool:
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
    if (pos.line, pos.character) >= (r.end.line, r.end.character):
        return False
    return True


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
        self._stats["files"] = 0
        self._stats["symbols"] = 0
        self._stats["references"] = 0
        self._stats["cache_hits"] = 0
        self._stats["cache_misses"] = 0

        # Load .dbg enrichers first so per-file ingest can decorate
        # WorkspaceSymbol records inline.
        self._reload_dbg()

        for path in _iter_source_files(self.project_root):
            self._ingest_file(path)

        self._stats["last_reindex_seconds"] = time.perf_counter() - t0

    def reindex_file(self, path: Path) -> None:
        """Re-parse a single file. Cheap path for save/didChange handlers."""
        path = Path(path).resolve()
        uri = path.as_uri()
        # Drop existing entries from this file.
        for sym in self._symbols_by_uri.pop(uri, []):
            bucket = self._symbols_by_name.get(sym.name, [])
            self._symbols_by_name[sym.name] = [s for s in bucket if s.uri != uri]
            if not self._symbols_by_name[sym.name]:
                del self._symbols_by_name[sym.name]
        for ref in self._references_by_uri.pop(uri, []):
            bucket = self._references_by_name.get(ref.name, [])
            self._references_by_name[ref.name] = [r for r in bucket if r.uri != uri]
            if not self._references_by_name[ref.name]:
                del self._references_by_name[ref.name]
        # Add fresh entries.
        if path.is_file():
            self._ingest_file(path, single_file=True)

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
        for f in files:
            try:
                idx = load_dbg(f)
            except Exception:
                continue  # broken .dbg shouldn't poison the whole project index
            self._dbg.add_index(idx)

    def _cache_dir(self) -> Path:
        return self.project_root / ".ca65-ls" / "cache"

    def _cache_path_for(self, path: Path) -> Path:
        # Hash absolute path into the filename so all projects' caches
        # are namespaced inside their own .ca65-ls/cache/ dir.
        h = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
        return self._cache_dir() / f"{h[:16]}.pkl"

    def _load_from_cache(
        self, path: Path, key: str
    ) -> _CachedFileEntry | None:
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

    def _ingest_file(self, path: Path, *, single_file: bool = False) -> None:
        """Parse one file (cache-aware) and merge its entries into the indexes."""
        try:
            key = _file_cache_key(path)
        except OSError:
            return

        cached = self._load_from_cache(path, key)
        if cached is not None:
            self._stats["cache_hits"] = int(self._stats["cache_hits"]) + 1
            buffer_symbols = cached.symbols
            references = cached.references
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
                self._stats["files"] = int(self._stats["files"]) + 1
                return
            buffer_symbols = view.flat_symbols()
            references = view.all_references()
            self._save_to_cache(
                path,
                _CachedFileEntry(
                    key=key,
                    symbols=list(buffer_symbols),
                    references=list(references),
                ),
            )

        self._stats["files"] = int(self._stats["files"]) + 1
        uri = path.as_uri()

        ws_symbols: list[WorkspaceSymbol] = []
        for bs in _flatten(buffer_symbols):
            ws = self._promote(bs, uri)
            ws_symbols.append(ws)
            self._symbols_by_name.setdefault(ws.name, []).append(ws)
        self._symbols_by_uri[uri] = ws_symbols
        self._stats["symbols"] = int(self._stats["symbols"]) + len(ws_symbols)

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
                ref = SymbolReference(
                    name=ref_name, uri=uri, range=ref_range, scope_path=ref_scope
                )
            ref_records.append(ref)
            self._references_by_name.setdefault(ref.name, []).append(ref)
        self._references_by_uri[uri] = ref_records
        self._stats["references"] = int(self._stats["references"]) + len(ref_records)

        if single_file:
            # In incremental mode we may not have reloaded .dbg, which is
            # fine: the cached enrichment from the last full reindex still
            # applies to the freshly-ingested records.
            pass

    def _promote(self, bs: BufferSymbol, uri: str) -> WorkspaceSymbol:
        """Lift a BufferSymbol to a WorkspaceSymbol, enriching with .dbg if available."""
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
