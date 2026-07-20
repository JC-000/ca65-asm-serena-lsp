"""
dbg_oracle -- parses cc65 .dbg files into a queryable symbol index.

Two roles for this module:
  1. Production: the Indexer enriches its tree-sitter-derived symbol table with
     address info, segment membership, and cross-module export resolution from
     a real build's .dbg file (when one exists).
  2. Test ground truth: the oracle parses the same .dbg file independently of
     the rest of ca65-ls and is used by tests to assert that workspace/symbol
     results agree with cc65's own view of the world.

Format reference: cc65/cc65 src/ld65/dbgfile.c (also cc65.github.io/doc/debug-info.html).
The file is line-oriented; each line is a record of the form:

    <kind> key=value,key=value,...

Values can be quoted strings, hex ("0x..."), decimals, or +-separated id lists
("1+2+3").  The first non-comment line is `version major=M,minor=N`; the second
is an `info` record listing counts of each subsequent record type.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- raw parsing


_RECORD_RE = re.compile(r"^([a-z][a-z0-9_]*)\s+(.*)$")


def _parse_value(raw: str) -> Any:
    """Parse a single field value: quoted string, hex int, decimal int, or +-list."""
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    if "+" in raw and all(
        part.lstrip("0x").replace("x", "").rstrip().isalnum() for part in raw.split("+")
    ):
        return [_parse_value(part) for part in raw.split("+")]
    if raw.startswith("0x") or raw.startswith("0X"):
        return int(raw, 16)
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


def _split_fields(body: str) -> dict[str, Any]:
    """Split a record body into a dict, respecting quotes."""
    fields: dict[str, Any] = {}
    i = 0
    n = len(body)
    while i < n:
        # Read key
        key_start = i
        while i < n and body[i] != "=":
            i += 1
        if i >= n:
            break
        key = body[key_start:i].strip()
        i += 1  # skip '='
        # Read value (respect quoted strings)
        if i < n and body[i] == '"':
            i += 1
            val_start = i
            while i < n and body[i] != '"':
                i += 1
            value = '"' + body[val_start:i] + '"'
            i += 1  # closing quote
        else:
            val_start = i
            while i < n and body[i] != ",":
                i += 1
            value = body[val_start:i]
        if i < n and body[i] == ",":
            i += 1
        fields[key] = _parse_value(value)
    return fields


def parse_dbg(path: Path | str) -> dict[str, list[dict[str, Any]]]:
    """Parse a .dbg file into {record_kind: [field_dict, ...]}."""
    text = Path(path).read_text(encoding="utf-8")
    out: dict[str, list[dict[str, Any]]] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _RECORD_RE.match(line)
        if not m:
            raise ValueError(f"{path}:{lineno}: malformed record: {line!r}")
        kind, body = m.group(1), m.group(2)
        out.setdefault(kind, []).append(_split_fields(body))
    return out


# ------------------------------------------------------- normalized symbol view


@dataclass(frozen=True)
class SymbolRecord:
    """A single symbol with all the cross-references resolved into a flat record."""

    name: str
    kind: str  # "label" | "equ" | "import" | "scope" | "proc" | "struct"
    file: str | None  # source file path (relative to compiler cwd) where defined
    line: int | None  # 1-based source line of definition
    scope_path: tuple[str, ...]  # e.g. ("helpers", "foo") for helpers::foo's @inner
    parent_name: str | None  # for cheap locals: the enclosing non-cheap label
    addrsize: str  # "zeropage" | "absolute" | "far" | ...
    addr: int | None  # resolved address if known
    size: int | None  # size in bytes if known
    segment: str | None  # segment name (CODE/BSS/ZEROPAGE/...) if known
    import_of: str | None  # for type=imp: name of the exporting symbol


@dataclass
class DbgIndex:
    """A queryable, normalized view of a parsed .dbg file."""

    symbols_by_name: dict[str, list[SymbolRecord]] = field(default_factory=dict)
    files: dict[int, str] = field(default_factory=dict)
    scopes: dict[int, dict[str, Any]] = field(default_factory=dict)
    raw: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def lookup(self, name: str) -> list[SymbolRecord]:
        return self.symbols_by_name.get(name, [])

    def all_exports(self) -> list[SymbolRecord]:
        """All symbols defined here that other modules can .import (labels + equs not marked type=imp)."""
        return [
            rec for recs in self.symbols_by_name.values() for rec in recs if rec.kind != "import"
        ]


def build_index(parsed: dict[str, list[dict[str, Any]]]) -> DbgIndex:
    """Turn the raw parse into a queryable index with scope paths resolved."""
    idx = DbgIndex(raw=parsed)

    # File id -> path
    for f in parsed.get("file", []):
        idx.files[int(f["id"])] = str(f["name"])

    # Line id -> (file_id, line_no)
    line_loc: dict[int, tuple[int, int]] = {}
    for ln in parsed.get("line", []):
        line_loc[int(ln["id"])] = (int(ln["file"]), int(ln["line"]))

    # Scope id -> raw dict, plus walk to build a name path
    scopes_raw = {int(s["id"]): s for s in parsed.get("scope", [])}
    idx.scopes = scopes_raw

    def scope_path_of(scope_id: int) -> tuple[str, ...]:
        """Walk parent chain, collecting non-empty scope names from outer to inner."""
        path: list[str] = []
        cur_id: int | None = scope_id
        seen: set[int] = set()
        while cur_id is not None and cur_id not in seen:
            seen.add(cur_id)
            sc = scopes_raw.get(cur_id)
            if sc is None:
                break
            name = str(sc.get("name", "")).strip()
            if name:
                path.append(name)
            parent = sc.get("parent")
            cur_id = int(parent) if parent is not None else None
        return tuple(reversed(path))

    # Sym id -> exporter name (for resolving type=imp records)
    syms_raw = {int(s["id"]): s for s in parsed.get("sym", [])}
    exporter_name: dict[int, str] = {}
    for s in syms_raw.values():
        if s.get("type") == "lab" or s.get("type") == "equ":
            exporter_name[int(s["id"])] = str(s["name"])

    # Parent sym -> enclosing label name (for cheap-locals lookup)
    def parent_label_name(parent_id_field: Any) -> str | None:
        if parent_id_field is None:
            return None
        pid = int(parent_id_field)
        p = syms_raw.get(pid)
        if p is None:
            return None
        return str(p.get("name"))

    # First def-line id (sym.def can be a single int or a list of ints)
    def first_line(def_field: Any) -> tuple[int | None, int | None]:
        if def_field is None:
            return (None, None)
        first = def_field[0] if isinstance(def_field, list) else def_field
        loc = line_loc.get(int(first))
        if loc is None:
            return (None, None)
        file_id, lineno = loc
        return (file_id, lineno)

    KIND_MAP = {"lab": "label", "equ": "equ", "imp": "import"}

    for s in syms_raw.values():
        raw_kind = str(s.get("type", ""))
        kind = KIND_MAP.get(raw_kind, raw_kind)
        scope_id = int(s.get("scope", 0)) if s.get("scope") is not None else None
        file_id, lineno = first_line(s.get("def"))
        path = scope_path_of(scope_id) if scope_id is not None else ()

        # For imports, look up the exporter so we can cross-reference back.
        imp_of: str | None = None
        if kind == "import" and s.get("exp") is not None:
            imp_of = exporter_name.get(int(s["exp"]))

        rec = SymbolRecord(
            name=str(s["name"]),
            kind=kind,
            file=idx.files.get(file_id) if file_id is not None else None,
            line=lineno,
            scope_path=path,
            parent_name=parent_label_name(s.get("parent")),
            addrsize=str(s.get("addrsize", "")),
            addr=int(s["val"]) if s.get("val") is not None else None,
            size=int(s["size"]) if s.get("size") is not None else None,
            segment=None,  # filled in below via seg cross-reference
            import_of=imp_of,
        )
        idx.symbols_by_name.setdefault(rec.name, []).append(rec)

    # Decorate with segment names by re-walking sym records that have seg=N
    seg_names = {int(sg["id"]): str(sg["name"]) for sg in parsed.get("seg", [])}
    for s in syms_raw.values():
        if s.get("seg") is None:
            continue
        seg_name = seg_names.get(int(s["seg"]))
        if seg_name is None:
            continue
        name = str(s["name"])
        # Find the matching record in our index (dataclass is frozen, so rebuild)
        recs = idx.symbols_by_name.get(name, [])
        for i, r in enumerate(recs):
            if r.segment is None and r.kind != "import":
                recs[i] = SymbolRecord(**{**r.__dict__, "segment": seg_name})
                break

    return idx


# ----------------------------------------------------------- helpers and CLI


def load(path: Path | str) -> DbgIndex:
    """Convenience: parse + index in one call."""
    return build_index(parse_dbg(path))


def _to_json(idx: DbgIndex) -> dict[str, Any]:
    return {
        "symbols": {
            name: [
                {
                    "name": r.name,
                    "kind": r.kind,
                    "file": r.file,
                    "line": r.line,
                    "scope_path": list(r.scope_path),
                    "parent_name": r.parent_name,
                    "addrsize": r.addrsize,
                    "addr": r.addr,
                    "size": r.size,
                    "segment": r.segment,
                    "import_of": r.import_of,
                }
                for r in recs
            ]
            for name, recs in sorted(idx.symbols_by_name.items())
        }
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="ca65-dbg-dump",
        description="Parse a cc65 .dbg file and emit a JSON symbol index.",
    )
    p.add_argument("dbgfile", type=Path, help="Path to a .dbg file from ld65 --dbgfile")
    p.add_argument("--name", help="Look up only this symbol (else dump everything)")
    args = p.parse_args(argv)

    idx = load(args.dbgfile)
    if args.name:
        result = [r.__dict__ for r in idx.lookup(args.name)]
        json.dump(result, sys.stdout, indent=2, default=list)
    else:
        json.dump(_to_json(idx), sys.stdout, indent=2, default=list)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
