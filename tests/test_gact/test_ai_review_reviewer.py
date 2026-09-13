"""iowarp/clio-agent#1044 (epic #1031 Pillar 1): the one-shot AI-review reviewer.

The ``ai-review`` approval mode wires a SEPARATE in-process AI reviewer into the
permission gate. This suite proves the security contract and the fail-safe wiring
WITHOUT a real LM (the reviewer verdict + the LM-absent path are mocked):

* an ``allow`` verdict -> the gate returns ``allow`` and records a resolved row with
  ``grantor=reviewer`` (a reviewer decision is always attributable);
* a ``deny`` verdict -> the gate returns ``deny``, recorded with ``grantor=reviewer``;
* ``escalate`` / no-LM / reviewer error / timeout -> the gate FALLS THROUGH to the
  existing human ``evt.wait`` (fail-safe: a human decides, never a silent auto-allow),
  and the pending row carries the TYPED escalation reason;
* the invariant stack ABOVE the reviewer is untouched: a read fast-allows without ever
  consulting the reviewer; the plan/architect lock hard-denies without the reviewer; an
  explicit deny policy denies without the reviewer;
* ``respond_permission`` still resolves byte-identically via the extracted
  ``resolve_permission`` (``grantor=user``), now stamping the grantor.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from clio_agent.gact.app import _make_permission_gate, _tool_session_context, build_app
from clio_agent.gact.runtime import ai_review as ai_review_mod
from clio_agent.gact.runtime.ai_review import (
    REASON_AI_REVIEW_ALLOW,
    REASON_AI_REVIEW_DENY,
    REASON_AI_REVIEW_ERROR,
    REASON_AI_REVIEW_ESCALATE,
    REASON_AI_REVIEW_NO_LM,
    REASON_AI_REVIEW_TIMEOUT,
    _bounded_args_summary,
    _run_reviewer,
    ai_review_verdict,
)

pytestmark = pytest.mark.usefixtures("host_agent_executor")

_FS_WRITE = "fs_apply_edit_write"
_UNCLASSIFIED = "shell.exec"


def _wait_for_row(app, *, timeout: float = 2.5) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = list(app.state.permissions.values())
        if rows:
            return rows[0]
        time.sleep(0.02)
    pytest.fail("permission row never registered")


def _ai_review_session(app, client, *, mode: str = "code") -> str:
    body: dict[str, Any] = {"title": "t", "approval_mode": "ai-review"}
    if mode != "code":
        body["mode"] = mode
    return client.post("/v1/sessions", json=body).json()["id"]


# --------------------------------------------------------------------------- #
# 1. allow verdict -> gate allows + records grantor=reviewer
# --------------------------------------------------------------------------- #


def test_ai_review_allow_resolves_with_reviewer_grantor(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setattr(
        ai_review_mod, "ai_review_verdict", lambda *a, **k: ("allow", REASON_AI_REVIEW_ALLOW)
    )
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = _ai_review_session(app, c)
        gate = _make_permission_gate(app)
        with _tool_session_context(sid):
            assert gate(_FS_WRITE, {"filepath": "x", "content": "y"}) == "allow"
        rows = list(app.state.permissions.values())
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "resolved"
        assert row["action"] == "allow"
        assert row["grantor"] == "reviewer"
        assert row["reason"] == REASON_AI_REVIEW_ALLOW


def test_ai_review_allow_emits_resolved_with_grantor(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setattr(
        ai_review_mod, "ai_review_verdict", lambda *a, **k: ("allow", REASON_AI_REVIEW_ALLOW)
    )
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = _ai_review_session(app, c)
        captured: list = []
        orig = app.state.bus.publish
        app.state.bus.publish = lambda evt: (captured.append(evt), orig(evt))[1]
        gate = _make_permission_gate(app)
        with _tool_session_context(sid):
            assert gate(_FS_WRITE, {"filepath": "x", "content": "y"}) == "allow"
        resolved = [
            e
            for e in captured
            if e.type == "permission.resolved" and e.payload.get("grantor") == "reviewer"
        ]
        assert resolved, "an ai-review allow must emit permission.resolved with grantor=reviewer"


# --------------------------------------------------------------------------- #
# 2. deny verdict -> gate denies + records grantor=reviewer
# --------------------------------------------------------------------------- #


def test_ai_review_deny_resolves_denied_with_reviewer_grantor(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setattr(
        ai_review_mod, "ai_review_verdict", lambda *a, **k: ("deny", REASON_AI_REVIEW_DENY)
    )
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = _ai_review_session(app, c)
        gate = _make_permission_gate(app)
        with _tool_session_context(sid):
            assert gate(_FS_WRITE, {"filepath": "x", "content": "y"}) == "deny"
        rows = list(app.state.permissions.values())
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "resolved"
        assert row["action"] == "deny"
        assert row["grantor"] == "reviewer"
        assert row["reason"] == REASON_AI_REVIEW_DENY


# --------------------------------------------------------------------------- #
# 3. escalate / fail-safe -> falls through to the human evt.wait (never auto-allow)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("reason"),
    [
        REASON_AI_REVIEW_ESCALATE,
        REASON_AI_REVIEW_NO_LM,
        REASON_AI_REVIEW_ERROR,
        REASON_AI_REVIEW_TIMEOUT,
    ],
)
def test_ai_review_escalate_falls_to_human_wait(tmp_path: Path, monkeypatch, reason: str) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setattr(ai_review_mod, "ai_review_verdict", lambda *a, **k: ("escalate", reason))
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = _ai_review_session(app, c)
        gate = _make_permission_gate(app)
        result: dict[str, str] = {}

        def fire() -> None:
            with _tool_session_context(sid):
                result["decision"] = gate(_FS_WRITE, {"filepath": "x", "content": "y"})

        thread = threading.Thread(target=fire)
        thread.start()
        try:
            row = _wait_for_row(app)
            # Fail-safe: the row is PENDING (a human must decide) with the typed escalation reason,
            # never a silent auto-allow.
            assert row["status"] == "pending"
            assert row["reason"] == reason
            assert "decision" not in result, "the gate must still be blocked on the human wait"
            pid = row["id"]
            assert c.post(f"/v1/permissions/{pid}", json={"action": "allow"}).status_code == 204
            thread.join(timeout=2.0)
            assert result["decision"] == "allow"
            # The human resolution stamps grantor=user, not reviewer.
            assert app.state.permissions[pid]["grantor"] == "user"
        finally:
            thread.join(timeout=2.0)


# --------------------------------------------------------------------------- #
# 4. the invariant stack ABOVE the reviewer is never weakened by ai-review
# --------------------------------------------------------------------------- #


def test_read_bypasses_reviewer_entirely(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    spy = Mock(return_value=("allow", REASON_AI_REVIEW_ALLOW))
    monkeypatch.setattr(ai_review_mod, "ai_review_verdict", spy)
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = _ai_review_session(app, c)
        gate = _make_permission_gate(app)
        with _tool_session_context(sid):
            assert gate("fs_read_file", {"filepath": "x"}) == "allow"
        spy.assert_not_called()
        assert app.state.permissions == {}


@pytest.mark.parametrize("locked_mode", ["plan", "architect"])
def test_plan_lock_beats_ai_review(tmp_path: Path, monkeypatch, locked_mode: str) -> None:
    from fastapi.testclient import TestClient

    spy = Mock(return_value=("allow", REASON_AI_REVIEW_ALLOW))
    monkeypatch.setattr(ai_review_mod, "ai_review_verdict", spy)
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = _ai_review_session(app, c, mode=locked_mode)
        gate = _make_permission_gate(app)
        with _tool_session_context(sid):
            assert gate(_FS_WRITE, {"filepath": "x", "content": "y"}) == "deny"
        spy.assert_not_called()
        row = _wait_for_row(app)
        assert row["status"] == "auto_denied"


def test_explicit_deny_policy_beats_ai_review(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    spy = Mock(return_value=("allow", REASON_AI_REVIEW_ALLOW))
    monkeypatch.setattr(ai_review_mod, "ai_review_verdict", spy)
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = _ai_review_session(app, c)
        resp = c.put(
            "/v1/policies",
            json={
                "policies": [
                    {
                        "scope": "session",
                        "scope_id": sid,
                        "tool_name_pattern": _FS_WRITE,
                        "action": "deny",
                    }
                ]
            },
        )
        assert resp.status_code == 200
        gate = _make_permission_gate(app)
        with _tool_session_context(sid):
            assert gate(_FS_WRITE, {"filepath": "x", "content": "y"}) == "deny"
        spy.assert_not_called()
        rows = list(app.state.permissions.values())
        assert len(rows) == 1
        assert rows[0]["reason"] == "policy_deny"


def test_explicit_ask_policy_beats_ai_review_escalates_to_human(
    tmp_path: Path, monkeypatch
) -> None:
    """#1044 precedence: an explicit per-tool ``ask`` policy is 'always confirm THIS with a
    HUMAN' and beats ai-review mode — the reviewer must NOT auto-decide it. The call falls to
    the human evt.wait (reviewer spy NOT called; the reviewer-pending reason NOT stamped),
    uniform with #1034 (explicit policy > mode). Sabotage: drop the ``policy_action != 'ask'``
    guard on the reviewer branch -> the reviewer auto-allows an explicitly-ask'd tool -> red."""
    from fastapi.testclient import TestClient

    from clio_agent.gact.permission_gate import REASON_AI_REVIEW_REVIEWER_PENDING

    spy = Mock(return_value=("allow", REASON_AI_REVIEW_ALLOW))
    monkeypatch.setattr(ai_review_mod, "ai_review_verdict", spy)
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = _ai_review_session(app, c)
        resp = c.put(
            "/v1/policies",
            json={
                "policies": [
                    {
                        "scope": "session",
                        "scope_id": sid,
                        "tool_name_pattern": _FS_WRITE,
                        "action": "ask",
                    }
                ]
            },
        )
        assert resp.status_code == 200
        gate = _make_permission_gate(app)
        result: dict[str, str] = {}

        def fire() -> None:
            with _tool_session_context(sid):
                result["decision"] = gate(_FS_WRITE, {"filepath": "x", "content": "y"})

        thread = threading.Thread(target=fire)
        thread.start()
        try:
            row = _wait_for_row(app)
            assert row["status"] == "pending"
            spy.assert_not_called()  # the reviewer must NOT run for an explicitly-ask'd tool
            assert row.get("reason") != REASON_AI_REVIEW_REVIEWER_PENDING
            assert "decision" not in result, "the gate must be blocked on the HUMAN wait"
            pid = row["id"]
            assert c.post(f"/v1/permissions/{pid}", json={"action": "deny"}).status_code == 204
            thread.join(timeout=2.0)
            assert result["decision"] == "deny"
        finally:
            thread.join(timeout=2.0)


