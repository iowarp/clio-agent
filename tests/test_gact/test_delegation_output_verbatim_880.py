"""#880 — a completed delegation's ``output`` is the child's answer BYTE-FOR-BYTE.

The delegation return contract is ``{ output , workflow_state }``. ``output`` carries
the child's parent-bound answer verbatim, whatever its shape: a 4000-char JSON blob is
returned as that blob, and the UI renders it verbatim behind *show more*. The server no
longer blanks a structured answer, and no server-authored summary is synthesized in its
place (the whole ``return_summary.py`` layer was deleted). The typed machine state rides
the row's separate ``workflow_state`` carrier, never parsed out of ``output`` prose.

These tests assert on the value the delegation row STORES (the row
``execute_delegated_experts`` returns and persists), driving the REAL success branch
against a real ``app.state`` ledger. They go RED if the ``output = "" if
_looks_like_structured_answer(output) else output`` blanking is restored, or if a
server-authored ``output_summary`` re-appears as the parent-consumable text.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import dspy
import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.turn_delegation import execute_delegated_experts
from clio_agent.gact.turn_state import TurnState
from clio_agent.gact.types import AgentDef
from clio_agent.gact.workflow_state.schema import WorkflowStateSchema

# A STRUCTURED (JSON) child answer — the exact shape the deleted blanking targeted.
# Assembled so the serialization is stable and the assertion pins the exact bytes.
_STRUCTURED_ANSWER = json.dumps(
    {
        "region": "cascadia",
        "stations": ["P001", "P002", "P003"],
        "summary": "3 GNSS stations resolved with analysis-ready time series",
        "counts": {"resolved": 3, "pending": 0},
    }
)


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
        turn_id="turn_880",
        trace_id="trace_880",
        retry_attempt_id="",
        native_images=[],
    )
    state.workflow_schema = WorkflowStateSchema()
    state.invocation_agent_id = "root"
    state.active_agent_id = "root"
    return state


def _drive_completed_delegation(
    app: Any,
    sid: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    child_answer: str,
) -> list[dict[str, Any]]:
    """Run one (root -> analysis) delegation whose child returns ``child_answer`` as its
    typed ``answer``, hitting the SUCCESS branch of ``execute_delegated_experts`` against
    a real ``app.state`` ledger. Returns the executed delegation rows (the STORED shape).
    """

    parent = AgentDef(id="root", source="expert_pack", title="Root")
    child = AgentDef(id="analysis", source="expert_pack", title="Analysis", parent_id="root")

    monkeypatch.setattr(
        "clio_agent.gact.turn_delegation._resolve_runtime_dynamic_agent",
        lambda _app, agent_id, *, session_id="", **_kw: child if agent_id == "analysis" else None,
    )

    async def _child_returns(_state: Any, _target: Any, _prompt: str) -> Any:
        return dspy.Prediction(answer=child_answer, reasoning="done", expert_handoffs=[])

    monkeypatch.setattr("clio_agent.gact.turn_delegation.run_dynamic_agent_sync", _child_returns)

    # Isolate the completed-row build: the child settles no further delegations.
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
        execute_delegated_experts(state, parent, rows, source_text="resolve the GNSS stations")
    )


def _completed_row(executed: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [r for r in executed if str(r.get("stage") or "") == "delegate.completed"]
    assert completed, f"no completed delegation row in {[r.get('stage') for r in executed]}"
    return completed[-1]


def test_completed_delegation_output_is_structured_answer_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The STORED completed row's ``output`` is the child's STRUCTURED (JSON) answer
    byte-for-byte. Restoring the ``_looks_like_structured_answer`` blanking (which set
    ``output=""`` for a structured answer) turns this RED."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_StubAgent())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        executed = _drive_completed_delegation(
            app, sid, monkeypatch, child_answer=_STRUCTURED_ANSWER
        )

        row = _completed_row(executed)
        # Byte-for-byte: not blanked, not summarized, not re-serialized.
        assert row["output"] == _STRUCTURED_ANSWER
        assert row["status"] == "completed"
        # The retired server-authored channels do not exist on the row shape at all
        # (deleted keys, not present-and-empty).
        assert "output_summary" not in row
        assert "output_raw" not in row
        assert "summary" not in row


def test_completed_delegation_output_verbatim_on_the_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reload == live: the persisted ``expert_handoff`` part the wire carries holds the
    structured answer verbatim (no summary channel, no scrub). This is the value the
    client renders behind *show more*."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_StubAgent())
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        _drive_completed_delegation(app, sid, monkeypatch, child_answer=_STRUCTURED_ANSWER)

        reloaded = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        handoff_parts = [
            p
            for m in reloaded
            if m["role"] == "assistant"
            for p in m.get("parts", [])
            if p.get("type") == "expert_handoff"
        ]
        return_parts = [
            p for p in handoff_parts if str(p.get("stage") or "") == "delegate.completed"
        ]
        assert return_parts, "no completed-return handoff part on the wire"
        meta = return_parts[-1].get("metadata") or {}
        assert meta.get("output") == _STRUCTURED_ANSWER
        # No retired summary vocabulary leaks onto the Part.metadata shape.
        assert "output_summary" not in meta
        assert "output_raw" not in meta


def _drive_empty_answer_with_trajectory(
    app: Any,
    sid: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    trajectory: Any,
) -> list[dict[str, Any]]:
    """Run one (root -> analysis) delegation whose child returns an EMPTY prose answer but
    a non-empty ReAct ``trajectory``, hitting the tool-evidence empty-answer fallback."""

    parent = AgentDef(id="root", source="expert_pack", title="Root")
    child = AgentDef(id="analysis", source="expert_pack", title="Analysis", parent_id="root")

    monkeypatch.setattr(
        "clio_agent.gact.turn_delegation._resolve_runtime_dynamic_agent",
        lambda _app, agent_id, *, session_id="", **_kw: child if agent_id == "analysis" else None,
    )

    async def _child_returns(_state: Any, _target: Any, _prompt: str) -> Any:
        return dspy.Prediction(answer="", reasoning="", expert_handoffs=[], trajectory=trajectory)

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
        execute_delegated_experts(state, parent, rows, source_text="resolve the GNSS stations")
    )


def test_empty_answer_tool_evidence_substitution_records_a_typed_degradation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a tool-backed child returns an EMPTY answer, CLIO substitutes bounded
    tool-trajectory evidence as ``output`` — a SERVER-COMPOSED value. No-silent-fallback:
    that substitution records a typed ``turn_degradation`` reason so the composed
    ``output`` the parent + UI 'show more' render is queryable, not silent.

    Deleting the ``record_turn_degradation`` call turns this RED (the substitution would
    then be a silent server-authored content swap into the UI-rendered field)."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_StubAgent())
    trajectory = {"observation_0": "found 3 GNSS stations staged at /tmp/fresh.sac"}
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        executed = _drive_empty_answer_with_trajectory(app, sid, monkeypatch, trajectory=trajectory)

        # The composed tool evidence IS what landed as ``output`` (the field the UI renders).
        row = _completed_row(executed)
        assert "found 3 GNSS stations staged at /tmp/fresh.sac" in row["output"]

        # No-silent-fallback: a typed degradation reason was recorded for this session.
        ledger = getattr(app.state, "turn_degradations", {}).get(sid, [])
        reasons = [entry.get("reason") for entry in ledger]
        assert "tool_agent_evidence_substituted_for_empty_answer" in reasons


def test_completed_delegation_prose_answer_also_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prose answer is carried on the SAME single ``output`` channel, unchanged — the
    contract is field-shape-agnostic (one source), proving the structured case is not a
    special-cased second channel."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_StubAgent())
    prose = "3 GNSS stations resolved with analysis-ready time series."
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        executed = _drive_completed_delegation(app, sid, monkeypatch, child_answer=prose)
        assert _completed_row(executed)["output"] == prose
