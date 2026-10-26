"""Authored flow child names reach the existing conversation identity boundary."""

from __future__ import annotations

from typing import cast

import pytest
from clio_agent.flows.call_metadata import child_session_metadata
from clio_agent.flows.context import FlowCall, FlowContext


@pytest.mark.parametrize(
    "kind,target", [("agent", "clio/standard"), ("subagent", "self"), ("flow", "council")]
)
def test_named_child_keeps_its_execution_metadata(kind: str, target: str) -> None:
    """Naming a worker must not drop self isolation or nested flow configuration."""
    call = FlowCall(
        cast(FlowContext, None),
        {"id": "council", "options": {"models": ["account/model"]}},
        kind,
        target,
        "assignment",
        "invocation",
        None,
        label="  Integrity review  ",
    )
    metadata = child_session_metadata(call, 2, "run-parent")
    assert metadata is not None
    assert metadata["subagent_name"] == "Integrity review"
    if kind == "flow":
        assert metadata["agent_flow"]["id"] == "council"
        assert metadata["agent_flow"]["options"] == {"models": ["account/model"]}
    elif target == "self":
        assert metadata["agent_flow"]["id"] == "react"
        assert metadata["agent_flow"]["parent_run"] == "run-parent"


def test_named_editor_keeps_its_editing_rights() -> None:
    """A display name supplements rather than replaces the editor capability context."""
    call = FlowCall(
        cast(FlowContext, None),
        "self",
        "subagent",
        "self",
        "review",
        "inv",
        None,
        label="Review validation",
    )
    call.editor = {"run_id": "run-parent", "step_id": "validation"}
    assert child_session_metadata(call, 2, "run-parent") == {
        "subagent_name": "Review validation",
        "workflow_editor": {"run_id": "run-parent", "step_id": "validation"},
    }


def test_unnamed_agent_does_not_invent_identity_metadata() -> None:
    """Existing callers that do not supply a name preserve their metadata contract."""
    call = FlowCall(
        cast(FlowContext, None), "clio/standard", "agent", "clio/standard", "task", "inv", None
    )
    assert child_session_metadata(call, 1, "run") is None
