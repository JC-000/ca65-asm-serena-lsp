# Releasing `ca65-ls`

> **v0.1.0 status:** release content is frozen (CHANGELOG dated 2026-07-20);
> the `ca65-ls-v0.1.0` tag is intentionally NOT pushed yet because the PyPI
> trusted publisher is not registered — pushing it now would fail the publish
> job. Once registered on pypi.org, push the tag and the workflow does the rest.

## Current state: publishable, first release pending

The former PyPI blocker is **resolved (2026-07-20)**: the `tree-sitter-ca65`
grammar is now **vendored** into `packages/ca65-ls/vendor/tree-sitter-ca65/`
(pinned upstream commit `b22ead1`, MIT) and compiled into the wheel as the
abi3 C extension `ca65_ls._grammar._binding`. There are no git-URL
dependencies left, so PyPI will accept the package. Provenance and the
update-to-newer-grammar procedure live in
`packages/ca65-ls/vendor/tree-sitter-ca65/NOTICE.md`.

Until the first release is tagged, users install from git:

```sh
pip install "ca65-ls @ git+https://github.com/JC-000/ca65-asm-serena-lsp@main#subdirectory=packages/ca65-ls"
```

Build notes:

- Build backend is **setuptools** (`setup.py` defines the extension;
  metadata stays in `pyproject.toml`). The extension targets the CPython
  limited API, so each platform needs exactly one `cp310-abi3` wheel that
  covers Python 3.10+.
- The publish workflow builds wheels for linux x86_64/aarch64, macOS
  universal2, and Windows AMD64 via `cibuildwheel`, plus an sdist (which
  contains the vendored C sources and builds anywhere with a C compiler).

### If upstream ever publishes to PyPI (de-vendoring)

The ask is still open at
[pogyomo/tree-sitter-ca65#1](https://github.com/pogyomo/tree-sitter-ca65/issues/1).
If a `tree-sitter-ca65` PyPI release appears and we prefer it: add
`"tree-sitter-ca65>=0.X.Y"` back to `dependencies`, point
`ca65_ls/buffer/document.py` back at `import tree_sitter_ca65`, delete
`vendor/`, `setup.py`, `MANIFEST.in`, and `ca65_ls/_grammar/`, and the
package becomes pure-Python again (build backend can return to hatchling).
Not urgent — vendoring is self-sufficient.

## Publishing to PyPI

Once the dependency is sorted:

1. Bump `version` in `packages/ca65-ls/pyproject.toml`.
2. Update `packages/ca65-ls/CHANGELOG.md` with the release notes (move the
   "unreleased" heading to the version/date).
3. Commit, then create a tag matching the format `ca65-ls-vX.Y.Z`:
     ```sh
     git tag -a ca65-ls-v0.1.0 -m "ca65-ls v0.1.0"
     git push origin ca65-ls-v0.1.0
     ```
4. The
   [`publish-ca65-ls.yml`](.github/workflows/publish-ca65-ls.yml) workflow
   builds the sdist + per-platform abi3 wheels (cibuildwheel) and publishes via PyPI's
   [trusted-publisher OIDC flow](https://docs.pypi.org/trusted-publishers/).
   **Before the first release**, register this repo + workflow under
   "Trusted Publishers" on PyPI for the `ca65-ls` project.  No API token
   needs to live in GitHub secrets.

## Versioning

Tags use the `ca65-ls-vX.Y.Z` prefix to leave room for other packages in
this monorepo to use their own tag schemes later.  `vX.Y.Z` follows
semver:

- **MAJOR** bumps on incompatible LSP-API changes that would break
  Serena's `Ca65LanguageServer` shim.
- **MINOR** bumps on new LSP capabilities (e.g. when we add semantic
  tokens, code actions, etc.).
- **PATCH** bumps on bug fixes / perf / internal cleanups.