# --------------------------------------------------------------------------- #
# 5. respond_permission byte-identical via the extracted resolve_permission
# --------------------------------------------------------------------------- #


def test_respond_permission_stamps_grantor_user(tmp_path: Path) -> None:
    """The HTTP resolution path resolves as grantor=user (byte-identical to the prior inline
    body, now with the grantor recorded on the row + both resolved payloads)."""
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t", "approval_mode": "ask"}).json()["id"]
        gate = _make_permission_gate(app)
        result: dict[str, str] = {}

        def fire() -> None:
            with _tool_session_context(sid):
                result["decision"] = gate(_UNCLASSIFIED, {"cmd": "rm -rf /"})

        thread = threading.Thread(target=fire)
        thread.start()
        try:
            row = _wait_for_row(app)
            pid = row["id"]
            assert c.post(f"/v1/permissions/{pid}", json={"action": "allow"}).status_code == 204
            thread.join(timeout=2.0)
            assert result["decision"] == "allow"
            resolved = app.state.permissions[pid]
            assert resolved["status"] == "resolved"
            assert resolved["action"] == "allow"
            assert resolved["grantor"] == "user"
        finally:
            thread.join(timeout=2.0)


# --------------------------------------------------------------------------- #
# 6. the reviewer function itself: fail-safe wiring (unit level, no real LM)
# --------------------------------------------------------------------------- #


