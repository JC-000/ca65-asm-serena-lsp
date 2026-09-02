"""Through-Serena layer: start ca65-ls via SolidLSP exactly as Serena does.

Runs only under the fork venv (`~/Documents/serena/.venv`), where both
`solidlsp` and `ca65_ls` are importable; elsewhere every test skips. Serena's
per-project cache is redirected to a temp directory so nothing is written
into a project's `.serena/`.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

solidlsp = pytest.importorskip("solidlsp", reason="through-Serena tests need the fork venv")

from solidlsp import SolidLanguageServer  # noqa: E402
from solidlsp.ls_config import LanguageServerConfig, LanguageServerId  # noqa: E402
from solidlsp.settings import SolidLSPSettings  # noqa: E402

FIXTURE = Path(__file__).parent.parent / "fixtures" / "test_repo"


def pytest_configure(config):
    config.addinivalue_line("markers", "serena: tests that drive ca65-ls through SolidLSP")


def pytest_collection_modifyitems(items):
    here = Path(__file__).parent
    for item in items:
        if here in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.serena)


@contextmanager
def started(
    project_root: Path, data_dir: Path, *, ignored: list[str] | None = None, timeout: float = 120.0
) -> Iterator[SolidLanguageServer]:
    """Mirror of the fork's test/conftest.py::_create_ls for LanguageServerId.CA65."""
    config = LanguageServerConfig(
        ls_id=LanguageServerId.CA65,
        ignored_paths=list(ignored or []),
        trace_lsp_communication=False,
        workspace_folders=["."],
        additional_workspace_folders=[],
    )
    (data_dir / "project").mkdir(parents=True, exist_ok=True)
    (data_dir / "home").mkdir(parents=True, exist_ok=True)
    ls = SolidLanguageServer.create(
        config,
        str(project_root),
        timeout=timeout,
        solidlsp_settings=SolidLSPSettings(
            solidlsp_dir=str(data_dir / "home"),
            project_data_path=str(data_dir / "project"),
            ls_specific_settings={},
        ),
    )
    with ls.start_server_context():
        yield ls


@pytest.fixture
def fixture_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "test_repo"
    shutil.copytree(FIXTURE, dst)
    return dst


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "serena-data"


def write_project(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root
