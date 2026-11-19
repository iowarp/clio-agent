"""The marketplace visuals plugin lists the tools that build a view.

A built-in that declares nothing is deferred (``tools.defer_builtins``). The surface
tools once declared nothing, so no agent saw ``create_a2ui_surface`` or the figure
guidance gated on it, and a map answer arrived as a static PNG.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

MODULE = (
    Path(__file__).resolve().parents[2]
    / "external/clio-agent-marketplace/plugins/visuals/ai.iowarp.clio/tools/builtin.py"
)


def _declarations() -> dict[str, bool | None]:
    spec = importlib.util.spec_from_file_location("visuals_builtin_under_test", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {
        ref.name: ref.defer
        for ref in vars(module).values()
        if hasattr(ref, "name") and hasattr(ref, "defer")
    }


def test_building_a_surface_is_listed_and_the_rest_deferred() -> None:
    declared = _declarations()
    for name in ("create_a2ui_surface", "update_a2ui_components", "update_a2ui_data_model"):
        assert declared[name] is False, name
    for name in ("inspect_a2ui_surface", "capture_a2ui_surface", "delete_a2ui_surface"):
        assert declared[name] is True, name
