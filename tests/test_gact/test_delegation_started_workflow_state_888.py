"""#888 — the ``delegate.started`` row carries the typed ``workflow_state`` snapshot
the parent PASSES INTO the child.

Today ``delegate.completed`` / ``parent.resumed`` rows carry ``metadata.workflow_state``
(the returned / merged state). The ``delegate.started`` row carried none, so the state a
parent seeds a child with was invisible on the wire even though the server composes it
(the same mapping ``_append_accumulated_workflow_state_context`` renders into the child's
execution prompt). #888 attaches that passed-down snapshot as a typed carrier on the
started row and its live ``expert_handoff`` Part — and thus on the persisted Part.metadata
the ``GET /messages`` reload serves.

Shape discipline (#885): a NON-EMPTY mapping -> the ``workflow_state`` key is present on
the started row and on Part.metadata; an EMPTY/absent mapping -> the key is ABSENT (never
present-and-empty).

These tests drive the REAL success branch of ``execute_delegated_experts`` against a real
``app.state`` ledger (the ``_drive_started_delegation`` harness monkeypatches only the
child run), asserting on the value the started row STORES and the value the reloaded wire
Part.metadata SERVES. Removing the attach in ``_delegate_started_row`` turns them RED.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import dspy
import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.turn_delegation import (
    execute_delegated_experts,
    settle_dynamic_agent_delegations,
)
from clio_agent.gact.turn_state import TurnState
from clio_agent.gact.types import AgentDef
from clio_agent.gact.workflow_state.schema import WorkflowStateSchema

# A non-trivial nested typed snapshot — the "what was this child seeded with" evidence a
# parent passes into a child (a ranked station catalog produced by an earlier sibling).
_PASSED_STATE: dict[str, Any] = {
    "station_catalog": {
        "station_ids": ["P001", "P002", "P003"],
        "region": "cascadia",
    },
    "counts": {"resolved": 3, "pending": 0},
}


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
        turn_id="turn_888",
        trace_id="trace_888",
        retry_attempt_id="",
        native_images=[],
    )
    state.workflow_schema = WorkflowStateSchema()
    state.invocation_agent_id = "root"
    state.active_agent_id = "root"
    return state


def _drive_started_delegation(
    app: Any,
    sid: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    passed_workflow_state: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Run one (root -> analysis) delegation, forwarding ``passed_workflow_state`` exactly
    as the settle loop forwards the parent's typed snapshot. Returns the executed rows."""

    parent = AgentDef(id="root", source="expert_pack", title="Root")
    child = AgentDef(id="analysis", source="expert_pack", title="Analysis", parent_id="root")

    monkeypatch.setattr(
        "clio_agent.gact.turn_delegation._resolve_runtime_dynamic_agent",
        lambda _app, agent_id, *, session_id="", **_kw: child if agent_id == "analysis" else None,
    )

    async def _child_returns(_state: Any, _target: Any, _prompt: str) -> Any:
        return dspy.Prediction(answer="done", reasoning="done", expert_handoffs=[])

    monkeypatch.setattr("clio_agent.gact.turn_delegation.run_dynamic_agent_sync", _child_returns)

    async def _no_nested(_state: Any, _target: Any, pred: Any, *, source_text: str = "") -> Any:
        return pred, []

    monkeypatch.setattr(
        "clio_agent.gact.turn_delegation.settle_dynamic_agent_delegations", _no_nested
    )

    state = _turn_state(app, sid)
    rows = [
        {
            "delegate_to": "analysis",
            "agent_id": "analysis",
            "question": "resolve the GNSS stations",
            "thought": "route to analysis",
            "status": "requested",
            "execute": True,
            "source": "agent_next_expert",
        }
    ]
    return asyncio.run(
        execute_delegated_experts(
            state,
            parent,
            rows,
            source_text="resolve the GNSS stations",
            passed_workflow_state=passed_workflow_state,
        )
    )


def _live_started_metadata(app: Any, sid: str) -> dict[str, Any]:
    """Return the LIVE ``delegate.started`` Part.metadata the server emitted this turn."""

    parts = list((getattr(app.state, "live_assistant_parts", {}) or {}).get(sid, []))
    started = [
        p
        for p in parts
        if getattr(p, "type", "") == "expert_handoff"
        and str(getattr(p, "stage", "") or "") == "delegate.started"
    ]
    assert started, (
        f"no live delegate.started part (stages={[getattr(p, 'stage', '') for p in parts]})"
    )
    return dict(started[-1].metadata or {})


def _reloaded_started_part(client: TestClient, sid: str) -> dict[str, Any]:
    reloaded = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    started_parts = [
        p
        for m in reloaded
        if m["role"] == "assistant"
        for p in m.get("parts", [])
        if p.get("type") == "expert_handoff" and str(p.get("stage") or "") == "delegate.started"
    ]
    assert started_parts, "no delegate.started handoff part on the wire"
    return started_parts[-1]


def test_started_row_carries_passed_workflow_state_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parent that HAS accumulated state: the LIVE delegate.started Part.metadata carries
    the passed-down snapshot verbatim on its typed ``workflow_state`` carrier."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_StubAgent())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        _drive_started_delegation(app, sid, monkeypatch, passed_workflow_state=_PASSED_STATE)

        meta = _live_started_metadata(app, sid)
        assert meta.get("workflow_state") == _PASSED_STATE
        assert meta.get("status") == "running"


def test_started_part_metadata_carries_snapshot_on_the_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reload == live: the persisted ``delegate.started`` Part.metadata the wire serves
    carries the passed-down snapshot identically to the live emission."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_StubAgent())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        _drive_started_delegation(app, sid, monkeypatch, passed_workflow_state=_PASSED_STATE)

        part = _reloaded_started_part(client, sid)
        meta = part.get("metadata") or {}
        assert meta.get("workflow_state") == _PASSED_STATE
        # reload == live: identical snapshot on both surfaces.
        assert meta.get("workflow_state") == _live_started_metadata(app, sid).get("workflow_state")


