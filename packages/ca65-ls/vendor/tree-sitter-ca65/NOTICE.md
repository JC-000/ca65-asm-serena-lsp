# Vendored: tree-sitter-ca65

Vendored copy of the CA65 tree-sitter grammar, compiled into `ca65-ls` as the
internal extension module `ca65_ls._grammar._binding`.

- **Origin:** https://github.com/pogyomo/tree-sitter-ca65
- **Commit:** `b22ead17aa23636d39decb060b34de75e960397f` (2025-09-11, "fix: correctly parse expression")
- **License:** MIT, as declared by upstream in its `pyproject.toml`
  (`license.text = "MIT"`), `package.json`, `Cargo.toml`, and
  `tree-sitter.json`. The upstream repository ships no standalone LICENSE
  text file; the declarations in those manifests are the license grant.
- **Author:** pogyomo

## What is vendored

- `grammar.js` — the grammar definition (reference; needed to regenerate the parser)
- `src/parser.c`, `src/grammar.json`, `src/node-types.json` — `tree-sitter generate` output
- `src/tree_sitter/*.h` — runtime headers shipped with the generate output
- `bindings/python/binding.c` — upstream's Python binding shim (unmodified;
  compiled here as `ca65_ls._grammar._binding` — the `PyInit__binding` entry
  point matches the extension's final path component)

Queries (`queries/*.scm`) and the other language bindings are intentionally
not vendored — ca65-ls only needs `language()`.

## Why vendored

PyPI rejects packages whose dependencies use direct git URLs, and upstream has
no PyPI release (asked in
[pogyomo/tree-sitter-ca65#1](https://github.com/pogyomo/tree-sitter-ca65/issues/1),
no response). Vendoring removes ca65-ls's only git-URL dependency. If upstream
ever publishes to PyPI, this vendor dir can be dropped and the dependency
restored — see RELEASING.md.

## How to update to a newer upstream commit

```sh
git clone https://github.com/pogyomo/tree-sitter-ca65 /tmp/ts-ca65
git -C /tmp/ts-ca65 checkout <NEW_COMMIT>
cd packages/ca65-ls/vendor/tree-sitter-ca65
cp /tmp/ts-ca65/grammar.js .
cp /tmp/ts-ca65/src/{parser.c,grammar.json,node-types.json} src/
cp /tmp/ts-ca65/src/tree_sitter/*.h src/tree_sitter/
cp /tmp/ts-ca65/bindings/python/tree_sitter_ca65/binding.c bindings/python/
# if upstream ever adds src/scanner.c, copy it too — setup.py picks it up automatically
```

Then update the commit hash in this file, rebuild (`uv pip install -e ".[dev]"`),
and run the test suite. Check `docs/research/ts-ca65-coverage.md` for whether
the new commit closes any of the documented grammar gaps.
