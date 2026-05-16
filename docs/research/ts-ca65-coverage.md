# tree-sitter-ca65 directive coverage

## Source of truth

The repository named in the plan, `github.com/babasbot/tree-sitter-ca65`, no
longer exists (HTTP 404; the original `babasbot` namespace is gone, and the
referenced username has since been renamed). Four community forks/successors
are publicly reachable:

| Repo | Updated | grammar.js LoC | Notes |
|------|--------:|---------------:|-------|
| `pogyomo/tree-sitter-ca65` | 2025-09-11 | 2007 | Most comprehensive; full pseudo-instruction coverage; HEAD `b22ead1` |
| `buckynbrocko/tree-sitter-ca65` | 2024-01-18 | 734 | Decent directive coverage, has `global_scope_access` (but spelled `#::`, which is wrong) |
| `LLeny/tree-sitter-ca65` | 2024-05-01 | 218 | Minimal; covers mnemonics + `.proc` + `.macro` only |
| `techwritescode/tree-sitter-ca65` | 2024-12-21 | 69 | Toy/stub, paired with their `techwritescode/ca65-lsp` |

**Recommendation:** vendor **pogyomo/tree-sitter-ca65 @ b22ead1** as the
baseline grammar. It is the only fork that models cc65's full directive set
with named fields the indexer can extract. All coverage below is against
pogyomo unless stated otherwise. URLs in this doc reference
`raw.githubusercontent.com/pogyomo/tree-sitter-ca65/b22ead17aa23636d39decb060b34de75e960397f/grammar.js`
(abbreviated `<pogyomo-raw>` below) and the official CA65 docs at
`cc65.github.io/doc/ca65.html`.

## Per-construct coverage matrix

