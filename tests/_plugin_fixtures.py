"""Give a test its own default marketplace: the hermetic fixture plus extra plugins and agents.

``custom_marketplace`` copies ``tests/fixtures/plugin_marketplace`` into ``tmp_path``,
adds the plugins and agent modules a test needs, and points the default marketplace at
the copy, so every app built afterwards installs it (trusted, as the shipped default
is) into the test's own plugin store::

    root = custom_marketplace(
        tmp_path,
        monkeypatch,
        plugins={"hdf5": {"files": {"ai.iowarp.clio/tools/t.py": tools_module("view_pdf")}}},
        agents={"data.py": AGENT_SOURCE},
    )
"""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path
from typing import Any

from tests.test_plugins.helpers import fragment, plugin, tools_module, write

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "plugin_marketplace"

__all__ = ["FIXTURE", "custom_marketplace", "fragment", "tools_module", "write"]


def custom_marketplace(
    tmp_path: Path,
    monkeypatch: Any,
    *,
    plugins: dict[str, dict[str, Any]] | None = None,
    agents: dict[str, str] | None = None,
    name: str = "marketplace",
) -> Path:
    """Build and install-as-default a marketplace for this test. Returns its root."""

    root = tmp_path / name
    shutil.copytree(FIXTURE, root, ignore=shutil.ignore_patterns("__pycache__"))
    for plugin_id, options in (plugins or {}).items():
        target = root / "plugins" / plugin_id
        if target.exists():
            shutil.rmtree(target)
        plugin(root / "plugins", plugin_id, **options)
    for filename, source in (agents or {}).items():
        write(root / "agents" / filename, textwrap.dedent(source))
    monkeypatch.setenv("CLIO_DEFAULT_MARKETPLACE", str(root))
    return root
