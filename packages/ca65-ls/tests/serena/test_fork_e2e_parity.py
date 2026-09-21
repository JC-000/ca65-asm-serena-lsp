"""Parity port of the fork's `test/solidlsp/ca65/test_ca65_basic.py`.

Kept here so that moving CA65 out of the Serena tree did not lose the
end-to-end coverage against the bundled ca65-ls daemon.

The fork original also carried a `skipif` that disabled these tests when the `ca65`
binary was absent, inherited from Serena's generic conftest, which gates every
language server on its toolchain being installed. That guard is deliberately NOT
ported: these five tests are served entirely from the tree-sitter parse, and all
five were verified to pass with cc65 removed from PATH. Only the `.dbg` enrichment
path needs the toolchain, and none of these exercise it.
"""

from pathlib import Path

from .conftest import started


class TestCa65LanguageServer:
    """End-to-end tests against the bundled ca65-ls daemon."""

    def test_ls_is_running(self, fixture_copy: Path, data_dir: Path) -> None:
        with started(fixture_copy, data_dir) as ls:
            assert ls.is_running()
            # The fork original used `language_server.language_server.repository_root_path`
            # because Serena's fixture returned a wrapper. `started()` yields the
            # SolidLanguageServer directly, so we access the attribute directly here.
            assert Path(ls.repository_root_path).resolve() == fixture_copy.resolve()

    def test_document_symbols_helpers_scope(self, fixture_copy: Path, data_dir: Path) -> None:
        """`get_symbols_overview` on helpers.s must surface the .scope and its nested .procs."""
        with started(fixture_copy, data_dir) as ls:
            file_path = str(Path("src") / "helpers.s")
            symbols = ls.request_document_symbols(file_path).get_all_symbols_and_roots()
            _all_symbols, root_symbols = symbols

            root_names = [s.get("name") for s in root_symbols]
            # The synthetic corpus has a .struct S, a .macro mac1, and a .scope helpers
            # all at top level — every one must be visible to Serena.
            assert "helpers" in root_names, f"missing 'helpers' scope; got {root_names}"
            assert "S" in root_names, f"missing 'S' struct; got {root_names}"
            assert "mac1" in root_names, f"missing 'mac1' macro; got {root_names}"

            # The helpers scope must expose its nested .procs as children
            helpers = next(s for s in root_symbols if s.get("name") == "helpers")
            child_names = [c.get("name") for c in (helpers.get("children") or [])]
            assert "foo" in child_names, f"helpers.children missing 'foo'; got {child_names}"
            assert "bar" in child_names, f"helpers.children missing 'bar'; got {child_names}"

    def test_definition_crosses_module_boundary(self, fixture_copy: Path, data_dir: Path) -> None:
        """`request_definition` on `jsr lib_export` in main.s must jump to lib.s."""
        with started(fixture_copy, data_dir) as ls:
            main_path = str(Path("src") / "main.s")
            # main.s line 15 (1-indexed) is `        jsr     lib_export` -- 0-indexed: line 14.
            # `lib_export` starts at column 16 (8-space indent + "jsr" + 5-space gap).
            definitions = ls.request_definition(main_path, 14, 18)
            assert definitions, f"expected definitions, got {definitions}"
            # The defining file must be lib.s -- crossing the .import boundary.
            uris = {d["uri"] for d in definitions}
            assert any(u.endswith("src/lib.s") for u in uris), f"got {uris}"

    def test_references_finds_cross_file_use(self, fixture_copy: Path, data_dir: Path) -> None:
        """`request_references` on lib_export's definition in lib.s must find the use in main.s."""
        with started(fixture_copy, data_dir) as ls:
            lib_path = str(Path("src") / "lib.s")
            # lib.s line 14 (1-indexed) is `.proc lib_export` -- 0-indexed: line 13.
            # `lib_export` starts at column 6 (after ".proc ").
            references = ls.request_references(lib_path, 13, 8)
            assert references, f"expected references for lib_export, got {references}"
            uris = {r["uri"] for r in references}
            # At least one reference must be in main.s (the .import declarator or jsr site).
            assert any(u.endswith("src/main.s") for u in uris), f"references not in main.s: {uris}"

    def test_definition_of_proc_in_scope(self, fixture_copy: Path, data_dir: Path) -> None:
        """Goto-definition on `helpers_foo` in main.s resolves to the alias in helpers.s.

        The alias `.export helpers_foo := helpers::foo` is module-scope in helpers.s.
        Serena must be able to resolve the .import across the module boundary.
        """
        with started(fixture_copy, data_dir) as ls:
            main_path = str(Path("src") / "main.s")
            # main.s line 16 (1-indexed) `        jsr     helpers_foo` -- 0-indexed: line 15
            definitions = ls.request_definition(main_path, 15, 18)
            assert definitions, f"expected definitions, got {definitions}"
            uris = {d["uri"] for d in definitions}
            assert any(u.endswith("src/helpers.s") for u in uris), f"got {uris}"
