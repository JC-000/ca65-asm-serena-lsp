"""Guards the seam that replaced the in-tree `LanguageServerId.CA65` enum member.

CA65 is registered from *outside* the Serena tree, through the
`solidlsp.language_server_registration` entry point declared in ca65-ls's own
pyproject.  Two things about that arrangement can break silently, which is why they
are asserted here rather than left to the end-to-end tests:

  * `ls_specific_settings` is keyed by the string "ca65" instead of an enum member.
    `SolidLSPSettings.get_ls_specific_settings` resolves `ls_id.get_key()` first, so
    the string works -- but if it did not, every setting would quietly become a
    no-op and every existing test would still pass, because none of them assert
    that a configured value actually took effect.
  * `register_ca65()` must be idempotent (the entry point and our conftest both call
    it) without masking a genuine registration failure.
"""

from __future__ import annotations

import pytest
from solidlsp import SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig
from solidlsp.settings import SolidLSPSettings

from ca65_ls.serena_adapter import Ca65LanguageServer, register_ca65

from .conftest import CA65_ID, create


def test_settings_keyed_by_string_actually_reach_the_language_server(fixture_copy, data_dir):
    """A non-default `initialize_timeout` must be observable on the adapter.

    Asserting against DEFAULT_INITIALIZE_TIMEOUT is the point: a no-op settings path
    would leave the effective value at the default and look perfectly healthy.
    """
    ls = create(fixture_copy, data_dir, settings={"initialize_timeout": 3})
    adapter = getattr(ls, "language_server", ls)
    assert adapter.DEFAULT_INITIALIZE_TIMEOUT != 3, "test is vacuous if 3 is the default"
    assert adapter._initialize_timeout() == 3  # noqa: SLF001 (no public getter)


def test_object_keyed_settings_are_a_no_op(fixture_copy, data_dir):
    """Negative control for the test above: keying by the id OBJECT must not work.

    `SolidLSPSettings.get_ls_specific_settings` consults the object-keyed entry only
    when the id `isinstance`s as a `LanguageServerId` enum member, which an
    `ExternalLanguageServerId` never does. So a revert to the old enum-style keying
    would silently drop every setting. Without this case the happy-path test above
    would still pass if someone "helpfully" keyed the dict both ways, and the no-op
    would go unnoticed.
    """
    ls = SolidLanguageServer.create(
        LanguageServerConfig(ls_id=CA65_ID),
        str(fixture_copy),
        solidlsp_settings=SolidLSPSettings(
            solidlsp_dir=str(data_dir / "home"),
            project_data_path=str(data_dir / "project"),
            ls_specific_settings={CA65_ID: {"initialize_timeout": 3}},
        ),
    )
    adapter = getattr(ls, "language_server", ls)
    assert adapter._initialize_timeout() == adapter.DEFAULT_INITIALIZE_TIMEOUT  # noqa: SLF001


def test_registry_resolves_ca65_to_our_adapter():
    assert CA65_ID.get_key() == "ca65"
    assert CA65_ID.get_ls_class() is Ca65LanguageServer


@pytest.mark.parametrize(
    ("filename", "relevant"),
    [("a.s", True), ("a.asm", True), ("a.inc", True), ("a.py", False), ("a.txt", False)],
)
def test_matcher_claims_assembly_only(filename: str, relevant: bool):
    assert CA65_ID.get_source_fn_matcher().is_relevant_filename(filename) is relevant


def test_register_is_idempotent():
    """Already called at conftest import; calling again must not raise."""
    register_ca65()
    register_ca65()
