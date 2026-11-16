"""A session created with ``flow`` runs it as its own work; unattended runs never wait.

Covers the server half of running agents and flows from a script:
``POST /v1/sessions`` with ``flow`` (admitted through the same check as ``run_flow``),
the reserved metadata keys, an unattended run's Questions, the session-tree usage
route, and the run limits.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.test_flows.conftest import assistant_messages, post, text_of, wait_until


def _create(client: TestClient, **body: Any) -> Any:
    return client.post("/v1/sessions", json={"title": "sdk", **body})


def _answer(client: TestClient, sid: str) -> dict[str, Any]:
    return wait_until(lambda: (assistant_messages(client, sid) or [None])[0], timeout=60)


def test_flow_on_create_runs_as_the_sessions_own_work(flow_app: TestClient) -> None:
    created = _create(
        flow_app,
        agent={"id": "clio/orchestrator"},
        flow={"id": "fanout", "options": {}},
        unattended=True,
    )
    assert created.status_code in (200, 201), created.text
    session = created.json()
    assert session["metadata"]["agent_flow"] == {"id": "fanout", "options": {}}
    assert session["metadata"]["unattended"] is True
    post(flow_app, session["id"], "the survey")
    answer = text_of(_answer(flow_app, session["id"]))
    assert answer.count("done: part") == 3, answer


def test_flow_refusals_match_run_flow(flow_app: TestClient) -> None:
    unknown = _create(flow_app, agent={"id": "clio/orchestrator"}, flow={"id": "no-such-flow"})
    assert unknown.status_code == 422
    assert unknown.json()["error"]["error"] == "flow_not_resolved"
    assert "no-such-flow" in unknown.json()["error"]["message"]

    bad = _create(
        flow_app, agent={"id": "clio/orchestrator"}, flow={"id": "budgeted", "options": {"x": 1}}
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["error"] == "flow_options_invalid"


def test_run_flow_and_create_share_one_refusal(flow_app: TestClient) -> None:
    from clio_agent.gact.agents.resolution import resolve_for_session
    from clio_agent.gact.flow_tools import flow_refusal

    resolution = resolve_for_session(flow_app.app, "clio/orchestrator")
    refused = flow_refusal(resolution, "budgeted", {"x": 1})
    assert refused is not None and refused["reason"] == "flow_options_invalid"
    assert flow_refusal(resolution, "fanout", None) is None


def test_agent_flow_and_run_keys_cannot_come_through_metadata(flow_app: TestClient) -> None:
    for key in ("agent_flow", "unattended", "run_limits"):
        refused = _create(flow_app, metadata={key: {"id": "fanout", "depth": 3}})
        assert refused.status_code == 422, key
        assert refused.json()["error"]["error"] == "metadata_reserved"
    sid = _create(flow_app).json()["id"]
    patched = flow_app.patch(f"/v1/sessions/{sid}", json={"metadata": {"agent_flow": {}}})
    assert patched.status_code == 422
    assert patched.json()["error"]["error"] == "metadata_reserved"


def test_unattended_flow_question_is_answered_at_once(flow_app: TestClient) -> None:
    sid = _create(flow_app, agent={"id": "clio/askerer"}, unattended=True).json()["id"]
    post(flow_app, sid, "go")
    # ctx.ask(schema=bool) got the not-available reply (declined, no option chosen).
    assert text_of(_answer(flow_app, sid)) == "declined"
    questions = flow_app.get(f"/v1/sessions/{sid}/questions").json()["questions"]
    assert questions and all(q["status"] != "pending" for q in questions)


def test_attended_flow_question_still_waits(flow_app: TestClient) -> None:
    from clio_agent.gact.unattended import is_unattended

    sid = _create(flow_app, agent={"id": "clio/askerer"}).json()["id"]
    assert not is_unattended(flow_app.app, sid)


def test_usage_route_sums_the_session_tree(flow_app: TestClient) -> None:
    sid = _create(flow_app, agent={"id": "clio/orchestrator"}, flow={"id": "fanout"}).json()["id"]
    post(flow_app, sid, "the survey")
    _answer(flow_app, sid)
    usage = flow_app.get(f"/v1/sessions/{sid}/usage").json()
    assert usage["session_id"] == sid
    assert usage["sessions"] >= 4  # the run and its three children
    assert {"turns", "tokens", "cost_usd", "by_model"} <= usage.keys()
    assert flow_app.get("/v1/sessions/sess_missing/usage").status_code == 404


def test_run_limit_fails_the_next_step_typed(flow_app: TestClient) -> None:
    import pytest

    from clio_agent.gact.run_limits import RunLimitReached, check_before_step

    sid = _create(flow_app, max_turns=1).json()["id"]
    app = flow_app.app
    assert app.state.sessions.get(sid).metadata["run_limits"] == {"max_turns": 1}
    check_before_step(app, sid)  # the first model step is within the limit
    with pytest.raises(RunLimitReached) as caught:
        check_before_step(app, sid)
    assert caught.value.error_type == "run_limit_reached"
    assert caught.value.details["limit"] == "max_turns"
    reached = app.state.sessions.get(sid).metadata["run_limit_reached"]
    assert reached["limit"] == "max_turns"
    with pytest.raises(RunLimitReached):  # every later step in the tree fails the same way
        check_before_step(app, sid)


def test_unlimited_session_is_never_stopped(flow_app: TestClient) -> None:
    from clio_agent.gact.run_limits import check_before_step

    sid = _create(flow_app).json()["id"]
    for _ in range(5):
        check_before_step(flow_app.app, sid)
