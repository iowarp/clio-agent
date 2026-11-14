"""GG-F1: a ``clio.mcp-app.v1`` component's ``arguments`` skip the presentation-key scan.

They are the opening tool's own call arguments (Geo's layer ``style``), never
rendered, so ``style`` inside them passes. Everywhere else in the surface,
including the same component's other properties, the scan still refuses it.
The arguments keep a size and a nesting bound, each a typed refusal.
"""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.gact.a2ui import (
    MAX_A2UI_DEPTH,
    A2UIValidationError,
    max_a2ui_string_chars,
    validate_server_message,
)
from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.a2ui_catalogs.registry import CatalogRegistry
from clio_agent.gact.a2ui_catalogs.validation import (
    MAX_MCP_APP_ARGUMENT_BYTES,
    A2UIMcpAppArgumentsError,
    validate_value,
)
from clio_agent.gact.a2ui_producer import _common
from clio_agent.gact.a2ui_producer._refusal import KNOWN_REFUSAL_REASONS
from clio_agent.gact.mcp_app_calls import MAX_SAVED_ARGUMENT_BYTES

_CATALOGS = CatalogRegistry()
_ENTRY = _CATALOGS.get(workspace_catalog_id())

GLOBE_ARGUMENTS: dict[str, Any] = {
    "title": "GNSS stations near Mount Rainier",
    "layers": [
        {
            "name": "stations",
            "path": "stations.geojson",
            "style": {"color": "#d62728", "marker_size": 7},
            "color_by": "kind",
        },
        {
            "name": "trench",
            "geojson": {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"id": "trench", "style": {"stroke": "#1f77b4"}},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[-125.0, 48.0], [-124.6, 46.0]],
                        },
                    }
                ],
            },
            "style": {"color": "navy", "width": 2},
        },
    ],
    "style": "dark",
    "url": "file:///tmp/not-a-surface-url",
}


def _app(component_id: str = "globe", **extra: Any) -> dict[str, Any]:
    return {
        "id": component_id,
        "component": "clio.mcp-app.v1",
        "server": "geo",
        "tool": "show_on_globe",
        "arguments": GLOBE_ARGUMENTS,
        "state": [{"key": "selected_feature", "path": "/selection/station"}],
        **extra,
    }


def _validate(*components: dict[str, Any]) -> None:
    validate_server_message(
        {
            "version": "v0.9.1",
            "updateComponents": {"surfaceId": "dash", "components": list(components)},
        },
        catalogs=_CATALOGS,
        catalog_entry=_ENTRY,
    )


def _walk(*components: dict[str, Any]) -> None:
    """The safety walk alone, so a key the schema would also refuse reaches it."""

    validate_value(
        {"surfaceId": "dash", "components": list(components)},
        entry=_ENTRY,
        max_depth=MAX_A2UI_DEPTH,
        max_string=max_a2ui_string_chars(),
    )


def _deep(levels: int) -> Any:
    value: Any = "leaf"
    for _ in range(levels):
        value = {"style": value}
    return value


def test_style_inside_app_arguments_passes_at_every_depth() -> None:
    _walk(_app())


@pytest.mark.skipif(
    "clio.mcp-app.v1" not in _ENTRY.validators,
    reason="the installed clio-schemas catalog predates clio.mcp-app.v1",
)
def test_a_whole_dashboard_message_with_styled_app_arguments_passes() -> None:
    _validate(
        {"id": "root", "component": "Row", "children": ["globe", "text"]},
        _app(),
        {"id": "text", "component": "Text", "text": "Stations"},
    )


def test_a_forbidden_key_elsewhere_in_the_same_surface_is_still_refused() -> None:
    for key in ("style", "html", "script"):
        text = {"id": "text", "component": "Text", "text": "x", key: "y"}
        with pytest.raises(A2UIValidationError, match=f"prohibited: {key}"):
            _walk(_app(), text)


def test_a_forbidden_key_on_the_app_components_other_properties_is_still_refused() -> None:
    # On the component itself, beside arguments.
    with pytest.raises(A2UIValidationError, match="prohibited: style"):
        _walk(_app(style={"color": "red"}))
    # Inside a state channel of the same component.
    component = _app()
    component["state"] = [{"key": "selected_feature", "path": "/selection/s", "html": "<b>"}]
    with pytest.raises(A2UIValidationError, match="prohibited: html"):
        _walk(component)
    # And an arguments key on a component of another type is scanned as before.
    with pytest.raises(A2UIValidationError, match="prohibited: script"):
        _walk({"id": "t", "component": "Text", "text": "x", "arguments": {"script": "x"}})


def test_a_lookalike_app_object_in_the_data_model_gets_no_exemption() -> None:
    with pytest.raises(A2UIValidationError, match="prohibited: script"):
        validate_server_message(
            {
                "version": "v0.9.1",
                "updateDataModel": {
                    "surfaceId": "dash",
                    "path": "/x",
                    "value": {
                        "components": [
                            {"component": "clio.mcp-app.v1", "arguments": {"script": "x"}}
                        ]
                    },
                },
            },
            catalogs=_CATALOGS,
            catalog_entry=_ENTRY,
        )


def test_oversized_arguments_are_refused_with_their_typed_reason() -> None:
    assert MAX_MCP_APP_ARGUMENT_BYTES == MAX_SAVED_ARGUMENT_BYTES
    rows = [{"id": f"s{index}", "style": {"color": "#d62728"}} for index in range(3000)]
    with pytest.raises(A2UIMcpAppArgumentsError) as caught:
        _walk(_app(arguments={"features": rows}))
    assert caught.value.reason == "a2ui_mcp_app_arguments_too_large"
    assert caught.value.component_id == "globe"
    assert "the limit is 65536" in str(caught.value)


def test_a_string_over_the_string_limit_is_refused_as_too_large() -> None:
    with pytest.raises(A2UIMcpAppArgumentsError) as caught:
        _walk(_app(arguments={"geojson": "x" * 20000}))
    assert caught.value.reason == "a2ui_mcp_app_arguments_too_large"


def test_too_deep_arguments_are_refused_with_their_typed_reason() -> None:
    _walk(_app(arguments=_deep(16)))
    with pytest.raises(A2UIMcpAppArgumentsError) as caught:
        _walk(_app(arguments=_deep(19)))
    assert caught.value.reason == "a2ui_mcp_app_arguments_too_deep"


def test_the_producer_reports_the_typed_reason_with_a_hint(monkeypatch: Any) -> None:
    class _Store:
        def apply_batch_outcome(self, *_args: Any, **_kwargs: Any) -> Any:
            raise A2UIMcpAppArgumentsError("a2ui_mcp_app_arguments_too_deep", "globe", "nest")

    app = type("App", (), {"state": type("State", (), {"a2ui_store": _Store()})()})()
    outcome = _common.apply_messages(app, "sess_x", [], catalog_id=workspace_catalog_id())
    assert isinstance(outcome, dict)
    assert outcome["reason"] == "a2ui_mcp_app_arguments_too_deep"
    assert outcome["hint"]
    assert {
        "a2ui_mcp_app_arguments_too_large",
        "a2ui_mcp_app_arguments_too_deep",
    } <= KNOWN_REFUSAL_REASONS