| CA65 construct | pogyomo node | Coverage | Notes |
|---|---|---|---|
| `.proc Name … .endproc` (nestable) | `pseudo_inst_proc` | **full** | Has `name`, optional `:abs|:far|:zp` addr-spec, recursive `pseudo_inst_block` body — nesting works |
| `.scope [Name] … .endscope` | `pseudo_inst_scope` | **full** | Name is optional (anonymous scope) per CA65 spec; addr-spec optional |
| `.macro Name p1,… .endmacro` | `pseudo_inst_macro` | **full** | Plus `.mac`/`.endmac` aliases via keyword regex |
| Macro invocations | `macro_inst` (statement position), `macro_call` (expression position) | **partial** | Both are just `identifier (args…)` — indistinguishable from labels/symbols without context resolution. Parser must reconcile via the symbol table. |
| `.struct Name … .endstruct` | `pseudo_inst_struct` | **full** | Fields tagged as `pseudo_inst_struct_or_union_field` with `name` + `alloc` |
| `.union Name … .endunion` | `pseudo_inst_union` | **full** | Same field model as `.struct` |
| `Struct::field` (member access) | `member` | **partial** | Only `src::dst`. **No support for leading `::name` (global scope operator)** — see Gap #1 below. |
| `.import` / `.importzp` | `pseudo_inst_import` / `pseudo_inst_importzp` | **full** | Comma-list of `pseudo_inst_import_symbol` with optional `:spec` |
| `.export` / `.exportzp` | `pseudo_inst_export` / `pseudo_inst_exportzp` | **full** | Comma-list; supports `name : spec` and `name = expr` forms |
| `.global` / `.globalzp` | `pseudo_inst_global` / `pseudo_inst_globalzp` | **full** | Comma-list of `symbol` |
| `.segment "NAME"` | `pseudo_inst_segment` | **full** | `name` field is `_expression`, accepts `"NAME"` string literal; optional `:spec` |
| `.zeropage` / `.bss` / `.data` / `.code` / `.rodata` | `pseudo_inst_zeropage` etc. | **full** | Each is its own dot_keyword leaf node |
| Cheap local labels `@foo:` | `local_label` (declaration), `local_label_literal` (reference) | **full** | Defn `seq("@", identifier, ":")`; ref `seq("@", identifier)` in expressions |
| Anonymous label `:` | `unnamed_label` | **full** | Bare `:` as declaration |
| Anonymous ref `:+`, `:++`, `:-`, `:--` | `unnamed_label_literal` | **full** | `prec.left(seq(":", _inc | _dec))` — arbitrary `+`/`-` count via recursion |
| `::name` (leading namespace token) | — | **missing** | Not in grammar. CA65 spec says preceded by `::` means "search global scope"; see Gap #1. |
| `.if expr` | `pseudo_inst_if` | **full** | Body via `_pseudo_inst_if_common` which handles `.else`/`.elseif`/`.endif` |
| `.ifdef` / `.ifndef` | `pseudo_inst_ifdef` / `pseudo_inst_ifndef` | **full** | |
| `.ifblank` / `.ifnblank` / `.ifconst` / `.ifref` / `.ifnref` | each has its own rule | **full** | Plus `.ifp02`/`.ifp816`/etc. for CPU variants |
| `.else` / `.elseif` / `.endif` | `pseudo_inst_else`, `pseudo_inst_elseif`, `dot_keyword_endif` | **full** | |
| `.enum [Name] … .endenum` | `pseudo_inst_enum` | **partial** | Grammar **requires** a name (`field("name", $.symbol)`). CA65 allows anonymous `.enum` blocks (which dump members into enclosing scope) — see Gap #2. |
| `.set` (reassignable symbol) | `symbol_set` | **full** | Form `name .set expr` |
| `=` / `:=` (symbol equate / pinned-address assign) | `symbol_eq` / `symbol_assign` | **full** | |
| `.define` (textual macro) | `pseudo_inst_define` | **full** | Both bare and `name(params)` forms; body is `/.*/` regex (opaque to ts) |
| `.assert cond,action[,msg]` | `pseudo_inst_assert` | **full** | Actions limited to warning/error/ldwarning/lderror |
| 6502 mnemonics | `opcode_adc` … `opcode_tya` (56 rules) | **full** | All standard 6502 ops; case-insensitive |
| 65C02 mnemonics (`bra`, `phx`, `phy`, `plx`, `ply`, `stz`, `tsb`, `trb`, `bbr/bbs/rmb/smb`) | — | **missing** | None present in opcode list — see Gap #3 |
| 65816 mnemonics (`brl`, `cop`, `jml`, `jsl`, `mvn`, `mvp`, `pea`, `pei`, `per`, `rep`, `sep`, `rtl`, etc.) | — | **missing** | None present — see Gap #3 |
| `.setcpu "6502X"` / `.p02` / `.pc02` / `.p816` etc. | `pseudo_inst_setcpu`, `pseudo_inst_p02`, `pseudo_inst_pc02`, `pseudo_inst_p816`, … | **full** | CPU-switch directives are recognized even though the mnemonics for those CPUs are not |
| String literals `"…"` | `string` | **full** | `seq('"', /[^"]*/, '"')` — no escape support |
| Character literals `'x'` | `char` | **full** | Single character only |
| Numeric literals — decimal | `_number_dec` | **full** | `/[0-9]+/` |
| Numeric literals — hex `$ff` | `_number_hex` | **full** | `seq("$", /[a-fA-F0-9]+/)` |
| Numeric literals — binary `%1010` | `_number_bin` | **full** | `seq("%", /[0-1]+/)` |
| Numeric underscore separators (`feature underline_in_numbers`) | — | **missing** | The `.feature` flag is parsed; the actual `1_000` literal is not |
| Line comments `;` | `comment` | **full** | `seq(";", /.*/)` as extras |
| Block comments `/* … */` | — | **missing** | Only available when `.feature c_comment` is enabled; not in grammar |
| Expressions (unary/binary, prec table) | `unary_expression`, `binary_expression`, `_primary_expression`, `group_expression` | **full** | Mirrors CA65's `< > ^ ~ + - * /` etc. with correct precedences |
| Pseudo-variables (`*`, `.cpu`, `.time`, `.paramcount`, etc.) | `pseudo_var` | **full** | |
| Pseudo-functions (`.sizeof`, `.lobyte`, `.bank`, `.match`, etc.) | `pseudo_func_*` (28 rules) | **full** | |

### Grammar rule citations

Key rule definitions in pogyomo's `grammar.js` (`<pogyomo-raw>#L<N>`):