def test_verdict_no_lm_escalates(monkeypatch) -> None:
    """No LM resolved -> escalate (fail-safe), never an auto-allow."""
    monkeypatch.setattr(ai_review_mod, "_resolve_reviewer_lm", lambda app: (None, None))
    app = Mock()
    app.state.sessions.get.return_value = None
    verdict, reason = ai_review_verdict(app, "sess_x", _FS_WRITE, {"filepath": "x"}, None)
    assert verdict == "escalate"
    assert reason == REASON_AI_REVIEW_NO_LM


def test_verdict_allow_and_deny_map_to_typed_reasons(monkeypatch) -> None:
    monkeypatch.setattr(ai_review_mod, "_resolve_reviewer_lm", lambda app: (object(), None))
    app = Mock()
    app.state.sessions.get.return_value = None

    monkeypatch.setattr(ai_review_mod, "_run_reviewer", lambda *a, **k: ("allow", ""))
    assert ai_review_verdict(app, "s", _FS_WRITE, {}, None) == ("allow", REASON_AI_REVIEW_ALLOW)

    monkeypatch.setattr(ai_review_mod, "_run_reviewer", lambda *a, **k: ("deny", ""))
    assert ai_review_verdict(app, "s", _FS_WRITE, {}, None) == ("deny", REASON_AI_REVIEW_DENY)

    monkeypatch.setattr(ai_review_mod, "_run_reviewer", lambda *a, **k: ("escalate", "escalate"))
    assert ai_review_verdict(app, "s", _FS_WRITE, {}, None) == (
        "escalate",
        REASON_AI_REVIEW_ESCALATE,
    )


