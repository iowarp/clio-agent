"""#882 — a failed delegation must reach the wire as ONE header, not a duplicate.

The delegation settle engine emits ``delegate.started`` (the single header a verbatim
client renders) and, on the failure path, a terminal conclusion part. Before #882 the
failure conclusion carried stage ``delegate.failed`` — which a verbatim client renders
on the SAME header lane as ``delegate.started``. So deleting the client-side
``seenDelegation`` dedup (epic #880's deliverable) would render TWO headers for one
(parent → child) failed delegation.

This pins the server guarantee that lets the client dedup be deleted: the failure
conclusion rides the terminal-RETURN lane (stage ``delegate.completed`` with status
``failed``, mirroring a success conclusion), so exactly ONE handoff part is on the
header lane. A part carries exactly ONE stage: ``metadata['stage']`` is the legacy
mirror the client falls back to (``part.stage || part.metadata.stage``) and must agree
with the typed field, or the same part renders on two lanes depending on which stage a
consumer reads.

The failure is NOT hidden (no-silent-fallback): ``status='failed'`` rides the part, and
the precise ``delegate.failed`` stays on the ``delegation.failed`` semantic event and
the persisted ``metadata.expert_handoffs`` row. Reload serves the live parts verbatim,
so the persisted projection agrees byte-for-byte with the live stream
(``reload == live``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import turn_delegation as turn_delegation_module
from clio_agent.gact.app import build_app
from clio_agent.gact.turn_delegation import execute_delegated_experts
from clio_agent.gact.turn_state import TurnState
from clio_agent.gact.types import AgentDef
from clio_agent.gact.workflow_state.schema import WorkflowStateSchema

# The stages a verbatim client (transcriptDelegationModel.ts) renders as a delegation
# HEADER: anything that is NOT the terminal-return lane (delegate.completed / completed)
# and NOT the structurally-dropped parent.resumed twin. delegate.started AND (pre-#882)
# delegate.failed both landed here — the exact duplication ``seenDelegation`` masked.
_RETURN_LANE_STAGES = {"delegate.completed", "completed"}
_DROPPED_STAGES = {"parent.resumed"}


def _is_header_lane(part: Any) -> bool:
    stage = str(getattr(part, "stage", "") or "")
    return stage not in _RETURN_LANE_STAGES and stage not in _DROPPED_STAGES


class _StubAgent:
    def forward(self, question: str, session_id: str):  # pragma: no cover - unused
        raise NotImplementedError


def _turn_state(app: Any, sid: str) -> TurnState:
    sess = app.state.sessions.get(sid)
    state = TurnState(
        app=app,
        sid=sid,
        user_text="",
        user_msg=sess,
        turn_agent_id="root",
        sess=sess,
        bus=app.state.bus,
        turn_id="turn_882",
        trace_id="trace_882",
        retry_attempt_id="",
        native_images=[],
    )
    state.workflow_schema = WorkflowStateSchema()
    state.invocation_agent_id = "root"
    state.active_agent_id = "root"
    return state


def _drive_failed_delegation(
    app: Any,
    sid: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    events: list[tuple[str, dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Run one (root → analysis) delegation whose child raises, hitting the failure
    branch of ``execute_delegated_experts`` against a real ``app.state`` ledger.

    When ``events`` is passed, every semantic event the settle engine emits is
    recorded as ``(event_type, payload)`` — the real emitter still runs.
    """

    if events is not None:
        real_emit = turn_delegation_module._emit_semantic_event

        def _recording_emit(_app: Any, _sid: str, event_type: str, **kw: Any) -> dict[str, Any]:
            events.append((event_type, dict(kw.get("payload") or {})))
            return real_emit(_app, _sid, event_type, **kw)

        monkeypatch.setattr(
            "clio_agent.gact.turn_delegation._emit_semantic_event", _recording_emit
        )

    parent = AgentDef(id="root", source="expert_pack", title="Root")
    child = AgentDef(id="analysis", source="expert_pack", title="Analysis", parent_id="root")

    monkeypatch.setattr(
        "clio_agent.gact.turn_delegation._resolve_runtime_dynamic_agent",
        lambda _app, agent_id, *, session_id="", **_kw: child if agent_id == "analysis" else None,
    )

    async def _boom(_state: Any, _target: Any, _prompt: str) -> Any:
        raise RuntimeError("child blew up")

    monkeypatch.setattr("clio_agent.gact.turn_delegation.run_dynamic_agent_sync", _boom)

    state = _turn_state(app, sid)
    rows = [
        {
            "delegate_to": "analysis",
            "agent_id": "analysis",
            "question": "inspect the evidence",
            "thought": "route to analysis",
            "status": "requested",
            "execute": True,
            "source": "agent_next_expert",
        }
    ]
    return asyncio.run(
        execute_delegated_experts(state, parent, rows, source_text="inspect the evidence")
    )


