"""Server-side ``clio.chart.v1`` spec/preset validation (#1549 G3).

Before this module's fix, ``clio_schemas.a2ui.chart_spec.check_chart_spec``
(the SAME guard the gact-tui renderer runs) was never consulted server-side:
a chart spec that broke a rule the catalog's JSON Schema cannot express (byte
size, view count, nesting depth) came back from ``create_a2ui_surface`` as
``rendered: true`` and only failed client-side -- invisible to the agent that
produced it. A bad preset parameter (unknown field, missing required field,
a malformed ``xType``) was the same story, one layer earlier: the preset was
never even expanded server-side to check. These tests drive
:mod:`clio_agent.gact.a2ui_producer._chart_spec` through the real producer
tools.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact import context as gact_context
from clio_agent.gact.a2ui_capabilities import remember_client_capabilities
from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.a2ui_producer import (
    build_create_a2ui_surface_tool,
    build_update_a2ui_components_tool,
)
from clio_agent.gact.app import build_app

WORKSPACE_ID = workspace_catalog_id()

_ROWS = [{"t": 0, "v": 1.0}, {"t": 1, "v": 2.0}]
_XY_ENCODING = {
    "x": {"field": "t", "type": "quantitative"},
    "y": {"field": "v", "type": "quantitative"},
}


def _producer_session(tmp_path: Path, monkeypatch: Any) -> tuple[Any, str, Path]:
    """Build an app + session in-process (no HTTP/ARC pipeline needed).

    Mirrors ``test_a2ui_producer.py``'s own ``_session`` helper: the seeded
    default workspace is rebound to this test's ``tmp_path`` and the session
    is created directly through the store, exactly the same path the
    producer tools themselves read through ``gact.context``. Driving session/
    workspace creation through the HTTP routes instead (as
    ``test_a2ui_data_reference.py`` does) also works, but pulls in the ARC
    semantic-event pipeline these tests have no need of.
    """

    from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities  # noqa: PLC0415

    app = build_app(sessions_path=tmp_path / "sessions.json")
    app.state.workspaces.update("ws_default", root_path=str(tmp_path))
    session = app.state.sessions.create(workspace_id="ws_default", title="chart guard")
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: session.id)
    caps = A2UIClientCapabilities.model_validate({"v0.9": {"supportedCatalogIds": [WORKSPACE_ID]}})
    remember_client_capabilities(app, session.id, caps)
    return app, session.id, tmp_path


def _create(components: list[dict[str, Any]]) -> dict[str, Any]:
    return build_create_a2ui_surface_tool()(surface_id="surface", components=components)


def _chart(spec: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    component: dict[str, Any] = {
        "id": "root",
        "component": "clio.chart.v1",
        "data": _ROWS,
        **extra,
    }
    if spec is not None:
        component["spec"] = spec
    return component


# --------------------------------------------------------------------------- #
# Inline spec: rules the catalog's JSON Schema cannot express
# --------------------------------------------------------------------------- #


def test_oversize_spec_is_a_typed_refusal(tmp_path: Path, monkeypatch: Any) -> None:
    _app, _sid, _root = _producer_session(tmp_path, monkeypatch)

    spec = {
        "data": {"name": "source"},
        "mark": "point",
        "encoding": _XY_ENCODING,
        # Comfortably past MAX_SPEC_BYTES (65536) once serialized.
        "description": "x" * 70_000,
    }
    result = _create([_chart(spec)])

    assert result["ok"] is False
    assert result["reason"] == "a2ui_chart_spec_rejected"
    assert "spec_too_large" in result["detail"]
    assert result["hint"]


def test_too_many_views_is_a_typed_refusal(tmp_path: Path, monkeypatch: Any) -> None:
    _app, _sid, _root = _producer_session(tmp_path, monkeypatch)

    # MAX_VIEWS is 8; nine hconcat'd units is one too many.
    spec = {"hconcat": [{"mark": "point", "encoding": _XY_ENCODING} for _ in range(9)]}
    result = _create([_chart(spec)])

    assert result["ok"] is False
    assert result["reason"] == "a2ui_chart_spec_rejected"
    assert "too_many_views" in result["detail"]
    assert result["hint"]


def test_forbidden_key_is_a_typed_refusal(tmp_path: Path, monkeypatch: Any) -> None:
    _app, _sid, _root = _producer_session(tmp_path, monkeypatch)

    spec = {
        "layer": [
            {
                "mark": "point",
                "encoding": _XY_ENCODING,
                # "usermeta" is forbidden at ANY depth -- a nested occurrence
                # like this one is NOT expressible as a JSON Schema
                # propertyNames enum (only the top-level key allowlist is),
                # so it must be caught here.
                "usermeta": {"anything": True},
            }
        ]
    }
    result = _create([_chart(spec)])

    assert result["ok"] is False
    assert result["reason"] == "a2ui_chart_spec_rejected"
    assert "forbidden_key" in result["detail"]
    assert "/layer/0/usermeta" in result["detail"]
    assert result["hint"]


def test_valid_spec_passes(tmp_path: Path, monkeypatch: Any) -> None:
    _app, _sid, _root = _producer_session(tmp_path, monkeypatch)

    spec = {"data": {"name": "source"}, "mark": "point", "encoding": _XY_ENCODING}
    result = _create([_chart(spec)])

    assert result.get("ok") is not False, result
    assert result["rendered"] is True


def test_update_a2ui_components_also_runs_the_chart_guard(tmp_path: Path, monkeypatch: Any) -> None:
    _app, _sid, _root = _producer_session(tmp_path, monkeypatch)

    good_spec = {"data": {"name": "source"}, "mark": "point", "encoding": _XY_ENCODING}
    created = _create([_chart(good_spec)])
    assert created.get("ok") is not False, created

    bad_spec = {"hconcat": [{"mark": "point", "encoding": _XY_ENCODING} for _ in range(9)]}
    result = build_update_a2ui_components_tool()(
        surface_id="surface", components=[_chart(bad_spec)]
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_chart_spec_rejected"
    assert "too_many_views" in result["detail"]


# --------------------------------------------------------------------------- #
# Preset parameters: validated server-side before the preset ever renders
# --------------------------------------------------------------------------- #


def test_bad_preset_parameter_is_a_typed_refusal(tmp_path: Path, monkeypatch: Any) -> None:
    _app, _sid, _root = _producer_session(tmp_path, monkeypatch)

    # The "scatter" preset requires xField, yField AND entityField --
    # entityField is missing here.
    result = _create([_chart(preset="scatter", xField="t", yField="v")])

    assert result["ok"] is False
    assert result["reason"] == "a2ui_chart_preset_invalid"
    assert "entityField" in result["detail"]
    assert result["hint"]


def test_unknown_preset_field_is_a_typed_refusal(tmp_path: Path, monkeypatch: Any) -> None:
    _app, _sid, _root = _producer_session(tmp_path, monkeypatch)

    # "boxplot" does not use facetField (only colorField/selectionParam are
    # optional) -- passing it anyway is rejected, not silently ignored.
    result = _create(
        [
            _chart(
                preset="boxplot",
                xField="t",
                yField="v",
                entityField="run",
                facetField="extra",
            )
        ]
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_chart_preset_invalid"
    assert "facetField" in result["detail"]


def test_valid_preset_passes(tmp_path: Path, monkeypatch: Any) -> None:
    _app, _sid, _root = _producer_session(tmp_path, monkeypatch)

    result = _create([_chart(preset="scatter", xField="t", yField="v", entityField="t")])

    assert result.get("ok") is not False, result
    assert result["rendered"] is True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
