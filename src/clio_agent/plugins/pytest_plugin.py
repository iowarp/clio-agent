"""A pytest fixture that runs ``clio-agent plugin check`` on a plugin or marketplace.

Enable it with ``pytest_plugins = ["clio_agent.plugins.pytest_plugin"]``::

    def test_marketplace_resolves(plugin_check):
        report = plugin_check(Path(__file__).parents[1])
        assert report.ok, report.render()

The fixture uses an isolated plugin store under the test's temporary folder,
seeded with the plugins that ship in clio-agent, so a check never depends on
what the developer has installed.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from clio_agent.plugins.check import CheckReport, check
from clio_agent.plugins.service import PluginService
from clio_agent.plugins.store import PluginStore


@pytest.fixture
def plugin_check(tmp_path: Path) -> Callable[..., CheckReport]:
    """Return ``check(path)`` bound to an isolated store; see the module docstring."""

    service = PluginService(PluginStore(tmp_path / "plugin-store"))

    def run(path: Path, *, installed: str = "") -> CheckReport:
        return check(path, installed=installed, service=service)

    run.service = service  # type: ignore[attr-defined]
    return run