def test_run_reviewer_timeout_escalates(monkeypatch) -> None:
    """A reviewer LM call that exceeds the bounded budget -> ('escalate', 'timeout')."""
    import contextlib

    monkeypatch.setattr(ai_review_mod.dspy, "context", lambda **_k: contextlib.nullcontext())

    def _slow(_sig):
        def _call(**_inputs):
            time.sleep(5.0)
            return Mock(decision="allow")

        return _call

    monkeypatch.setattr(ai_review_mod.dspy, "Predict", _slow)
    verdict, key = _run_reviewer(object(), {"tool_name": "x"}, timeout_s=0.2)
    assert verdict == "escalate"
    assert key == "timeout"


def test_run_reviewer_error_escalates(monkeypatch) -> None:
    """A reviewer LM call that raises -> ('escalate', 'error') (fail-safe, never auto-allow)."""
    import contextlib

    monkeypatch.setattr(ai_review_mod.dspy, "context", lambda **_k: contextlib.nullcontext())

    def _boom(_sig):
        def _call(**_inputs):
            raise RuntimeError("provider down")

        return _call

    monkeypatch.setattr(ai_review_mod.dspy, "Predict", _boom)
    verdict, key = _run_reviewer(object(), {"tool_name": "x"}, timeout_s=2.0)
    assert verdict == "escalate"
    assert key == "error"


def test_run_reviewer_unknown_decision_escalates(monkeypatch) -> None:
    """An out-of-vocabulary reviewer decision -> escalate (never coerced to allow)."""
    import contextlib

    monkeypatch.setattr(ai_review_mod.dspy, "context", lambda **_k: contextlib.nullcontext())

    def _weird(_sig):
        def _call(**_inputs):
            return Mock(decision="maybe")

        return _call

    monkeypatch.setattr(ai_review_mod.dspy, "Predict", _weird)
    verdict, key = _run_reviewer(object(), {"tool_name": "x"}, timeout_s=2.0)
    assert verdict == "escalate"
    assert key == "escalate"


def test_bounded_args_summary_truncates() -> None:
    big = {"blob": "z" * 5000}
    summary = _bounded_args_summary(big)
    assert summary.endswith("...(truncated)")
    assert len(summary) <= ai_review_mod._ARGS_SUMMARY_MAX_CHARS + len("...(truncated)")
