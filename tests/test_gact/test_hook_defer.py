"""P2.6 #1074 — durable defer at every yield point (PreToolUse + turn-ending).

Exercises the real subprocess hook wire (exit-0/exit-2), the parked permission-gate
primitive, and the #1031 deferred-resume fold:

* a PreToolUse ``defer`` PARKS the tool call + PERSISTS a pending approval;
* an out-of-band ``approve`` resumes it and the tool runs (optionally with the
  modify/synthesize the approval carries);
* an out-of-band ``deny`` returns a typed deny to the model;
* ``deny`` beats ``defer`` (tighten-only) and a resume applies EXACTLY ONCE;
* a defer never silently auto-approves (no-session / timeout → fail-safe deny);
* a turn-ending (Stop / UserPromptSubmit) ``defer`` suspends the turn and resumes as a
  new turn on an out-of-band approve.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import _make_permission_gate, _tool_session_context, build_app
from clio_agent.gact.hooks import (
    PRE_TOOL_USE,
    TURN_DEFER_KIND,
    USER_PROMPT_SUBMIT,
    HookDecision,
    HookOutcome,
    hook_reasons,
    install_global_dispatcher,
    take_pre_tool_intercept,
)
from clio_agent.gact.hooks.defer import HOOK_DEFER_PENDING_META, PRETOOL_DEFER_KIND
from clio_agent.gact.hooks.wire import parse_hook_output
from clio_agent.gact.permission_gate import DenyDecision, resolve_permission
from tests.test_gact._hook_fixtures import (
    command_run,
    dispatcher_from_rows,
    make_command_dispatcher,
    write_hook_script,
)

pytestmark = pytest.mark.usefixtures("host_agent_executor")


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = ""
    routing_rationale: str = ""


class _Agent:
    def forward(self, question: str, session_id: str = "default") -> _Pred:
        return _Pred()


_DEFER_BODY = (
    "import json, sys\n"
    "print(json.dumps({'decision': 'defer', 'reason': 'needs human review'}))\n"
    "sys.exit(0)\n"
)


def _wait_for_pending(app, *, kind: str, timeout: float = 3.0) -> str:
    """Poll ``app.state.permissions`` until a pending row of ``kind`` appears; return pid."""

    deadline = time.time() + timeout
    while time.time() < deadline:
        for pid, row in list(app.state.permissions.items()):
            if row.get("kind") == kind and row.get("status") == "pending":
                return pid
        time.sleep(0.02)
    raise AssertionError(f"no pending {kind} permission row appeared")


# --------------------------------------------------------------------------- #
# Unit: the wire contract (defer is first-class; deny beats defer).             #
# --------------------------------------------------------------------------- #


def test_parse_defer_decision_is_first_class() -> None:
    decision = parse_hook_output(
        {"decision": "defer", "reason": "hold"}, hook_id="d", event=PRE_TOOL_USE
    )
    assert decision.decision == "defer"
    assert decision.reason == "hold"


def test_merge_defer_outcome_is_defer() -> None:
    outcome = HookOutcome.merge(
        [HookDecision(decision="defer", reason="hold", hook_id="d")], records=[]
    )
    assert outcome.is_defer is True
    assert outcome.denied is False


def test_deny_beats_defer_in_merge() -> None:
    """Tighten-only: when one hook denies and another defers, the deny wins."""

    outcome = HookOutcome.merge(
        [
            HookDecision(decision="defer", reason="hold", hook_id="a"),
            HookDecision(decision="deny", reason="blocked", hook_id="b"),
        ],
        records=[],
    )
    assert outcome.decision == "deny"
    assert outcome.denied is True
    assert outcome.is_defer is False


# --------------------------------------------------------------------------- #
# PreToolUse defer — the headline within-session durable park.                  #
# --------------------------------------------------------------------------- #


def test_pretool_defer_parks_then_out_of_band_approve_runs_tool(tmp_path: Path) -> None:
    install_global_dispatcher(
        make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=_DEFER_BODY)
    )
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        with TestClient(app) as c:
            sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

            approved: dict[str, str] = {}

            def approve_when_pending() -> None:
                pid = _wait_for_pending(app, kind=PRETOOL_DEFER_KIND)
                approved["pid"] = pid
                # The durable cross-restart mirror carries the pending defer.
                sess = app.state.sessions.get(sid)
                assert pid in (sess.metadata.get(HOOK_DEFER_PENDING_META) or {})
                resolve_permission(app, pid, "allow")

            t = threading.Thread(target=approve_when_pending)
            t.start()
            gate = _make_permission_gate(app)
            with _tool_session_context(sid):
                decision = gate("hdf5_write", {"path": "/tmp/x"})
            t.join(timeout=3.0)

            assert decision == "allow"
            # A plain approve carries no intercept — the tool runs with original args.
            assert take_pre_tool_intercept() is None
            pid = approved["pid"]
            assert app.state.permissions[pid]["status"] == "resolved"
            # The durable mirror is pruned on resolve.
            sess = app.state.sessions.get(sid)
            assert pid not in (sess.metadata.get(HOOK_DEFER_PENDING_META) or {})
    finally:
        install_global_dispatcher(None)


def test_pretool_defer_out_of_band_deny_returns_typed_deny(tmp_path: Path) -> None:
    install_global_dispatcher(
        make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=_DEFER_BODY)
    )
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        with TestClient(app) as c:
            sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

            def deny_when_pending() -> None:
                pid = _wait_for_pending(app, kind=PRETOOL_DEFER_KIND)
                resolve_permission(app, pid, "deny")

            t = threading.Thread(target=deny_when_pending)
            t.start()
            gate = _make_permission_gate(app)
            with _tool_session_context(sid):
                decision = gate("hdf5_write", {"path": "/tmp/x"})
            t.join(timeout=3.0)

            assert decision == "deny"
            assert isinstance(decision, DenyDecision)
            assert decision.deny_message  # a typed, model-facing message, never empty
            assert take_pre_tool_intercept() is None
    finally:
        install_global_dispatcher(None)


def test_pretool_defer_approve_with_modify_drives_interceptor(tmp_path: Path) -> None:
    install_global_dispatcher(
        make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=_DEFER_BODY)
    )
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        with TestClient(app) as c:
            sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

            def approve_with_modify() -> None:
                pid = _wait_for_pending(app, kind=PRETOOL_DEFER_KIND)
                resolve_permission(app, pid, "allow", intercept={"input": {"path": "/safe"}})

            t = threading.Thread(target=approve_with_modify)
            t.start()
            gate = _make_permission_gate(app)
            with _tool_session_context(sid):
                decision = gate("hdf5_write", {"path": "/tmp/x"})
            t.join(timeout=3.0)

            assert decision == "allow"
            intercept = take_pre_tool_intercept()
            assert intercept is not None
            assert intercept.kind == "modify"
            assert intercept.modified_args == {"path": "/safe"}
    finally:
        install_global_dispatcher(None)


def test_pretool_defer_approve_with_synthesize_drives_interceptor(tmp_path: Path) -> None:
    install_global_dispatcher(
        make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=_DEFER_BODY)
    )
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        with TestClient(app) as c:
            sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

            def approve_with_synth() -> None:
                pid = _wait_for_pending(app, kind=PRETOOL_DEFER_KIND)
                resolve_permission(app, pid, "allow", intercept={"result": {"cached": True}})

            t = threading.Thread(target=approve_with_synth)
            t.start()
            gate = _make_permission_gate(app)
            with _tool_session_context(sid):
                decision = gate("hdf5_write", {"path": "/tmp/x"})
            t.join(timeout=3.0)

            assert decision == "allow"
            intercept = take_pre_tool_intercept()
            assert intercept is not None
            assert intercept.kind == "synthesize"
            assert intercept.result == {"cached": True}
    finally:
        install_global_dispatcher(None)


def test_pretool_defer_resume_applies_exactly_once(tmp_path: Path) -> None:
    install_global_dispatcher(
        make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=_DEFER_BODY)
    )
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        with TestClient(app) as c:
            sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

            def approve_twice() -> None:
                pid = _wait_for_pending(app, kind=PRETOOL_DEFER_KIND)
                first = resolve_permission(app, pid, "allow")
                # A second resolve is an idempotent no-op (the once-gate).
                second = resolve_permission(app, pid, "deny")
                assert first is not None
                assert second is None

            t = threading.Thread(target=approve_twice)
            t.start()
            gate = _make_permission_gate(app)
            with _tool_session_context(sid):
                decision = gate("hdf5_write", {"path": "/tmp/x"})
            t.join(timeout=3.0)

            # The first (allow) resolution stands — the second (deny) never applied.
            assert decision == "allow"
    finally:
        install_global_dispatcher(None)


def test_deny_beats_defer_end_to_end_does_not_park(tmp_path: Path) -> None:
    """Two PreToolUse hooks (one deny, one defer): deny wins, the call is DENIED, never parked."""

    deny_script = write_hook_script(
        tmp_path,
        "deny.py",
        "import json, sys\nprint(json.dumps({'decision': 'deny', 'reason': 'nope'}))\nsys.exit(0)\n",
    )
    defer_script = write_hook_script(tmp_path, "defer.py", _DEFER_BODY)
    dispatcher = dispatcher_from_rows(
        [
            {"id": "a-deny", "on": [PRE_TOOL_USE], "run": command_run(deny_script)},
            {"id": "b-defer", "on": [PRE_TOOL_USE], "run": command_run(defer_script)},
        ]
    )
    install_global_dispatcher(dispatcher)
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        with TestClient(app) as c:
            sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
            gate = _make_permission_gate(app)
            with _tool_session_context(sid):
                decision = gate("hdf5_write", {"path": "/tmp/x"})
            assert decision == "deny"
            # No pretool_defer row was ever parked.
            assert not any(
                r.get("kind") == PRETOOL_DEFER_KIND for r in app.state.permissions.values()
            )
    finally:
        install_global_dispatcher(None)


def test_pretool_defer_timeout_denies_fail_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_HOOKS_DEFER_TIMEOUT", "0.3")
    install_global_dispatcher(
        make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=_DEFER_BODY)
    )
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        with TestClient(app) as c:
            sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
            gate = _make_permission_gate(app)
            start = time.time()
            with _tool_session_context(sid):
                decision = gate("hdf5_write", {"path": "/tmp/x"})
            elapsed = time.time() - start
            # Never a silent auto-approve: an unresolved defer is DENIED after the bound.
            assert decision == "deny"
            assert elapsed >= 0.25
            assert any(r.get("reason") == "hook_defer_timeout" for r in hook_reasons())
    finally:
        install_global_dispatcher(None)


def test_pretool_defer_without_session_denies_fail_safe(tmp_path: Path) -> None:
    install_global_dispatcher(
        make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=_DEFER_BODY)
    )
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        with TestClient(app):
            gate = _make_permission_gate(app)
            # No session created / bound: the defer has nowhere to persist → deny.
            decision = gate("hdf5_write", {"path": "/tmp/x"})
            assert decision == "deny"
            assert any(r.get("reason") == "hook_defer_no_session" for r in hook_reasons())
    finally:
        install_global_dispatcher(None)


# --------------------------------------------------------------------------- #
# Turn-ending defer (UserPromptSubmit / Stop) — suspend + deferred-resume.       #
# --------------------------------------------------------------------------- #


def _poll_status(c: TestClient, sid: str, wanted: set[str], timeout: float = 6.0) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = c.get(f"/v1/sessions/{sid}").json()["status"]
        if last in wanted:
            return last
        time.sleep(0.05)
    raise AssertionError(f"session never reached {wanted}; last status={last!r}")


def test_user_prompt_submit_defer_suspends_then_resumes_on_approve(tmp_path: Path) -> None:
    body = (
        "import json, sys\n"
        "envelope = json.load(sys.stdin)\n"
        "if 'defer' in (envelope.get('prompt') or '').lower():\n"
        "    print(json.dumps({'decision': 'defer', 'reason': 'await signoff'}))\n"
        "sys.exit(0)\n"
    )
    install_global_dispatcher(
        make_command_dispatcher(tmp_path, event=USER_PROMPT_SUBMIT, body=body)
    )
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        with TestClient(app) as c:
            sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
            ack = c.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "please defer this request"}]},
            )
            assert ack.status_code == 200
            assert _poll_status(c, sid, {"waiting_user"}) == "waiting_user"
            pid = _wait_for_pending(app, kind=TURN_DEFER_KIND)
            assert app.state.permissions[pid]["hook_event"] == "UserPromptSubmit"

            # Approve out-of-band: the turn resumes as a NEW turn (the resume once-gate
            # keeps the UserPromptSubmit hook from re-deferring the approved prompt).
            resolve_permission(app, pid, "allow")
            assert _poll_status(c, sid, {"idle", "completed"}) in {"idle", "completed"}
    finally:
        install_global_dispatcher(None)


def test_user_prompt_submit_defer_deny_rejects_prompt(tmp_path: Path) -> None:
    body = (
        "import json, sys\n"
        "envelope = json.load(sys.stdin)\n"
        "if 'defer' in (envelope.get('prompt') or '').lower():\n"
        "    print(json.dumps({'decision': 'defer', 'reason': 'await signoff'}))\n"
        "sys.exit(0)\n"
    )
    install_global_dispatcher(
        make_command_dispatcher(tmp_path, event=USER_PROMPT_SUBMIT, body=body)
    )
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        with TestClient(app) as c:
            sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
            c.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "please defer this request"}]},
            )
            assert _poll_status(c, sid, {"waiting_user"}) == "waiting_user"
            pid = _wait_for_pending(app, kind=TURN_DEFER_KIND)
            resolve_permission(app, pid, "deny")
            # Denied prompt: the session returns to idle without running a turn.
            assert _poll_status(c, sid, {"idle"}) == "idle"
    finally:
        install_global_dispatcher(None)


def test_stop_defer_suspends_then_releases_on_approve(tmp_path: Path) -> None:
    marker = tmp_path / "stop_defer_once.txt"
    body = (
        "import json, os, sys\n"
        f"marker = {str(marker)!r}\n"
        "if not os.path.exists(marker):\n"
        "    open(marker, 'w').write('x')\n"
        "    print(json.dumps({'decision': 'defer', 'reason': 'await signoff'}))\n"
        "sys.exit(0)\n"
    )
    install_global_dispatcher(make_command_dispatcher(tmp_path, event="Stop", body=body))
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        with TestClient(app) as c:
            sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
            c.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "hello"}]},
            )
            assert _poll_status(c, sid, {"waiting_user"}) == "waiting_user"
            pid = _wait_for_pending(app, kind=TURN_DEFER_KIND)
            assert app.state.permissions[pid]["hook_event"] == "Stop"
            # Approve: completion accepted, the session releases to idle (no re-drive).
            resolve_permission(app, pid, "allow")
            assert _poll_status(c, sid, {"idle", "completed"}) in {"idle", "completed"}
    finally:
        install_global_dispatcher(None)


def test_stop_defer_deny_redrives_one_more_turn(tmp_path: Path) -> None:
    marker = tmp_path / "stop_defer_deny_once.txt"
    body = (
        "import json, os, sys\n"
        f"marker = {str(marker)!r}\n"
        "if not os.path.exists(marker):\n"
        "    open(marker, 'w').write('x')\n"
        "    print(json.dumps({'decision': 'defer', 'reason': 'not done yet'}))\n"
        "sys.exit(0)\n"
    )
    install_global_dispatcher(make_command_dispatcher(tmp_path, event="Stop", body=body))
    try:
        app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
        with TestClient(app) as c:
            sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
            c.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "hello"}]},
            )
            assert _poll_status(c, sid, {"waiting_user"}) == "waiting_user"
            pid = _wait_for_pending(app, kind=TURN_DEFER_KIND)
            before = len(app.state.messages.get(sid, []))
            # Deny: not done — re-drive one more turn (the marker lets Stop settle now).
            resolve_permission(app, pid, "deny")
            assert _poll_status(c, sid, {"idle", "completed"}) in {"idle", "completed"}
            # The redrive appended at least one new (user redrive) message.
            deadline = time.time() + 3.0
            while time.time() < deadline and len(app.state.messages.get(sid, [])) <= before:
                time.sleep(0.05)
            assert len(app.state.messages.get(sid, [])) > before
    finally:
        install_global_dispatcher(None)