def _handoff_parts(app: Any, sid: str) -> list[Any]:
    return [p for p in app.state.live_assistant_parts.get(sid, []) if p.type == "expert_handoff"]


def test_failed_delegation_emits_one_header_lane_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAILING-FIRST (#882): a failed delegation must leave exactly ONE handoff part on
    the client's header lane. Before the fix the failure conclusion carried
    ``delegate.failed`` (a second header), so a verbatim client with no dedup renders
    TWO headers for one (parent → child)."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_StubAgent())
    events: list[tuple[str, dict[str, Any]]] = []
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        executed = _drive_failed_delegation(app, sid, monkeypatch, events=events)

        handoffs = _handoff_parts(app, sid)
        stages = [str(p.stage or "") for p in handoffs]
        # The started header + the failure conclusion — the latter on the RETURN lane.
        assert stages == ["delegate.started", "delegate.completed"]
        # Exactly ONE header-lane part survives (what the verbatim client renders).
        assert sum(1 for p in handoffs if _is_header_lane(p)) == 1

        # No-silent-fallback: the failure is visible on the part, and precisely typed
        # everywhere a consumer can read the finer-grained stage.
        failed_part = handoffs[-1]
        assert failed_part.status == "failed"

        # ONE stage per part. ``metadata['stage']`` is the legacy mirror a client falls
        # back to (``part.stage || part.metadata.stage``); a mirror that disagreed with
        # the typed field would make the SAME part render on two different lanes
        # depending on which stage the consumer read.
        for part in handoffs:
            assert str(part.metadata.get("stage") or "") == str(part.stage or "")

        # The precise failure stage lives on the persisted row + the semantic event.
        assert executed[-1]["stage"] == "delegate.failed"
        assert executed[-1]["status"] == "failed"
        failed_events = [(t, p) for (t, p) in events if t.endswith(".failed")]
        assert [t for (t, _p) in failed_events] == ["delegation.failed"]
        assert failed_events[0][1]["stage"] == "delegate.failed"
        assert failed_events[0][1]["status"] == "failed"


def test_failed_delegation_reload_equals_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reload == live: the message projection (``GET /messages`` — the same
    ``to_wire`` + normalize_thought_ownership read boundary a persisted reload uses)
    serves the failure handoff parts verbatim, so their (stage, status) match the live
    stream byte-for-byte."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_StubAgent())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        _drive_failed_delegation(app, sid, monkeypatch)

        live = [(str(p.stage or ""), str(p.status or "")) for p in _handoff_parts(app, sid)]

        reloaded = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        persisted = [
            (p.get("stage") or "", p.get("status") or "")
            for m in reloaded
            if m["role"] == "assistant"
            for p in m.get("parts", [])
            if p.get("type") == "expert_handoff"
        ]

        assert persisted == live
        # And the reloaded projection carries exactly one header-lane handoff too.
        header_lane = [
            s
            for (s, _status) in persisted
            if s not in _RETURN_LANE_STAGES and s not in _DROPPED_STAGES
        ]
        assert header_lane == ["delegate.started"]
