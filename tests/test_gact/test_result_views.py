"""Views a tool result declares are shown as it arrives, and the agent is told (RV-D1)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import result_views
from clio_agent.gact.a2ui_producer import create

PROFILES = {
    "component": "clio.profile-stack.v1",
    "title": "casts",
    "dataUri": "/ws/casts.parquet",
    "profileField": "profile",
    "verticalField": "pressure",
    "valueField": "temperature",
}
STRUCTURED = {
    "summary": "Read 3 casts.",
    "outputs": [
        {"path": "/ws/casts.parquet", "role": "profiles", "view": PROFILES},
        {"path": "/ws/summary.parquet", "role": "table"},
    ],
}


@pytest.fixture
def created(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def func(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"rendered": True, "revision": 1, "surface_id": kwargs["surface_id"]}

    monkeypatch.setattr(
        create, "build_create_a2ui_surface_tool", lambda: SimpleNamespace(func=func)
    )
    monkeypatch.setattr("clio_agent.tools.tool_observation.peek_call_identity", lambda: "call_1")
    return calls


def test_only_declared_component_views_count() -> None:
    assert result_views.declared_views(STRUCTURED) == [PROFILES]
    assert result_views.declared_views({"outputs": [{"view": {"title": "x"}}]}) == []
    assert result_views.declared_views("text") == []
    many = {"outputs": [{"view": PROFILES}] * 10}
    assert len(result_views.declared_views(many)) == result_views.MAX_VIEWS


def test_a_declared_view_is_shown_and_the_agent_is_told(created: list[dict[str, Any]]) -> None:
    result_views.show(None, "ocean_ctd_read", STRUCTURED)
    assert len(created) == 1
    components = created[0]["components"]
    assert components[0] == {"id": "root", "component": "Column", "children": ["view-1"]}
    assert components[1] == {**PROFILES, "id": "view-1"}
    assert created[0]["surface_id"].startswith("ctd-read-")
    text = result_views.merge_note("Read 3 casts.")
    assert text.startswith("Read 3 casts.\n\nCLIO showed this result to the person")
    assert "clio.profile-stack.v1 (casts)" in text and "capture_a2ui_surface" in text
    # Consumed once.
    assert result_views.merge_note("next") == "next"


def test_a_result_without_views_opens_nothing(created: list[dict[str, Any]]) -> None:
    result_views.show(None, "ocean_teos10", {"summary": "s", "outputs": [{"path": "x"}]})
    assert created == [] and result_views.merge_note("s") == "s"


def test_a_refused_view_is_said_with_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        create,
        "build_create_a2ui_surface_tool",
        lambda: SimpleNamespace(
            func=lambda **_k: {"error": "a2ui_path_outside_workspace", "detail": "/tmp/x"}
        ),
    )
    monkeypatch.setattr("clio_agent.tools.tool_observation.peek_call_identity", lambda: "call_2")
    result_views.show(None, "ocean_ctd_read", STRUCTURED)
    text = result_views.merge_note("done")
    assert "could not show the views" in text and "a2ui_path_outside_workspace: /tmp/x" in text


def test_a_note_never_rides_another_calls_result(
    created: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    result_views.show(None, "ocean_ctd_read", STRUCTURED)
    monkeypatch.setattr(
        "clio_agent.tools.tool_observation.peek_call_identity", lambda: "call_other"
    )
    assert result_views.merge_note("other") == "other"
