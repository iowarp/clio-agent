"""What a client reads back about a variant run: served from clio-core, reload == live.

``GET /v1/sessions/{sid}/variant-runs`` serves each run's latest ``variant_record`` and,
per try, its own steps folded from the try's segments -- the same after a restart. The
interactions list carries a drafts question's ``metadata.variant``; Refine's advice
injection lands in its try's tab. Real ClioReAct on real clio-core, scripted engine.

SABOTAGE (each run manually, each went RED, then was restored):

* ``try_context`` not keeping ``segment_ids``: every ``steps`` list is empty.
* ``_steps`` folding the try scope's live segments instead of the kept ids: the
  second run reuses ``writer#run0``, so the first run's tab shows the second run.
* ``"metadata"`` dropped from the interaction payload: the interactions test fails.
* ``stamp_active_try`` returning the payload unchanged: the advice part has no tab.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.agents.variant_records import VARIANT_RECORD_KIND, VARIANT_RECORD_SCOPE
from clio_agent.gact.protocol.v3.message import part_to_v3_block
from clio_agent.gact.session_store import _append_session_message
from clio_agent.gact.types import Message, Part
from tests._scripted_engine import Reply, routed_lm
from tests.test_gact.test_variant_drafts import (
    ASK,
    RUN0,
    RUN1,
    SCOPE,
    World,
    _answer,
    _app,
    _draft,
    _forward,
    _score,
    _surface,
)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    """One app on real clio-core with a session (as in ``test_variant_drafts``)."""
    app, arc, events = _app(tmp_path / "sessions.json")
    sid = app.state.sessions.create(workspace_id="ws_default", title="runs").id
    yield World(app, arc, sid, events, tmp_path / "sessions.json")


def _runs(app: Any, sid: str) -> dict[str, Any]:
    response = TestClient(app).get(f"/v1/sessions/{sid}/variant-runs")
    assert response.status_code == 200, response.text
    return response.json()


def _lm_judged(world: World, turn: str, texts: tuple[str, str]) -> None:
    lm, _engine = routed_lm(
        {
            SCOPE: [_draft(2, judge="lm")],
            RUN0: [Reply(text=texts[0], thinking="weighing the tone")],
            RUN1: [Reply(text=texts[1])],
            "": [_score(0.2), _score(0.9)],
        }
    )
    _forward(world.app, world.sid, lm, ASK, turn)


def test_runs_are_served_from_clio_core_with_each_trys_steps_after_a_restart(
    world: World,
) -> None:
    _lm_judged(world, "turn_1", ("Draft A", "Draft B"))
    _append_session_message(
        world.app,
        world.sid,
        Message(
            id="msg_assistant_1",
            session_id=world.sid,
            turn_id="turn_1",
            role="assistant",
            parts=[Part(id="p1", type="text", text="Draft B")],
            created_at="2026-10-01T00:00:00+00:00",
            updated_at="2026-10-01T00:00:00+00:00",
        ),
    )
    _lm_judged(world, "turn_2", ("Draft C", "Draft D"))  # reuses writer#run0 / #run1

    live = _runs(world.app, world.sid)
    first, second = live["runs"]
    assert (first["schema"], first["judge"], first["status"]) == (
        "clio.variant_run.v1",
        "lm",
        "selected",
    )
    assert (first["turn_id"], first["anchor_message_id"]) == ("turn_1", "msg_assistant_1")
    assert second["anchor_message_id"] == ""  # its turn has no assistant message yet
    assert first["selected_index"] == 1
    try0 = first["tries"][0]
    assert {k: try0[k] for k in ("try_index", "scope", "state", "text", "score")} == {
        "try_index": 0,
        "scope": RUN0,
        "state": "completed",
        "text": "Draft A",
        "score": 0.2,
    }
    assert try0["tokens"] == {"input": 10, "output": 5, "total": 15}
    assert (try0["turn_id"], try0["anchor_message_id"]) == ("turn_1", "msg_assistant_1")
    # the try's own steps: its task, then its thinking and answer -- not run 2's
    assert try0["steps"] == [
        {"role": "user", "parts": [{"type": "text", "text": ASK}]},
        {
            "role": "assistant",
            "parts": [
                {"type": "thinking", "text": "weighing the tone"},
                {"type": "text", "text": "Draft A"},
            ],
        },
    ]
    assert second["tries"][0]["steps"][-1]["parts"][-1]["text"] == "Draft C"

    restarted, _arc, _events = _app(world.sessions_path)
    assert _runs(restarted, world.sid) == live


def test_the_interactions_list_carries_the_drafts_question_metadata(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    lm, _engine = routed_lm(
        {SCOPE: [_draft(2)], RUN0: [Reply(text="Draft A")], RUN1: [Reply(text="Draft B")]}
    )
    _forward(world.app, world.sid, lm, ASK, "turn_1")
    question = _surface(world.app, world.sid, "turn_1", monkeypatch)

    rows = TestClient(world.app).get(f"/v1/sessions/{world.sid}/interactions").json()
    [row] = [r for r in rows["interactions"] if r["payload"].get("question_id") == question.id]
    metadata = row["payload"]["metadata"]
    assert metadata["variants_id"] == question.metadata["variants_id"]
    assert metadata["variant"]["candidates"] == [
        {"id": RUN0, "try_index": 0, "text": "Draft A"},
        {"id": RUN1, "try_index": 1, "text": "Draft B"},
    ]
    [served] = _runs(world.app, world.sid)["runs"]
    assert (served["status"], served["judge"]) == ("awaiting_pick", "user")


def test_refine_advice_lands_in_its_trys_tab(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    lm, _engine = routed_lm(
        {
            SCOPE: [_draft(2, strategy="refine")],
            RUN0: [Reply(text="Long draft.")],
            RUN1: [Reply(text="Short draft.")],
        }
    )
    _forward(world.app, world.sid, lm, ASK, "turn_1")
    question = _surface(world.app, world.sid, "turn_1", monkeypatch)
    _answer(world.app, question, RUN0, comment="Shorter, please.")
    _forward(world.app, world.sid, lm, "[Answer] refine", "turn_2")

    variants_id = question.metadata["variants_id"]
    parts = (world.app.state.live_assistant_parts or {}).get(world.sid, [])
    [advice] = [p for p in parts if p.type == "injection" and p.source == "variant_advice"]
    assert (advice.metadata["variants_id"], advice.metadata["try_index"]) == (variants_id, 1)
    block = part_to_v3_block(advice.model_dump())
    assert (block["variants_id"], block["try_index"]) == (variants_id, 1)

    [run] = _runs(world.app, world.sid)["runs"]
    try1 = run["tries"][1]
    assert (try1["advice"], try1["forked_from"], try1["turn_id"]) == (
        "Shorter, please.",
        0,
        "turn_2",
    )
    # its tab: the advice, then its answer (the pick's line before it is the prefix)
    assert try1["steps"] == [
        {
            "role": "user",
            "parts": [{"type": "text", "text": "[clio: variant_advice]\nShorter, please."}],
        },
        {"role": "assistant", "parts": [{"type": "text", "text": "Short draft."}]},
    ]


def test_an_unreadable_record_and_an_unknown_session_are_typed(world: World) -> None:
    client = TestClient(world.app)
    missing = client.get("/v1/sessions/nope/variant-runs")
    assert (missing.status_code, missing.json()["error"]["error"]) == (404, "not_found")

    world.arc.append_segment(world.sid, VARIANT_RECORD_SCOPE, VARIANT_RECORD_KIND, {"x": 1})
    broken = client.get(f"/v1/sessions/{world.sid}/variant-runs")
    assert broken.status_code == 500
    assert broken.json()["error"]["error"] == "variant_record_unreadable"


def test_a_run_lists_its_tries_in_try_order_whatever_order_they_start() -> None:
    """Parallel tries start in any order; the run keeps them as Draft 1, 2, 3 (CI, Linux)."""
    from clio_agent.gact.agents.variant_records import TryRecord, VariantRun

    run = VariantRun(
        variants_id="var_t",
        session_id="sess_t",
        agent_id="main",
        turn_id="",
        origin="draft_alternatives",
        strategy="best_of_n",
        judge="user",
        n=3,
        n_requested=3,
        rubric="",
    )
    for index in (0, 2, 1):
        run.add_try(TryRecord(try_index=index, scope=f"main#run{index}"))
    assert [t.try_index for t in run.tries] == [0, 1, 2]
