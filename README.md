# ca65-asm-serena-lsp

Exploration repo for building a CA65 assembly language plugin / LSP integration for the [Serena MCP](https://github.com/oraios/serena) toolkit.

## Goal

Give Serena symbolic-code-navigation capabilities (find_symbol, find_referencing_symbols, get_symbols_overview, rename_symbol, etc.) over CA65 assembly source — the dialect used by the [cc65](https://cc65.github.io/) toolchain for 6502/65C02/65816 targets (NES, C64, Apple II, Atari, etc.).

## Status

Early scoping. Nothing here yet beyond this README.

## Likely shape

Serena's symbolic tools are powered by language servers. Two plausible directions:

1. **Wrap an existing CA65 LSP** (if a usable one exists) and register it with Serena's `SolidLanguageServer` layer.
2. **Build a minimal CA65 language server** ourselves — enough to expose labels, macros, procs, scopes, imports/exports, and includes as LSP symbols. CA65's grammar is small; a Tree-sitter or hand-rolled parser plus an LSP shim is tractable.

Decision deferred until we've surveyed prior art.
