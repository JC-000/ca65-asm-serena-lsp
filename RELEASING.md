# Releasing `ca65-ls`

## Current state: install-from-git only

`ca65-ls` is **not on PyPI** yet, because its `tree-sitter-ca65` grammar
dependency is also not on PyPI and PyPI policy rejects packages whose
dependencies use direct (`git+https://…`) URLs.

For now users install from git:

```sh
pip install "ca65-ls @ git+https://github.com/JC-000/ca65-asm-serena-lsp@main#subdirectory=packages/ca65-ls"
```

## Unblocking PyPI publication

We need `tree-sitter-ca65` available on PyPI before we can publish `ca65-ls`.
Two ways to get there:

### Option A (preferred): upstream PyPI publish

1. Tracking issue:
   [pogyomo/tree-sitter-ca65#1](https://github.com/pogyomo/tree-sitter-ca65/issues/1)
   — asks the maintainer to publish to PyPI; offers three levels of help
   (they publish; we contribute a Trusted-Publisher GH Actions workflow;
   we co-maintain on PyPI under their grant).
2. When the maintainer publishes (or grants us co-maintainer rights to
   publish), update `pyproject.toml`'s `tree-sitter-ca65` line:
     ```toml
     "tree-sitter-ca65>=0.X.Y",  # was: "tree-sitter-ca65 @ git+…@b22ead1"
     ```
   pinned to whatever version corresponds to commit `b22ead1`.
3. Then proceed to "Publishing to PyPI" below.

### Option B (fallback): vendor the grammar

If upstream PyPI publish doesn't happen on a timeline we like, vendor
`tree-sitter-ca65`'s grammar source into `packages/ca65-ls/vendor/` and
build it as part of `ca65-ls`'s own wheel.  Concretely:

1. Copy `grammar.js`, `src/` (the `tree-sitter generate` output — `parser.c`,
   `tree_sitter/` headers), and `LICENSE` from
   `pogyomo/tree-sitter-ca65@b22ead1` into `packages/ca65-ls/vendor/`.
2. Add a `hatch_build.py` (or equivalent) that compiles `parser.c` into a
   shared library and includes it in the wheel.
3. Update `ca65_ls/buffer/document.py`'s `_get_language()` to load the
   vendored library instead of importing the `tree_sitter_ca65` package.
4. Drop the `tree-sitter-ca65 @ git+…` dependency.

Option B is more work but fully under our control.

## Publishing to PyPI

Once the dependency is sorted:

1. Bump `version` in `packages/ca65-ls/pyproject.toml`.
2. Update `CHANGELOG.md` (TBD — add one if absent) with the release notes.
3. Commit, then create a tag matching the format `ca65-ls-vX.Y.Z`:
     ```sh
     git tag -a ca65-ls-v0.1.0 -m "ca65-ls v0.1.0"
     git push origin ca65-ls-v0.1.0
     ```
4. The
   [`publish-ca65-ls.yml`](.github/workflows/publish-ca65-ls.yml) workflow
   builds the sdist + wheel and publishes via PyPI's
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
