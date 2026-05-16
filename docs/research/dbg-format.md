# cc65 `.dbg` debug-info file format

Authoritative source: `cc65/cc65` tree at `github.com/cc65/cc65`, specifically
`src/ld65/dbgfile.c` and the per-record `Print*` routines listed below. The
file is emitted only when `ld65 --dbgfile <name>` (or its long form
`--dbgfile=<name>`) is passed; **passing `-g` to `ca65` is required** so
the object file carries the debug payload, but it is `ld65` that produces
the `.dbg` text. User-facing prose:
[cc65.github.io/doc/debug-info.html](https://cc65.github.io/doc/debug-info.html)
and [cc65.github.io/doc/ld65.html](https://cc65.github.io/doc/ld65.html).

## High-level structure

The file is plain ASCII, line-oriented, LF-terminated. Each line is one
record. Record layout is:

```
<record-kind>\t<key>=<value>[,<key>=<value>]*\n
```

The record kind is separated from the key/value list by a literal **tab**
(`\t`); inside the list, keys are separated by commas. Values that are
strings are double-quoted; values that are numbers may be plain decimal
or `0x`-prefixed hex; reference IDs are always plain decimal.

The kinds are emitted in this fixed order by `CreateDbgFile()`
([src/ld65/dbgfile.c#L104-L171](https://raw.githubusercontent.com/cc65/cc65/master/src/ld65/dbgfile.c)):

| # | Kind     | Section | Source `Print*` |
|---|----------|---------|-----------------|
| 1 | `version` | always 1 line | dbgfile.c#L114 |
| 2 | `info`    | always 1 line, totals | dbgfile.c#L122 |
| 3 | `csym`    | C-level symbols (only if cc65 emitted them) | `PrintHLLDbgSyms` (dbgsyms.c#L445) |
| 4 | `file`    | input files | `PrintDbgFileInfo` (fileinfo.c#L230) |
| 5 | `lib`     | input libraries | `PrintDbgLibraries` (library.c#L542) |
| 6 | `line`    | source-line mappings | `PrintDbgLineInfo` (lineinfo.c#L245) |
| 7 | `mod`     | object modules linked in | `PrintDbgModules` (objdata.c#L281) |
| 8 | `seg`     | output segments | `PrintDbgSegments` (segments.c#L629) |
| 9 | `span`    | byte ranges within a segment | `PrintDbgSpans` (span.c#L206) |
| 10 | `scope`  | symbol scopes (procs, scopes, structs, enums, files, global) | `PrintDbgScopes` (scopes.c#L118) |
| 11 | `sym`    | assembly symbols (labels, equates, imports) | `PrintDbgSyms` (dbgsyms.c#L351) |
| 12 | `type`   | C type-info table (only when cc65 emitted types) | `PrintDbgTypes` |

The order is stable across cc65 versions: consumers should still not rely
on it (e.g. `sym` records reference `scope` IDs that aren't seen until
later in the file). Two-pass parsing is required.

## Format version

The first line is exactly:

```
version	major=2,minor=0
```

This has held since cc65 2.13 (when `--dbgfile` matured). The MAJOR
revision changes if and only if the field set of an existing record kind
changes incompatibly; MINOR revs add new optional fields. As of cc65
2.19 the value is still `2.0`.

## Record reference: fields and meanings

### `version`

```
version	major=<u>,minor=<u>
```

Always one record. Currently `major=2,minor=0`.

### `info`

```
info	csym=<u>,file=<u>,lib=<u>,line=<u>,mod=<u>,scope=<u>,seg=<u>,span=<u>,sym=<u>,type=<u>
```

A hint for the consumer to pre-allocate. The counts are the values
returned by `*Count()` aggregators at link time. **Note:** in practice
these can over-count slightly — observed `file=711` while only 72 `file`
records appear in the same file (likely a count-before-dedupe in the
linker). Indexer should treat as advisory.

### `csym` (C-level symbol — emitted by cc65, not relevant for hand-asm)

```
csym	id=<u>,name="<s>",scope=<u>,type=<u>,sc=<auto|reg|static|ext>[,offs=<i>][,sym=<u>]
```

| Field | Type | Meaning |
|---|---|---|
| `id` | u32 | Unique within file |
| `name` | string | C identifier |
| `scope` | u32 | → `scope.id` |
| `type` | u32 | → `type.id` |
| `sc` | enum | Storage class |
| `offs` | i32 | Stack offset (auto vars only); omitted when zero |
| `sym` | u32 | → `sym.id` of the asm symbol backing this C symbol; omitted for `auto`/`reg` |

Source: dbgsyms.c#L465. **For a CA65-only project this section is empty.**

### `file`

```
file	id=<u>,name="<s>",size=<u>,mtime=0x<hex32>,mod=<u>[+<u>]*
```

| Field | Type | Meaning |
|---|---|---|
| `id` | u32 | File ID, referenced by `line.file` and `mod.file` |
| `name` | string | Absolute or build-relative path |
| `size` | u32 | Bytes |
| `mtime` | hex32 | Unix mtime as the linker saw it |
| `mod` | u32 list | `+`-separated module IDs that consumed this file. The first ID has no separator; subsequent IDs are prefixed with `+`. |

Example (real, from `hello.dbg`):

```
file	id=0,name="hello.s",size=5195,mtime=0x6A08F0F2,mod=0
file	id=18,name="/opt/.../asminc/cpu.mac",size=843,mtime=0x5ED2ADF3,mod=15+28+33+34+35+38+40+45
```

Source: fileinfo.c#L230, multi-module emit at fileinfo.c#L253.

### `lib`

```
lib	id=<u>,name="<s>"
```

One line per `.lib` archive on the link line. Referenced by `mod.lib`.
Source: library.c#L542.

### `line`

```
line	id=<u>,file=<u>,line=<u>[,type=<u>][,count=<u>][,span=<u>[+<u>]*]
```

| Field | Type | Meaning |
|---|---|---|
| `id` | u32 | Line-info ID, referenced from `sym.def`/`sym.ref` |
| `file` | u32 | → `file.id` |
| `line` | u32 | 1-based line number in that file |
| `type` | u32 | Omitted ⇒ `LI_TYPE_ASM (=0)` (a literal asm line). `1` = `LI_TYPE_EXT` (line from a generated/external mapping, e.g. cc65-to-asm). `2` = `LI_TYPE_MACRO` |
| `count` | u32 | Macro/include expansion count; omitted when 0 |
| `span` | u32 list | `+`-separated span IDs covering the bytes emitted by this source line; absent for non-emitting lines (comments, blank, directives that produce no bytes) |

`span` IDs reference `span.id` records (see below). The
`PrintDbgSpanList` helper at `span.c#L188` is the shared formatter for
this `+`-list.

Sources:
[lineinfo.c#L267](https://raw.githubusercontent.com/cc65/cc65/master/src/ld65/lineinfo.c).

### `mod`

```
mod	id=<u>,name="<s>",file=<u>[,lib=<u>]
```

One per object file linked in.

| Field | Meaning |
|---|---|
| `id` | Module ID; matches `file.mod` references |
| `name` | Original `.o` filename |
| `file` | → `file.id` of the primary source (`O->Files[0]`); for `hello.o` this is `hello.s` |
| `lib` | → `lib.id` if the module came out of an archive; omitted for standalone `.o` |

Sources: objdata.c#L281.

### `seg`

```
seg	id=<u>,name="<s>",start=0x<hex24>,size=0x<hex16>,addrsize=<absolute|zeropage|far|long>,type=<ro|rw>[,oname="<s>",ooffs=<u>][,bank=<u>]
```

| Field | Meaning |
|---|---|
| `id` | Segment ID; referenced from `span.seg` and `sym.seg` |
| `name` | The segment name from `.segment "FOO"` (or `CODE`/`BSS`/`DATA`/`RODATA`/`ZEROPAGE`) |
| `start` | Run-time start address (resolved from the linker config) |
| `size` | Bytes emitted into this segment |
| `addrsize` | Address-size class. `absolute` = 16-bit, `zeropage` = 8-bit, `far` = 24-bit, `long` = 32-bit |
| `type` | `ro` (read-only / code-or-rodata) or `rw` (BSS, data, ZP) |
| `oname` | Output file the segment was written to (e.g. `"hello.prg"`); omitted for BSS-style segments not written to disk |
| `ooffs` | Byte offset within `oname` where the segment starts |
| `bank` | Memory bank index (banked targets only); omitted if not banked |

Examples (real):

```
seg	id=0,name="CODE",start=0x000840,size=0x0872,addrsize=absolute,type=ro,oname="hello.prg",ooffs=65
seg	id=2,name="BSS",start=0x0011B9,size=0x002D,addrsize=absolute,type=rw
seg	id=4,name="ZEROPAGE",start=0x000002,size=0x001A,addrsize=zeropage,type=rw
```

Source: segments.c#L629.

### `span`

```
span	id=<u>,seg=<u>,start=<u>,size=<u>[,type=<u>]
```

A `span` is a contiguous byte range within one segment, attributed to a
single source line. The indexer doesn't usually need spans for symbol
resolution but they're useful for "what byte does this label produce".

| Field | Meaning |
|---|---|
| `id` | Span ID |
| `seg` | → `seg.id` |
| `start` | Byte offset within the segment (not the absolute address — add `seg.start` for that) |
| `size` | Bytes |
| `type` | → `type.id`; omitted when the span carries no type info |

Source: span.c#L229.

### `scope`

```
scope	id=<u>,name="<s>",mod=<u>[,type=<global|scope|struct|enum>][,size=<u>][,parent=<u>][,sym=<u>][,span=<u>[+<u>]*]
```

| Field | Meaning |
|---|---|
| `id` | Scope ID; referenced from `sym.scope` |
| `name` | Scope name. Empty string `""` for the file-level (top) scope |
| `mod` | → `mod.id`; the owning module |
| `type` | Scope kind. `global`, `scope` (`.scope`/`.proc`), `struct`, `enum`. **Omitted ⇒ the implicit `file` scope** (i.e. the unnamed top-of-file scope of a module). |
| `size` | Bytes the scope's code occupies (only for `.proc`-like scopes) |
| `parent` | → `scope.id` of the enclosing scope; omitted when the scope is its own parent (top-of-file scope of the module) |
| `sym` | → `sym.id` of the symbol that labels this scope (the `.proc`'s entry label) |
| `span` | `+`-list of `span.id` covering this scope's emitted bytes |

Examples (real):

```
scope	id=0,name="",mod=0,size=202,span=113+112
scope	id=1,name="_main",mod=0,type=scope,size=202,parent=0,sym=12,span=112
```

Source: scopes.c#L118.

### `sym`

This is the workhorse record for the LSP. Two distinct sub-shapes:

**(a) Imported symbol (`type=imp`):**

```
sym	id=<u>,name="<s>",addrsize=<…>[,size=<u>],scope=<u>,def=<u>[+<u>]*,ref=<u>[+<u>]*,type=imp[,exp=<u>]
```

**(b) Defined symbol (label `type=lab` or equate `type=equ`):**

```
sym	id=<u>,name="<s>",addrsize=<…>[,size=<u>],scope=<u>,def=<u>[+<u>]*,ref=<u>[+<u>]*,val=0x<hex>[,seg=<u>],type=<lab|equ>
```

For cheap local symbols (`@foo:`), `parent=<u>` replaces `scope=<u>`:

**(c) Cheap local (`@foo:`):**

```
sym	id=<u>,name="<s>",addrsize=<…>[,size=<u>],parent=<u>,def=<u>[+<u>]*,ref=<u>[+<u>]*,val=0x<hex>[,seg=<u>],type=lab
```

| Field | Meaning |
|---|---|
| `id` | Symbol ID |
| `name` | Identifier as written; for unnamed labels `:`, **not emitted at all** (anonymous labels are dropped from `.dbg`) |
| `addrsize` | One of `absolute`, `zeropage`, `far`, `long` |
| `size` | Bytes the labelled item occupies (only when known) |
| `scope` | → `scope.id` (for **non**-cheap-locals). Per dbgsyms.c#L382: `SYM_IS_STD(S->Type) ⇒ scope`, otherwise `parent` |
| `parent` | → `sym.id` of the enclosing non-cheap label (for cheap locals `@foo:`) |
| `def` | `+`-list of `line.id` where the symbol is defined |
| `ref` | `+`-list of `line.id` where the symbol is referenced |
| `val` | Resolved address/value (defined symbols only) |
| `seg` | → `seg.id` (defined symbols, when the value is in a segment) |
| `type` | `imp` (import), `lab` (label / location-fixed), `equ` (equate / `name = expr`) |
| `exp` | → `sym.id` of the matching `.export` in another module (`type=imp` only; absent if the exporter has no dbg info) |

Examples (real, from `hello.dbg`):

```
sym	id=12,name="_main",addrsize=absolute,size=202,scope=0,def=38,ref=63,val=0x840,seg=0,type=lab
sym	id=0,name="incsp2",addrsize=absolute,scope=1,def=67,ref=50,type=imp
sym	id=24,name="sp",addrsize=zeropage,scope=0,def=90,ref=51+5+68+27+58+93+129+32+94+112+109+54+110+2+3,type=imp
```

Source: dbgsyms.c#L351-L441. The `type=imp` branch is at L396-L414;
defined-symbol branch at L415-L435.

### `type`

```
type	id=<u>,val="<hex-encoded>"
```

C type encoding. The `val` is a hex-encoded type-descriptor blob; format
is documented in `src/common/gentype.h`. **Not used by the LSP for
assembly symbol indexing.**

## ID cross-reference graph

```
                              file <─────────── mod
                                ▲   (file.mod=)  │
                                │                │ (mod.file=)
                                │                ▼
            line.file = file.id ┘             seg
                ▲                              ▲ │
   (sym.def/ref)│                              │ │ (span.seg=)
                │                              │ ▼
                sym ──────► scope ──► scope   span
        (sym.scope=scope.id)   ▲                ▲
        (sym.parent=sym.id)    │                │
                              (scope.parent)    │
                                                │
                                  (scope.span=span.id list,
                                   line.span=span.id list)

   csym.scope=scope.id, csym.sym=sym.id      lib.id ← mod.lib
   sym.exp=sym.id (cross-module import→export resolution)
```

Practical traversal for "find me everywhere `foo` is referenced":

1. Find `sym` records where `name="foo"`.
2. For each `ref` ID, look up the `line` record → get `(file, line)`.
3. For each `def` ID, same lookup → the canonical definition site(s).
4. For cross-module follow: if `type=imp`, follow `exp` to the
   exporting module's `sym`, then recurse.

## Version-specific notes

- `version 2.0` has been the emitted version since at least cc65 2.13.
  No incompatible change has been recorded through cc65 2.19 (current
  brew formula, October 2025).
- Pre-2.0 (cc65 ≤ 2.12) used a different, undocumented format that the
  current `da65`/`dbginfo` consumers can no longer parse. The indexer
  should reject any file whose first line is not `version\tmajor=2,...`.
- The `bank` field on `seg` was added when banked-memory targets landed
  (cc65 2.15ish, for Atari 130XE etc.). Older `.dbg` files simply omit
  it — no version bump.
- `csym`, `type`, and the `count=` field on `line` are only emitted
  when cc65 actually produced HLL debug info, i.e. the input chain
  involved `cc65 -g`. A pure-asm project (the c64-* corpora) will have
  `csym=0,type=0` in the `info` line and no records of those kinds.

## Annotated sample (~30 lines, all record kinds)

Redacted from real `hello.dbg` (cc65 2.19, `c64` target):

```
version	major=2,minor=0
info	csym=15,file=72,lib=1,line=3114,mod=49,scope=2,seg=11,span=114,sym=25,type=3
csym	id=12,name="main",scope=1,type=0,sc=ext,sym=12
csym	id=13,name="XSize",scope=1,type=0,sc=auto,offs=-1
file	id=0,name="hello.s",size=5195,mtime=0x6A08F0F2,mod=0
file	id=18,name="cpu.mac",size=843,mtime=0x5ED2ADF3,mod=15+28+33+34+35+38+40+45
lib	id=0,name="/opt/.../lib/c64.lib"
line	id=12,file=0,line=218,span=98
line	id=17,file=2,line=89,type=1,span=110
line	id=39,file=0,line=248
mod	id=0,name="hello.o",file=0
mod	id=1,name="_cursor.o",file=17,lib=0
seg	id=0,name="CODE",start=0x000840,size=0x0872,addrsize=absolute,type=ro,oname="hello.prg",ooffs=65
seg	id=2,name="BSS",start=0x0011B9,size=0x002D,addrsize=absolute,type=rw
seg	id=4,name="ZEROPAGE",start=0x000002,size=0x001A,addrsize=zeropage,type=rw
span	id=0,seg=1,start=0,size=13,type=1
span	id=2,seg=0,start=0,size=3
scope	id=0,name="",mod=0,size=202,span=113+112
scope	id=1,name="_main",mod=0,type=scope,size=202,parent=0,sym=12,span=112
sym	id=12,name="_main",addrsize=absolute,size=202,scope=0,def=38,ref=63,val=0x840,seg=0,type=lab
sym	id=0,name="incsp2",addrsize=absolute,scope=1,def=67,ref=50,type=imp
sym	id=24,name="sp",addrsize=zeropage,scope=0,def=90,ref=51+5+68,type=imp
type	id=0,val="00"
type	id=1,val="800D20"
```

## Reference grammar (for the Indexer engineer)

```ebnf
file        = version_line info_line { record } ;
version_line = "version" TAB "major=" UINT "," "minor=" UINT NL ;
info_line   = "info" TAB info_fields NL ;
info_fields = info_pair { "," info_pair } ;
info_pair   = ("csym" | "file" | "lib" | "line" | "mod"
              | "scope" | "seg" | "span" | "sym" | "type") "=" UINT ;

record      = ( csym_rec | file_rec | lib_rec | line_rec | mod_rec
              | seg_rec | span_rec | scope_rec | sym_rec | type_rec ) NL ;

csym_rec    = "csym" TAB "id=" UINT "," "name=" STRING "," "scope=" UINT ","
                       "type=" UINT "," "sc=" ("auto"|"reg"|"static"|"ext")
                       [ "," "offs=" INT ] [ "," "sym=" UINT ] ;

file_rec    = "file" TAB "id=" UINT "," "name=" STRING "," "size=" UINT ","
                       "mtime=0x" HEX "," "mod=" UINT { "+" UINT } ;

lib_rec     = "lib"  TAB "id=" UINT "," "name=" STRING ;

line_rec    = "line" TAB "id=" UINT "," "file=" UINT "," "line=" UINT
                       [ "," "type=" UINT ] [ "," "count=" UINT ]
                       [ "," "span=" UINT { "+" UINT } ] ;

mod_rec     = "mod"  TAB "id=" UINT "," "name=" STRING "," "file=" UINT
                       [ "," "lib=" UINT ] ;

seg_rec     = "seg"  TAB "id=" UINT "," "name=" STRING ","
                       "start=0x" HEX "," "size=0x" HEX ","
                       "addrsize=" ADDR_SIZE "," "type=" ("ro"|"rw")
                       [ "," "oname=" STRING "," "ooffs=" UINT ]
                       [ "," "bank=" UINT ] ;

span_rec    = "span" TAB "id=" UINT "," "seg=" UINT ","
                       "start=" UINT "," "size=" UINT
                       [ "," "type=" UINT ] ;

scope_rec   = "scope" TAB "id=" UINT "," "name=" STRING "," "mod=" UINT
                       [ "," "type=" SCOPE_TYPE ] [ "," "size=" UINT ]
                       [ "," "parent=" UINT ] [ "," "sym=" UINT ]
                       [ "," "span=" UINT { "+" UINT } ] ;

sym_rec     = "sym"  TAB "id=" UINT "," "name=" STRING ","
                       "addrsize=" ADDR_SIZE [ "," "size=" UINT ]
                       ( "," "scope=" UINT | "," "parent=" UINT )
                       [ "," "def=" UINT { "+" UINT } ]
                       [ "," "ref=" UINT { "+" UINT } ]
                       ( imp_tail | def_tail ) ;
imp_tail    = "," "type=imp" [ "," "exp=" UINT ] ;
def_tail    = "," "val=0x" HEX [ "," "seg=" UINT ]
                       "," "type=" ("lab" | "equ") ;

type_rec    = "type" TAB "id=" UINT "," "val=" STRING ;

ADDR_SIZE   = "absolute" | "zeropage" | "far" | "long" ;
SCOPE_TYPE  = "global" | "scope" | "struct" | "enum" ;  (* "file" = field absent *)

UINT, INT   = ASCII decimal ;
HEX         = ASCII hex digits ;
STRING      = '"' { any char except '"' } '"' ;  (* no escape sequences observed *)
TAB         = "\t" ;  NL = "\n" ;
```

## Indexer cheat-sheet — minimum viable parse

For Serena's `workspace/symbol` and `find_referencing_symbols`, the
indexer only needs to load three record kinds:

1. **`file`** — `(id, name)` → maps file IDs back to filesystem paths.
2. **`line`** — `(id, file, line)` → maps line-info IDs to source
   positions. Ignore `span`, `type`, `count` in v1.
3. **`sym`** — for each, extract:
   - `name`
   - the scope chain (`scope` → walk `scope.parent` to root; the
     concatenation of `scope.name`s with `::` is the qualified name)
   - definition sites = each `line` referenced by `def`
   - reference sites = each `line` referenced by `ref`
   - `type=lab|equ` → "this is a real definition"
   - `type=imp` → "this is an import; follow `exp` for the canonical
     definition in another module"

`scope`, `mod`, `seg`, `span`, `csym`, `type`, `lib` can wait for M2.
`scope` is needed before M1 wraps because cheap-local symbols only carry
`parent=<sym-id>`, not a scope ID — so the indexer must resolve the
parent symbol's scope chain.

## Validation log

This spec was validated against a real `.dbg` (3408 lines, 25 sym
records, 11 segments, 2 scopes, 72 files, 3114 lines, 1 lib) produced
by:

```sh
brew install cc65          # cc65 2.19, October 2025 formula
git clone --depth 1 https://github.com/cc65/cc65 /tmp/cc65-research
cd /tmp/cc65-research/samples
make SYS=c64 hello.o           # builds hello.s from hello.c
ld65 --dbgfile hello.dbg -o hello.prg -t c64 hello.o c64.lib
```

The first 200 lines of that file were inspected and every field shape
described above was confirmed against `src/ld65/{dbgfile,dbgsyms,
scopes,segments,span,fileinfo,lineinfo,library,objdata}.c` at
`master`. The only field-shape surprise was the `info` line's `file=711`
count when only 72 `file` records appear — appears to be a
pre-dedup-count quirk of the linker; the spec treats `info` counts as
advisory.