- `pseudo_inst_proc` — L1100 ([raw](https://raw.githubusercontent.com/pogyomo/tree-sitter-ca65/b22ead17aa23636d39decb060b34de75e960397f/grammar.js))
- `pseudo_inst_scope` — L1161
- `pseudo_inst_macro` — L1031
- `pseudo_inst_struct` — L1192
- `pseudo_inst_union` — L1209
- `pseudo_inst_segment` — L1176
- `pseudo_inst_import` — L919 / `pseudo_inst_export` — L627
- `pseudo_inst_global` / `globalzp` — L738 / L747
- `pseudo_inst_enum` — L599
- `pseudo_inst_if`/`ifdef`/`ifndef` — L801 / L826 / L843
- `pseudo_inst_assert` — L442
- `local_label` — L96; `unnamed_label` — L97; `unnamed_label_literal` — L1946
- `member` (`a::b`) — L1939
- `string`/`char`/`number` literals — L1937 / L1938 / L1933
- Opcode table (6502 only) — L120-L177, then `opcode_adc` … `opcode_tya` definitions L240-L295

## Top-5 grammar gaps for the Parser engineer

1. **No leading `::name` global-scope operator.** CA65's
   `cc65.github.io/doc/ca65.html#ss5.4` documents that a leading `::`
   forces the lookup into the global scope (e.g. `lda #::bar` inside
   `.scope foo`). pogyomo's `member` rule (L1939) only models `src::dst`
   binary access; buckynbrocko's `global_scope_access` only fires after a
   leading `#`, which is wrong. **Workaround:** post-process expression
   sub-trees and re-tokenise any `::ident` that didn't match `member`;
   long-term, contribute a `global_member` alternative upstream.

2. **65C02 / 65816 / Rockwell / HuC6280 mnemonics are missing.** Only
   the 56 standard 6502 opcodes are enumerated (L120-L177). For the eight
   c64-* corpora this matters less (the C64's 6510 is 6502-compatible),
   but for any Apple IIgs / SNES / NES-MMC5 / PCE code these instructions
   will be parsed as `macro_inst` (unresolved identifier) or as `ERROR`
   nodes when followed by a register operand. The directive
   `.setcpu "65C02"` is recognised but does not enable new mnemonics in
   the parser. **Workaround:** during `documentSymbol`/`references`,
   filter out tokens whose identifier matches a known 65C02/65816
   mnemonic before promoting them to symbols. Long-term contribution:
   parametrise `actual_inst` on a CPU mode (LLeny's grammar already
   enumerates these — could be lifted).

3. **`.enum` requires a name; anonymous `.enum` errors out.** Grammar
   `pseudo_inst_enum` at L599 sets `field("name", $.symbol)` as required.
   CA65 explicitly allows anonymous enums that bleed members into the
   enclosing scope (often used for state flags). **Workaround:** scan
   sources for the literal regex `.enum\s*\r?\n` and synthesize a
   placeholder `name` node; the indexer must still create the members in
   the enclosing scope.

4. **Statement-position macro invocation is ambiguous with cheap-local
   reference and label declaration.** Lines 102-115: `_inst` chooses
   between `macro_inst | actual_inst | _pseudo_inst`, and `macro_inst`
   accepts any identifier with optional comma-separated raw args. So a
   stray `foo` on a line parses as `macro_inst foo`. The indexer's
   resolver must mark these as `kind=may-be-macro` and reconcile only
   after the project-wide macro table is loaded. (See `Risk register #4`
   in the plan — this is exactly that risk's grammar root cause.)

5. **Block comments `/* … */` and `.feature underline_in_numbers` are
   not modelled.** `.feature c_comment` is recognised as a directive but
   its body-changing effect is not. Workspaces that enable this feature
   (cc65's own `samples/cbm/cbm.s` does not, but `c64-aes256-ecdsa` may)
   will break the lexer mid-file. **Workaround:** strip block comments
   in a pre-pass when `.feature c_comment` is observed at top-of-file;
   contribute an external-scanner upstream.

### Minor gaps not in the top-5

- String escapes — `"\n"` parses as 4 characters; the `.feature
  string_escapes` flag is recognised but the inner regex `[^"]*` doesn't
  treat backslash specially.
- Address-spec inside `.import name : zp` after `.feature
  leading_dot_in_identifiers` is enabled — pogyomo's identifier regex
  `/[a-zA-Z_][0-9a-zA-Z_]*/` (L1959) rejects identifiers starting with
  `.`.
- `.feat at_in_identifiers` / `dollar_in_identifiers` — identifier regex
  rejects `@`/`$` mid-identifier.
- `.include "foo.inc"` body is **not** transitively parsed; the buffer
  layer's symbol table needs the indexer to follow includes itself.

## Bottom line for the Parser engineer

Adopt pogyomo @ `b22ead1`. Expect to write a 50-200-line post-pass over
the parse tree that (a) re-resolves `::name` global references,
(b) downgrades 65C02/65816 mnemonics that landed in `macro_inst` nodes
back into instruction nodes, (c) recognises anonymous `.enum` blocks,
and (d) tags ambiguous identifier-only statements as `may-be-macro`.
Upstream contributions for #1, #2, #3 are worth filing — they're small
and the maintainer is active (most recent commit Sept 2025).
