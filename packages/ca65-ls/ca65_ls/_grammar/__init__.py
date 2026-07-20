"""Vendored tree-sitter-ca65 grammar.

Exposes ``language()`` from the compiled ``_binding`` extension, mirroring the
API of the upstream ``tree_sitter_ca65`` package it replaces. Grammar sources
and provenance: ``vendor/tree-sitter-ca65/`` (see NOTICE.md there).
"""

from ca65_ls._grammar._binding import language

__all__ = ["language"]
