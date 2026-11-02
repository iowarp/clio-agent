"""The session plan: plan_enter, plan_update, status, working state, fork, judge, rail."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.app import build_app
from clio_agent.gact.compaction_policy import KeepPolicy, post_compaction_context
from clio_agent.gact.goal_evidence import _build_transcript
from clio_agent.gact.session_plan import (
    PLAN_STATUS_KEY,
    PlanError,
    enter_plan,
    plan_injection,
    rail_plan,
    update_plan,
)
from clio_agent.gact.session_plan_presentation import present
from clio_agent.gact.working_state import working_state_injections

pytestmark = pytest.mark.usefixtures("host_agent_executor")

_PLAN = "# Objective\nFind the regression.\n\n## Threads\n- bisect: open\n"


@pytest.fixture
def app_session(tmp_path: Path) -> Any:
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "regression hunt"}).json()["id"]
        yield app, sid, client


def _session(app: Any, sid: str) -> Any:
    return app.state.sessions.get(sid)


def test_plan_enter_switches_to_plan_mode_with_a_draft(app_session: Any) -> None:
    app, sid, _ = app_session
    text = enter_plan(app, sid, "a long investigation")
    session = _session(app, sid)
    assert session.mode == "plan"
    assert session.metadata[PLAN_STATUS_KEY] == "draft"
    assert session.metadata["plan_file"].endswith(".md")
    assert "plan_update(content=...)" in text


def test_plan_update_writes_replaces_and_closes(app_session: Any) -> None:
    app, sid, _ = app_session
    enter_plan(app, sid)
    written = update_plan(app, sid, content=_PLAN)
    assert (written["change"], written["status"]) == ("whole", "draft")
    replaced = update_plan(app, sid, old="- bisect: open", new="- bisect: closed (commit 4)")
    assert replaced["change"] == "passage"
    path = Path(_session(app, sid).metadata["plan_file"])
    assert "bisect: closed (commit 4)" in path.read_text(encoding="utf-8")
    with pytest.raises(PlanError) as missing:
        update_plan(app, sid, old="not there", new="x")
    assert missing.value.reason == "plan_passage_missing"
    with pytest.raises(PlanError):
        update_plan(app, sid, status="finished")
    assert update_plan(app, sid, status="done")["status"] == "done"


def test_plan_update_works_outside_plan_mode(app_session: Any) -> None:
    app, sid, _ = app_session
    result = update_plan(app, sid, content=_PLAN)
    assert result["status"] == "approved"
    assert _session(app, sid).mode != "plan"


def test_an_active_plan_is_working_state_outside_plan_mode(app_session: Any) -> None:
    app, sid, _ = app_session
    enter_plan(app, sid)
    update_plan(app, sid, content=_PLAN)
    # In plan mode the plan-mode reminder names the file; the plan itself is not repeated.
    sources = [s for s, _ in working_state_injections(app, sid, _session(app, sid))]
    assert "plan_mode" in sources and "plan" not in sources
    app.state.sessions.update(sid, mode="edit", metadata_patch={PLAN_STATUS_KEY: "approved"})
    text = plan_injection(app, sid, _session(app, sid))
    assert text.startswith("## The plan (approved, ")
    assert "continue on it, and update it with plan_update" in text and "bisect" in text
    update_plan(app, sid, status="abandoned")
    assert plan_injection(app, sid, _session(app, sid)) == ""


def test_the_rail_and_the_goal_judge_see_the_plan_and_todos(app_session: Any) -> None:
    app, sid, client = app_session
    update_plan(app, sid, content=_PLAN)
    todos = [{"content": "bisect the range", "status": "in_progress"}]
    app.state.sessions.update(sid, metadata_patch={"todos": todos})
    work = client.get(f"/v1/sessions/{sid}/work").json()
    assert work["plan"]["status"] == "approved" and "bisect" in work["plan"]["text"]
    assert rail_plan(SimpleNamespace(metadata={})) is None
    transcript = _build_transcript(app, sid)
    assert transcript.startswith("--- session plan (approved) ---")
    assert "bisect the range" in transcript


def test_a_fork_gets_its_own_copy_of_the_plan_and_the_todos(app_session: Any) -> None:
    from clio_agent.gact.conversation_copy import create_branch

    app, sid, _ = app_session
    update_plan(app, sid, content=_PLAN)
    app.state.sessions.update(
        sid, metadata_patch={"todos": [{"content": "a", "status": "pending"}]}
    )
    branch = create_branch(app, _session(app, sid))
    source_path = _session(app, sid).metadata["plan_file"]
    branch_path = branch.metadata["plan_file"]
    assert branch_path != source_path
    assert Path(branch_path).read_text(encoding="utf-8") == Path(source_path).read_text(
        encoding="utf-8"
    )
    assert branch.metadata["todos"] == [{"content": "a", "status": "pending"}]
    assert branch.metadata[PLAN_STATUS_KEY] == "approved"


def _segment(seg_id: str, kind: str, turn: str, content: dict[str, Any]) -> Any:
    return SimpleNamespace(id=seg_id, kind=kind, turn_id=turn, content=content)


def test_compaction_keeps_the_latest_working_state_verbatim() -> None:
    live = [
        _segment("todo1", "user", "t1", {"text": "old", "source": "todos", "actor": "algorithm"}),
        _segment("q1", "user", "t1", {"text": "question"}),
        _segment("th1", "thought", "t1", {}),
        _segment("todo2", "user", "t2", {"text": "new", "source": "todos", "actor": "algorithm"}),
        _segment("plan", "user", "t2", {"text": "plan", "source": "plan", "actor": "algorithm"}),
        _segment("q2", "user", "t2", {"text": "go on"}),
    ]
    decision = post_compaction_context(
        live, open_turn_id="t2", policy=KeepPolicy(keep_head=True, keep_last_turns=0)
    )
    assert {"todo2", "plan", "q2"} <= set(decision.keep)
    assert {"todo1", "q1", "th1"} <= set(decision.summarize)


def test_presentation_of_a_replaced_passage() -> None:
    row = {"plan_file": "/s/plans/hunt.md", "status": "approved", "change": "passage"}
    view = present({}, "", {**row, "old": "- a: open", "new": "- a: closed"})
    assert view["summary"] == "Plan passage replaced"
    assert [b["id"] for b in view["blocks"]] == ["plan-file", "status", "old", "new"]
    assert view["blocks"][1]["text"] == "Approved, being worked on"


def test_a_quiet_active_plan_is_mentioned_after_the_stale_steps(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import context as ctx
    from clio_agent.gact import working_state

    app, sid, _ = app_session
    update_plan(app, sid, content=_PLAN)
    monkeypatch.setattr(working_state, "stale_steps", lambda: 3)
    noted: list[list[tuple[str, str]]] = []
    loop = SimpleNamespace(recorder=SimpleNamespace(injections=noted.append))
    tokens = [ctx.set_app(app), ctx.set_session_id(sid)]
    try:
        working_state.note_stale_plan(loop, ["fs_read_file"])
        working_state.note_stale_plan(loop, ["plan_update"])  # a plan write resets the count
        for _ in range(2):
            working_state.note_stale_plan(loop, ["fs_read_file"])
        assert noted == []
        working_state.note_stale_plan(loop, ["fs_search_text"])
        [[(source, text)]] = noted
        assert source == working_state.PLAN_STALE_SOURCE
        assert "has not been updated in 3 steps" in text and "plan_update" in text
        update_plan(app, sid, status="done")
        for _ in range(4):
            working_state.note_stale_plan(loop, ["fs_read_file"])
        assert len(noted) == 1  # a closed plan is not mentioned
    finally:
        for token in reversed(tokens):
            ctx.reset(token)


def test_the_person_can_save_an_edited_plan(app_session: Any) -> None:
    app, sid, client = app_session
    update_plan(app, sid, content=_PLAN)
    edited = _PLAN.replace("- bisect: open", "- bisect: drop it, check the config first")
    saved = client.put(f"/v1/sessions/{sid}/plan", json={"content": edited})
    assert saved.status_code == 200, saved.text
    assert "check the config first" in saved.json()["plan"]["text"]
    assert client.put(f"/v1/sessions/{sid}/plan", json={"content": "  "}).status_code == 422
    assert client.put("/v1/sessions/sess_missing/plan", json={"content": "x"}).status_code == 404
    # The agent gets the edit at its next step, marked as the person's, once.
    from clio_agent.gact import context as ctx
    from clio_agent.gact.working_state import offer_person_edit

    offered: list[list[tuple[str, str]]] = []
    tokens = [ctx.set_app(app), ctx.set_session_id(sid)]
    try:
        offer_person_edit(SimpleNamespace(injections=offered.append))
        offer_person_edit(SimpleNamespace(injections=offered.append))
    finally:
        for token in reversed(tokens):
            ctx.reset(token)
    [[(source, text)]] = offered
    assert source == "plan" and text.startswith("## The plan, as the person edited it")
    assert "check the config first" in text
    # Afterwards the plan is ordinary working state again.
    assert plan_injection(app, sid, _session(app, sid)).startswith("## The plan (approved")


def test_the_plan_is_offered_whole_unless_past_the_window_share(
    app_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan is the agent's own file: a 60,000-character plan is offered whole on a
    200k window (the old fixed cut was 12,000), cut only past the window's share, and
    whole when the window is unknown, traced. SABOTAGE: return ``text[:12_000]`` from
    ``_cut_plan`` and the first case is cut; drop its ``trace.event`` and the last fails."""
    from clio_agent.gact.runtime import context_tokens
    from clio_agent.runtime import trace

    app, sid, _ = app_session
    long_plan = "# Objective\n" + "".join(
        f"- step {i:05d}: measure and record\n" for i in range(1_800)
    )
    assert len(long_plan) > 50_000
    update_plan(app, sid, content=long_plan)
    monkeypatch.setattr(context_tokens, "_estimate_text_tokens", lambda text: len(text) // 4)
    window = "clio_agent.gact.context.active_react_context_window"
    monkeypatch.setattr(window, lambda: 200_000)  # share: 25,000 tokens
    whole = plan_injection(app, sid, _session(app, sid))
    assert "step 01799" in whole and "The plan continues" not in whole
    monkeypatch.setattr(window, lambda: 32_000)  # share: 4,000 tokens, 16,000 characters
    cut = plan_injection(app, sid, _session(app, sid))
    assert "step 01799" not in cut and "[The plan continues; read it whole at" in cut
    events: list[str] = []
    monkeypatch.setattr(trace, "event", lambda _area, message, *args: events.append(message % args))
    monkeypatch.setattr(window, lambda: 0)
    assert "step 01799" in plan_injection(app, sid, _session(app, sid))
    assert any(event.startswith("plan_window_unknown") for event in events)
