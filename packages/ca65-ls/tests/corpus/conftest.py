"""Corpus contract suite: runs the language server against the user's real
CA65 projects (see corpus.json) and checks invariants that must hold for the
symbolic tools to be trustworthy.

- Projects that are not present are skipped, so CI without the corpus passes.
- The index is built with ``cache=False`` so the suite never writes into the
  projects and never reads a stale cache.
- Select with ``-m corpus`` / deselect with ``-m "not corpus"``.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

import pytest

from ca65_ls.index.workspace import SOURCE_SUFFIXES, WorkspaceIndex, _iter_source_files
from ca65_ls.server import Ca65LanguageServer, _path_to_uri

HERE = Path(__file__).parent
CORPUS_ROOT = Path(os.environ.get("CA65_CORPUS_ROOT", Path.home() / "Documents")).expanduser()
PROJECTS: list[str] = json.loads((HERE / "corpus.json").read_text())["projects"]


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "corpus: contract tests against the real CA65 project corpus"
    )


def pytest_collection_modifyitems(items):
    for item in items:
        if HERE in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.corpus)


def _present(name: str) -> bool:
    return (CORPUS_ROOT / name).is_dir()


def pytest_generate_tests(metafunc):
    if "project_name" in metafunc.fixturenames:
        params = [
            pytest.param(
                name,
                marks=()
                if _present(name)
                else pytest.mark.skip(reason=f"{name} not under {CORPUS_ROOT}"),
            )
            for name in PROJECTS
        ]
        metafunc.parametrize("project_name", params, scope="session")


class Project:
    """One corpus project with a cache-free index and a server bound to it."""

    def __init__(self, root: Path):
        self.root = root
        self.index = WorkspaceIndex(root, cache=False)
        self.index.reindex()
        self.server = Ca65LanguageServer()
        # Bind the server to our cache-free index instead of add_workspace(),
        # which would build a second, cache-writing index.
        self.server.indexes[_path_to_uri(root)] = self.index
        self.server.workspace_roots.append(root)
        self.files: list[Path] = sorted(_iter_source_files(root))
        self._text: dict[Path, list[str]] = {}
        self.symbols = list(self.index.all_symbols())
        self.by_uri: dict[str, list] = defaultdict(list)
        for sym in self.symbols:
            self.by_uri[sym.uri].append(sym)

    def lines(self, path: Path) -> list[str]:
        if path not in self._text:
            self._text[path] = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return self._text[path]

    def uri(self, path: Path) -> str:
        return _path_to_uri(path)

    def path(self, uri: str) -> Path:
        from ca65_ls.server import _uri_to_path

        return _uri_to_path(uri)


_PROJECTS: dict[str, Project] = {}


def _load(name: str) -> Project:
    if name not in _PROJECTS:
        _PROJECTS[name] = Project(CORPUS_ROOT / name)
    return _PROJECTS[name]


@pytest.fixture(scope="session")
def project(project_name: str) -> Project:
    return _load(project_name)


@pytest.fixture(scope="session")
def corpus() -> list[Project]:
    """Every present corpus project. RED tests assert over the whole corpus so
    that a defect present in only some projects still gives one strict xfail
    that flips to a failure the moment it is fixed everywhere."""
    present = [_load(name) for name in PROJECTS if _present(name)]
    if not present:
        pytest.skip(f"no corpus projects under {CORPUS_ROOT}")
    return present


__all__ = ["SOURCE_SUFFIXES", "Project"]
