"""Conversation ownership remains distinct from provenance parentage."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.permission_delivery import attended_session_id
from clio_agent.gact.protocol.v3.session import session_to_v3
from clio_agent.gact.session_kind import session_kind
from clio_agent.gact.sessions import Session


@pytest.mark.parametrize(
    ("parent", "agent", "metadata", "expected"),
    [
        ("", {"id": "main"}, {}, "conversation"),
        ("root", {"id": "main"}, {}, "branch"),
        ("root", {"id": "expert"}, {"session_kind": "branch"}, "branch"),
        ("root", {"id": "main", "mode": "subagent"}, {}, "agent"),
        ("root", {"id": "main"}, {"session_type": "agent_task"}, "agent"),
        ("root", {"id": "main"}, {"side_session": {"read_only": True}}, "lookup"),
        ("root", {}, {}, "agent"),
    ],
)
def test_session_projection_distinguishes_ownership(
    parent: str, agent: dict[str, str], metadata: dict[str, Any], expected: str
) -> None:
    record = Session(
        "session", "workspace", "title", parent_session_id=parent, agent=agent, metadata=metadata
    )
    assert session_kind(record) == expected
    assert session_to_v3(record)["session_kind"] == expected
    assert "metadata" not in session_to_v3(record)


def test_branch_and_its_children_attend_branch_without_losing_parentage() -> None:
    records = {
        "root": Session("root", "workspace", "Original"),
        "branch": Session("branch", "workspace", "Branch", parent_session_id="root"),
        "child": Session(
            "child",
            "workspace",
            "Worker",
            parent_session_id="branch",
            agent={"id": "expert", "mode": "subagent"},
        ),
    }
    app = SimpleNamespace(state=SimpleNamespace(sessions=SimpleNamespace(get=records.get)))
    assert attended_session_id(app, "branch") == "branch"
    assert attended_session_id(app, "child") == "branch"
    assert records["branch"].parent_session_id == "root"
