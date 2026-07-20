# Changelog

## 0.1.0 — unreleased

First release.

- **LSP features:** document symbols (hierarchical: procs, scopes, labels,
  cheap locals, macros, structs/unions, enums, imports/exports), go to
  definition, find references (scope-aware: cheap locals and scope-local
  labels stay confined to their routine), hover, rename (scope-aware, with
  kind-preserving prefix rules), workspace symbol search.
- **Parsing:** tree-sitter-ca65 grammar (pogyomo/tree-sitter-ca65 @
  `b22ead1`), vendored and compiled into the wheel as an abi3 C extension —
  works on any CA65 codebase with no build artifacts required.
- **Enrichment:** when a cc65 build links with `ld65 --dbgfile`, the `.dbg`
  file supplies post-link addresses, segment membership, and symbol sizes
  (surfaced via the LSP `detail` field). Caches auto-invalidate when build
  artifacts change.
- **Tools:** `ca65-ls --stdio` (the server), `ca65-dbg-dump` (dump a `.dbg`
  as JSON).
- Designed for [Serena MCP](https://github.com/oraios/serena) integration
  (via the `JC-000/serena` fork's `Ca65LanguageServer`), but usable as a
  plain LSP server by any editor.
