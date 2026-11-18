"""An install whose marketplace was read before ``follows`` existed is re-read on upgrade.

First-run journey (apps ledger, "First-run journey" JR-U1): a CLIO whose reader did not
know ``follows`` recorded the following plugin as invalid ("unknown ai.iowarp.clio
field(s): follows") and left it out. The reader changed without its version moving, so
the next CLIO kept that record: the connector got a skill-less carrier and the agent did
not know how to point it elsewhere. The reader's version now moves with ``follows``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from clio_agent.plugins import manifest as manifest_module
from clio_agent.plugins import store as store_module
from clio_agent.plugins.store import PluginStore

_MARKET = Path(__file__).resolve().parents[1] / "fixtures/connectors/marketplace"
_PLUGIN = "fixture-secret-skills"
#: The reader version every CLIO without ``follows`` recorded.
_PRE_FOLLOWS_READER = 2


def _old_reader_install(store: PluginStore, source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install ``source`` as a CLIO before ``follows`` would have read it."""

    with monkeypatch.context() as old:
        fields = tuple(f for f in manifest_module._EXTENSION_FIELDS if f != "follows")
        old.setattr(manifest_module, "_EXTENSION_FIELDS", fields)
        old.setattr(store_module, "READER_VERSION", _PRE_FOLLOWS_READER)
        store.install(str(source), name="clio", default=True)


def test_reader_version_moved_past_the_reader_without_follows() -> None:
    assert store_module.READER_VERSION > _PRE_FOLLOWS_READER


def test_an_install_read_without_follows_is_reread_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "market"
    shutil.copytree(_MARKET, source)
    store = PluginStore(tmp_path / "store")
    _old_reader_install(store, source, monkeypatch)

    before = store.read_state()
    assert f"clio/{_PLUGIN}" not in before["plugins"]
    assert any(
        "follows" in row.get("message", "") for row in before["marketplaces"]["clio"]["diagnostics"]
    )

    # The upgraded CLIO starts: the same snapshot is read again, without fetching.
    results = PluginStore(tmp_path / "store").reread_installed()

    assert [row["marketplace"] for row in results] == ["clio"]
    after = store.read_state()
    assert after["plugins"][f"clio/{_PLUGIN}"]["follows"] == "fixture-secret"
    assert not any(
        "follows" in row.get("message", "") for row in after["marketplaces"]["clio"]["diagnostics"]
    )
    assert after["marketplaces"]["clio"]["reader"] == store_module.READER_VERSION
    # Read once: the next start re-reads nothing.
    assert PluginStore(tmp_path / "store").reread_installed() == []
