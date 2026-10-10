"""Model-facing spawn schemas honor the owner's existing nullable defaults."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.test_gact.test_spawn_runtime_s4 import (
    _active_turn,
    _capture_emits,
    _fake_app,
    _InvokeSpy,
    _tools_by_name,
)


@pytest.mark.parametrize(
    "optional",
    [
        {"blueprint_id": None},
        {"placement": None},
        {"input_task_ids": None},
        {"blueprint_id": None, "placement": None, "input_task_ids": None},
    ],
    ids=["blueprint", "placement", "inputs", "all"],
)
def test_model_can_submit_nullable_spawn_options(
    optional: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise DSPy argument validation before the actual spawn closure/owner."""
    app = _fake_app()
    app.state.expert_invoker = _InvokeSpy()
    emitted = _capture_emits(monkeypatch)
    with _active_turn(app):
        tool = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)["spawn_agent_task"]
        result = json.loads(tool(agent="data_expert", task="Inspect owned data", **optional))
    assert result["accepted"] is True
    assert result["handle"] == "task_via_invoker"
    assert result["description"] == "Inspect owned data"
    assert len(app.state.expert_invoker.specs) == 1
    spec = app.state.expert_invoker.specs[0]
    assert spec.child_expert_id == "data_expert"
    assert spec.target_blueprint_id == ""
    assert spec.placement == "local"
    assert len(emitted) == 1


def test_model_can_inherit_batch_placement(monkeypatch: pytest.MonkeyPatch) -> None:
    """A nullable placement has the same owner behavior for batch submission."""
    app = _fake_app()
    app.state.expert_invoker = _InvokeSpy()
    _capture_emits(monkeypatch)
    with _active_turn(app):
        tool = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)["spawn_agents_parallel"]
        result = json.loads(
            tool(spawns=[{"agent": "data_expert", "task": "Inspect owned data"}], placement=None)
        )
    assert result["spawned"][0]["accepted"] is True
    assert len(app.state.expert_invoker.specs) == 1
    assert app.state.expert_invoker.specs[0].placement == "local"


@pytest.mark.parametrize(
    ("invalid", "message"),
    [
        ({"task": None}, "is invalid"),
        ({"blueprint_id": 1}, "valid string"),
        ({"input_task_ids": "task_x"}, "valid list"),
    ],
    ids=["task", "blueprint", "inputs"],
)
def test_invalid_spawn_arguments_are_rejected_before_submission(
    invalid: dict[str, Any], message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nullable options do not permit invalid types or null required assignments."""
    app = _fake_app()
    app.state.expert_invoker = _InvokeSpy()
    _capture_emits(monkeypatch)
    with _active_turn(app):
        tool = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)["spawn_agent_task"]
        arguments = {"agent": "data_expert", "task": "Inspect owned data", **invalid}
        with pytest.raises(ValueError, match=message):
            tool(**arguments)
    assert app.state.expert_invoker.specs == []


def test_empty_placement_remains_an_unaccepted_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only null inherits placement; an explicit empty string must still fail."""
    app = _fake_app()
    app.state.expert_invoker = _InvokeSpy()
    _capture_emits(monkeypatch)
    with _active_turn(app):
        tool = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)["spawn_agent_task"]
        result = json.loads(tool(agent="data_expert", task="Inspect owned data", placement=""))
    assert result["error"] == "invalid_placement"
    assert "accepted" not in result and "handle" not in result
    assert app.state.expert_invoker.specs == []
