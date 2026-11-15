"""A plugin server's ${PLUGIN_ROOT} / ${PLUGIN_DATA}, with or without a default."""

from __future__ import annotations

from clio_agent.plugins.servers import _expand


def test_plugin_paths_replace_the_bare_and_the_defaulted_forms() -> None:
    root, data = "/plugins/kicad", "/data/kicad"
    assert _expand("${PLUGIN_DATA}/tools", root, data, {}) == "/data/kicad/tools"
    # A connector entry writes a default so its Extensions probe (no plugin) does not fail;
    # inside the plugin the plugin's own folder wins.
    assert _expand("${PLUGIN_DATA:-}", root, data, {}) == "/data/kicad"
    assert _expand("${PLUGIN_ROOT:-/x}/bin", root, data, {}) == "/plugins/kicad/bin"
    assert _expand("${OTHER:-y}", root, data, {}) == "${OTHER:-y}"  # left for the generic pass
