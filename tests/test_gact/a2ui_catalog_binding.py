"""Test helper: give a session's agent extra A2UI catalogs through plugins.

An agent's A2UI catalogs are the ``catalogs`` rows of its pinned resolution: the
``clio-workspace`` builtin that ``core`` declares, then every catalog its other
plugins name (``ai.iowarp.clio.catalogs`` in ``plugin.json``) or ship
(``ai.iowarp.clio/a2ui/<name>/``), in plugin order. A default session runs
``clio/standard`` and so declares exactly ``[clio-workspace]``.

Tests that need more install one of the native plugin fixtures under
``tests/fixtures/a2ui_plugins`` into the app's plugin store and add it to the
session's live delta, exactly as the + menu does (``pin_session_agent``):

* ``a2ui-basic`` names the builtin Basic catalog -> ``[clio-workspace, basic]``;
* ``a2ui-minimal`` / ``a2ui-second`` / ``a2ui-own`` each ship one catalog of their
  own (``minimal``, ``second``, ``own``) aliasing Basic kernels under new names.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clio_agent.gact.agents.resolution import pin_session_agent, plugin_service
from clio_agent.plugins.resolve import Delta, Resolution

A2UI_PLUGINS = Path(__file__).resolve().parents[1] / "fixtures" / "a2ui_plugins"
BASIC_PLUGIN = "a2ui-basic"
MINIMAL_PLUGIN = "a2ui-minimal"
SECOND_PLUGIN = "a2ui-second"
OWN_PLUGIN = "a2ui-own"
MINIMAL_CATALOG_ID = "https://example.test/a2ui/catalogs/minimal"
SECOND_CATALOG_ID = "https://example.test/a2ui/catalogs/second"
OWN_CATALOG_ID = "https://example.test/a2ui/catalogs/own"


def catalog_dir(plugin_id: str, catalog: str) -> Path:
    """The fixture plugin's shipped catalog folder (``ai.iowarp.clio/a2ui/<catalog>``)."""

    return A2UI_PLUGINS / plugin_id / "ai.iowarp.clio" / "a2ui" / catalog


def install_plugins(app: Any, *plugin_ids: str, root: Path = A2UI_PLUGINS) -> None:
    """Install each plugin folder ``root/<id>`` into ``app``'s store as its own source.

    The installed-catalog cache (``app.state.a2ui_catalogs``) is invalidated the
    way the plugin routes do after an install.
    """

    store = plugin_service(app).store
    for plugin_id in plugin_ids:
        store.install(str(root / plugin_id), name=plugin_id)
    registry = getattr(app.state, "a2ui_catalogs", None)
    if registry is not None:
        registry.invalidate()


def bind_session_plugins(
    app: Any, session_id: str, *plugin_ids: str, root: Path = A2UI_PLUGINS
) -> Resolution:
    """Install ``plugin_ids`` and pin ``session_id`` to its agent plus those plugins."""

    install_plugins(app, *plugin_ids, root=root)
    return pin_session_agent(app, session_id, delta=Delta(add=tuple(plugin_ids)))


def bind_builtin_catalogs(app: Any, session_id: str) -> Resolution:
    """Bind ``session_id`` to its agent plus ``a2ui-basic``: ``[clio-workspace, basic]``."""

    return bind_session_plugins(app, session_id, BASIC_PLUGIN)


__all__ = [
    "A2UI_PLUGINS",
    "BASIC_PLUGIN",
    "MINIMAL_CATALOG_ID",
    "MINIMAL_PLUGIN",
    "OWN_CATALOG_ID",
    "OWN_PLUGIN",
    "SECOND_CATALOG_ID",
    "SECOND_PLUGIN",
    "bind_builtin_catalogs",
    "bind_session_plugins",
    "catalog_dir",
    "install_plugins",
]