def test_started_row_and_wire_omit_the_key_when_no_passed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parent with NO accumulated state: the ``workflow_state`` key is ABSENT on both the
    live delegate.started Part.metadata and the reloaded Part.metadata (never
    present-and-empty, #885)."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_StubAgent())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        _drive_started_delegation(app, sid, monkeypatch, passed_workflow_state=None)

        assert "workflow_state" not in _live_started_metadata(app, sid)

        part = _reloaded_started_part(client, sid)
        assert "workflow_state" not in (part.get("metadata") or {})


def test_empty_mapping_is_treated_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An EMPTY passed mapping is present-and-empty at the source but must NOT surface a
    present-and-empty key on the started Part.metadata (#885 collapses empty -> absent)."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_StubAgent())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        _drive_started_delegation(app, sid, monkeypatch, passed_workflow_state={})

        assert "workflow_state" not in _live_started_metadata(app, sid)


def _drive_settle_loop_delegation(
    app: Any,
    sid: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    parent_workflow_state: dict[str, Any] | None,
) -> None:
    """Drive the REAL ``settle_dynamic_agent_delegations`` loop end-to-end.

    This is the PRODUCTION forwarding path: the settle loop itself reads the parent
    prediction's typed ``workflow_state`` and forwards it as ``passed_workflow_state``
    into ``execute_delegated_experts`` — this harness never injects the parameter.
    Only the child run + the parent re-entry are monkeypatched; the routing read, the
    snapshot read (``_prediction_workflow_state``), and the dispatch are the real code.
    """

    parent = AgentDef(id="root", source="expert_pack", title="Root")
    child = AgentDef(id="analysis", source="expert_pack", title="Analysis", parent_id="root")

    # The parent's ONE declared child, discoverable by the settle loop's routing read.
    monkeypatch.setattr(
        "clio_agent.gact.turn_delegation._runtime_child_agent_rows",
        lambda _app, parent_id, *, session_id="": [child] if parent_id == "root" else [],
    )
    monkeypatch.setattr(
        "clio_agent.gact.turn_delegation._resolve_runtime_dynamic_agent",
        lambda _app, agent_id, *, session_id="", **_kw: child if agent_id == "analysis" else None,
    )

    async def _child_returns(_state: Any, _target: Any, _prompt: str) -> Any:
        return dspy.Prediction(answer="done", reasoning="done", expert_handoffs=[])

    monkeypatch.setattr("clio_agent.gact.turn_delegation.run_dynamic_agent_sync", _child_returns)

    # The parent's re-entry after the child round: stop (the round under test is round 0).
    async def _parent_finishes(*_args: Any, **_kwargs: Any) -> tuple[Any, bool]:
        return dspy.Prediction(answer="final", reasoning="", next_expert="finish"), True

    monkeypatch.setattr("clio_agent.gact.turn_delegation.settle_parent_next_pred", _parent_finishes)

    # The parent prediction the REAL loop routes on: a typed next_expert route into the
    # declared child, carrying (or not) the typed workflow_state snapshot under test.
    parent_pred = dspy.Prediction(
        answer="prior sibling evidence",
        reasoning="route to analysis",
        next_expert="analysis",
        next_task="resolve the GNSS stations",
        expert_handoffs=[],
        **({"workflow_state": parent_workflow_state} if parent_workflow_state else {}),
    )

    state = _turn_state(app, sid)

    # Neutralize only the CHILD's nested settle recursion (module-global lookup inside
    # execute_delegated_experts); the loop under test is invoked via the direct
    # reference imported at module top, which the patch does not touch.
    async def _no_nested(_state: Any, _target: Any, pred: Any, *, source_text: str = "") -> Any:
        return pred, []

    real_settle = settle_dynamic_agent_delegations
    monkeypatch.setattr(
        "clio_agent.gact.turn_delegation.settle_dynamic_agent_delegations", _no_nested
    )
    asyncio.run(real_settle(state, parent, parent_pred, source_text="resolve the GNSS stations"))


def test_settle_loop_forwards_parent_state_onto_started_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """END-TO-END (#888 production path): the REAL settle loop reads the parent
    prediction's typed ``workflow_state`` and the ``delegate.started`` Part.metadata on
    the wire carries that snapshot — WITHOUT the test injecting ``passed_workflow_state``.

    Dropping the ``passed_workflow_state=parent_state`` forwarding at the settle-loop
    call site (leaving the ``_delegate_started_row`` attach intact) turns this RED while
    the direct-injection tests above stay green — this test owns the call-site wire."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_StubAgent())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        _drive_settle_loop_delegation(app, sid, monkeypatch, parent_workflow_state=_PASSED_STATE)

        # Live emission carries the parent's snapshot.
        meta = _live_started_metadata(app, sid)
        assert meta.get("workflow_state") == _PASSED_STATE

        # Persisted wire (GET /messages reload) carries it identically.
        part = _reloaded_started_part(client, sid)
        assert (part.get("metadata") or {}).get("workflow_state") == _PASSED_STATE


def test_settle_loop_without_parent_state_omits_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """END-TO-END absence twin: a parent prediction with NO typed ``workflow_state``
    yields a ``delegate.started`` part with the key ABSENT on live and reloaded metadata
    (#885 — never present-and-empty)."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_StubAgent())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        _drive_settle_loop_delegation(app, sid, monkeypatch, parent_workflow_state=None)

        assert "workflow_state" not in _live_started_metadata(app, sid)

        part = _reloaded_started_part(client, sid)
        assert "workflow_state" not in (part.get("metadata") or {})
