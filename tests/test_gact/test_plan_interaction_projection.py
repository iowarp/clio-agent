"""Real Plan questions retain the nested review contract consumed by the UI."""

from pathlib import Path

import pytest

from tests.test_gact.test_plan_exit import ApprovalWorld

pytest_plugins = ("tests.test_gact.test_plan_exit",)
pytestmark = pytest.mark.usefixtures("native_builtin_lifespan")


def test_pending_plan_exposes_complete_review_under_plan_exit(
    approval_world: ApprovalWorld,
) -> None:
    """Project the confirmed saved review instead of orphaned top-level fields."""
    response = approval_world.client.get(
        f"/v1/sessions/{approval_world.sid}/interactions",
        params={"include_children": "true"},
    )
    assert response.status_code == 200, response.text
    question = next(
        row
        for row in response.json()["interactions"]
        if row["task_id"] == approval_world.question.handle
    )
    payload = question["payload"]
    plan = payload["plan_exit"]
    assert plan["plan_content_status"] == "complete"
    assert plan["plan_content"] == Path(plan["plan_file"]).read_text(encoding="utf-8")
    assert plan["summary"]
    assert plan["artifact_ref"]["saved"] is True
    assert "plan_content" not in payload
    assert question["requires_human_response"] is True
