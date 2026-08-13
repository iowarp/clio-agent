"""iowarp/clio-agent#1034: session approval_mode axis + additive workspace-grant config.

Covers the orthogonal approval axis (``ask``/``auto-edits``/``bypass``/``ai-review``) and the
config surface that drives it:

* the field round-trips through create / PATCH / persist+reload (dual Session type, no migration);
* :func:`default_decision` maps each mode to the right gate decision;
* the four precedence invariants — reads fast-allow in EVERY mode; the plan/architect lock beats
  ``bypass``; an explicit ``deny`` policy beats ``auto-edits``/``bypass`` and an explicit ``allow``
  policy beats ``ask``; ``bypass`` still records a resolved audit row + ``permission.resolved``
  boundary event; ``ai-review`` carries the typed ``ai_review_reviewer_pending`` reason (never
  silent);
* the additive ``POST /v1/workspaces/{wid}/grants`` body — both the legacy subset shape AND the new
  ``kind`` shape work, plus the ``network_write_gate`` toggle;
* the ``permission_default`` deletion breaks nothing.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from clio_agent.gact.app import (
    _make_permission_gate,
    _tool_session_context,
    build_app,
)
from clio_agent.gact.permission_gate import (
    REASON_APPROVAL_AUTO_EDITS,
    REASON_APPROVAL_BYPASS,
    default_decision,
)
from clio_agent.gact.sessions import SessionStore
from clio_agent.gact.types import Session as WireSession
from clio_agent.gact.types import Tool

# Default sessions run the blueprint react ``main``; route it to each test's host fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")

# The catalog write-tagged fs tool (a non-read call that reaches the prompt boundary) and a
# genuinely-unclassifiable non-read call (writes live behind the OS fence, not a catalog tag).
_FS_WRITE = "fs_apply_edit_write"
_UNCLASSIFIED = "shell.exec"
_ALL_MODES = ("ask", "auto-edits", "bypass", "ai-review", "spotter-ai")


def _wait_for_row(app, *, timeout: float = 2.5) -> dict:
    """Block until the gate registers a permission row; fail if none appears."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = list(app.state.permissions.values())
        if rows:
            return rows[0]
        time.sleep(0.02)
    pytest.fail("permission row never registered")


# --------------------------------------------------------------------------- #
# 1. field round-trip: create / PATCH / persist+reload
# --------------------------------------------------------------------------- #


def test_approval_mode_defaults_to_ask(tmp_path: Path) -> None:
    store = SessionStore(path=tmp_path / "s.json")
    sess = store.create(workspace_id="ws_default")
    assert sess.approval_mode == "ask"
    assert WireSession(**sess.to_wire()).approval_mode == "ask"


def test_approval_mode_create_and_update_round_trip(tmp_path: Path) -> None:
    store = SessionStore(path=tmp_path / "s.json")
    sess = store.create(workspace_id="ws_default", approval_mode="bypass")
    assert sess.approval_mode == "bypass"
    updated = store.update(sess.id, approval_mode="auto-edits")
    assert updated is not None and updated.approval_mode == "auto-edits"
    # An invalid value is ignored (leaves the current value), never assigned raw.
    kept = store.update(sess.id, approval_mode="nonsense")
    assert kept is not None and kept.approval_mode == "auto-edits"


def test_approval_mode_persist_and_reload(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    store = SessionStore(path=path)
    sess = store.create(workspace_id="ws_default", approval_mode="ai-review")
    # Fresh store off the same file must reload the persisted value.
    reloaded = SessionStore(path=path).get(sess.id)
    assert reloaded is not None and reloaded.approval_mode == "ai-review"


def test_approval_mode_defaults_for_legacy_row(tmp_path: Path) -> None:
    """A persisted row written before this field existed loads with the ``ask`` default."""
    path = tmp_path / "s.json"
    path.write_text(
        '{"sess_legacy": {"id": "sess_legacy", "workspace_id": "ws_default", "title": "old"}}'
    )
    reloaded = SessionStore(path=path).get("sess_legacy")
    assert reloaded is not None and reloaded.approval_mode == "ask"


def test_patch_session_flips_approval_mode(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
        assert c.get(f"/v1/sessions/{sid}").json()["approval_mode"] == "ask"
        resp = c.patch(f"/v1/sessions/{sid}", json={"approval_mode": "bypass"})
        assert resp.status_code == 200
        assert resp.json()["approval_mode"] == "bypass"
        assert c.get(f"/v1/sessions/{sid}").json()["approval_mode"] == "bypass"


# --------------------------------------------------------------------------- #
# 2. default_decision per mode (pure function)
# --------------------------------------------------------------------------- #


def test_default_decision_bypass_allows_any_non_read() -> None:
    assert default_decision("bypass", "tool", _FS_WRITE, {}) == "allow"
    assert default_decision("bypass", "tool", _UNCLASSIFIED, {}) == "allow"


def test_default_decision_auto_edits_allows_only_writes() -> None:
    assert default_decision("auto-edits", "tool", _FS_WRITE, {}) == "allow"
    # shell is not catalog write-tagged (its writes live behind the OS fence) -> prompt.
    assert default_decision("auto-edits", "tool", _UNCLASSIFIED, {}) == "ask"


def test_default_decision_ask_and_ai_review_prompt() -> None:
    assert default_decision("ask", "tool", _FS_WRITE, {}) == "ask"
    assert default_decision("ai-review", "tool", _FS_WRITE, {}) == "ask"


def test_default_decision_spotter_ai_behaves_exactly_like_ask() -> None:
    """spotter-ai ARMS a watcher child (gact/spotter_watcher.py) but grants no
    auto-approval axis of its own — pinned EXPLICITLY in default_decision, not by
    fall-through accident (see the branch's docstring/comment)."""

    for name, args in ((_FS_WRITE, {"filepath": "x", "content": "y"}), (_UNCLASSIFIED, {})):
        assert default_decision("spotter-ai", "tool", name, args) == default_decision(
            "ask", "tool", name, args
        )
    assert default_decision("spotter-ai", "tool", _FS_WRITE, {}) == "ask"


# --------------------------------------------------------------------------- #
# 3. invariant: reads fast-allow in EVERY mode
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", _ALL_MODES)
def test_reads_never_gated_in_any_mode(tmp_path: Path, mode: str) -> None:
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t", "approval_mode": mode}).json()["id"]
        gate = _make_permission_gate(app)
        with _tool_session_context(sid):
            assert gate("fs_read_file", {"filepath": "x"}) == "allow"
        # A fast-allowed read never registers a permission row in any mode.
        assert app.state.permissions == {}


# --------------------------------------------------------------------------- #
# 4. invariant: plan/architect lock beats approval mode
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("locked_mode", ["plan", "architect"])
def test_plan_lock_beats_bypass(tmp_path: Path, locked_mode: str) -> None:
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post(
            "/v1/sessions",
            json={"title": "t", "mode": locked_mode, "approval_mode": "bypass"},
        ).json()["id"]
        gate = _make_permission_gate(app)
        with _tool_session_context(sid):
            assert gate(_FS_WRITE, {"filepath": "x", "content": "y"}) == "deny"
        row = _wait_for_row(app)
        assert row["status"] == "auto_denied"


# --------------------------------------------------------------------------- #
# 5. invariant: explicit policy beats mode
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", ["auto-edits", "bypass"])
def test_explicit_deny_policy_beats_relaxed_mode(tmp_path: Path, mode: str) -> None:
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t", "approval_mode": mode}).json()["id"]
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
        rows = list(app.state.permissions.values())
        assert len(rows) == 1
        assert rows[0]["reason"] == "policy_deny"


def test_explicit_allow_policy_beats_ask(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t", "approval_mode": "ask"}).json()["id"]
        resp = c.put(
            "/v1/policies",
            json={
                "policies": [
                    {
                        "scope": "session",
                        "scope_id": sid,
                        "tool_name_pattern": _FS_WRITE,
                        "action": "allow",
                    }
                ]
            },
        )
        assert resp.status_code == 200
        gate = _make_permission_gate(app)
        with _tool_session_context(sid):
            assert gate(_FS_WRITE, {"filepath": "x", "content": "y"}) == "allow"
        rows = list(app.state.permissions.values())
        assert len(rows) == 1
        assert rows[0]["reason"] == "policy_allow"


def test_explicit_ask_policy_survives_bypass(tmp_path: Path) -> None:
    """#1034 precedence (uniform): an explicit per-tool ``ask`` policy is a deliberate 'always
    confirm this tool' and beats the mode — even ``bypass`` must PROMPT for it, not auto-approve.
    Mode only governs the UN-policied case; explicit deny/allow/ask all beat mode."""
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t", "approval_mode": "bypass"}).json()["id"]
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
            # bypass must NOT auto-approve an explicitly-ask'd tool: it PROMPTS instead.
            assert row["status"] == "pending"
            assert row.get("reason") != REASON_APPROVAL_BYPASS
            pid = row["id"]
            assert c.post(f"/v1/permissions/{pid}", json={"action": "deny"}).status_code == 204
            thread.join(timeout=2.0)
            assert result["decision"] == "deny"
        finally:
            thread.join(timeout=2.0)


# --------------------------------------------------------------------------- #
# 6. bypass records a resolved audit row + boundary event; fence untouched
# --------------------------------------------------------------------------- #


def test_bypass_allows_and_records(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t", "approval_mode": "bypass"}).json()["id"]
        captured: list = []
        orig_publish = app.state.bus.publish
        app.state.bus.publish = lambda evt: (captured.append(evt), orig_publish(evt))[1]

        gate = _make_permission_gate(app)
        with _tool_session_context(sid):
            assert gate(_FS_WRITE, {"filepath": "x", "content": "y"}) == "allow"

        rows = list(app.state.permissions.values())
        assert len(rows) == 1
        assert rows[0]["status"] == "auto_approved"
        assert rows[0]["action"] == "allow"
        assert rows[0]["reason"] == REASON_APPROVAL_BYPASS
        # The resolved boundary event reached the bus, not a silent allow.
        resolved = [
            e
            for e in captured
            if e.type == "permission.resolved" and e.payload.get("reason") == REASON_APPROVAL_BYPASS
        ]
        assert resolved, "bypass allow must emit a permission.resolved boundary event"


def test_auto_edits_allows_fs_write_and_records(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t", "approval_mode": "auto-edits"}).json()[
            "id"
        ]
        gate = _make_permission_gate(app)
        with _tool_session_context(sid):
            assert gate(_FS_WRITE, {"filepath": "x", "content": "y"}) == "allow"
        rows = list(app.state.permissions.values())
        assert len(rows) == 1
        assert rows[0]["reason"] == REASON_APPROVAL_AUTO_EDITS


def test_auto_edits_non_write_falls_through_to_prompt(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t", "approval_mode": "auto-edits"}).json()[
            "id"
        ]
        gate = _make_permission_gate(app)
        result: dict[str, str] = {}

        def fire() -> None:
            with _tool_session_context(sid):
                result["decision"] = gate(_UNCLASSIFIED, {"cmd": "rm -rf /"})

        thread = threading.Thread(target=fire)
        thread.start()
        try:
            row = _wait_for_row(app)
            # A non-write under auto-edits PROMPTS (pending), not auto-approved.
            assert row["status"] == "pending"
            assert row.get("reason") != REASON_APPROVAL_AUTO_EDITS
            pid = row["id"]
            assert c.post(f"/v1/permissions/{pid}", json={"action": "deny"}).status_code == 204
            thread.join(timeout=2.0)
            assert result["decision"] == "deny"
        finally:
            thread.join(timeout=2.0)


# --------------------------------------------------------------------------- #
# 7. ai-review carries the typed reviewer-pending reason (never silent)
# --------------------------------------------------------------------------- #


def test_ai_review_escalate_prompts_and_human_resolves(tmp_path: Path, monkeypatch) -> None:
    """#1044 migrated: ai-review now RUNS the reviewer (it no longer just prompts). On ESCALATE
    (forced here via a mocked verdict — no LM dependency, deterministic) the gate falls to the
    human prompt with the typed escalation reason and a human resolves — never a silent auto-allow.
    The reviewer allow/deny + the full invariant matrix live in test_ai_review_reviewer.py."""
    from fastapi.testclient import TestClient

    from clio_agent.gact.runtime import ai_review as _ai_review_mod

    monkeypatch.setattr(
        _ai_review_mod,
        "ai_review_verdict",
        lambda *a, **k: ("escalate", _ai_review_mod.REASON_AI_REVIEW_ESCALATE),
    )
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t", "approval_mode": "ai-review"}).json()["id"]
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
            assert row["reason"] == _ai_review_mod.REASON_AI_REVIEW_ESCALATE
            pid = row["id"]
            assert c.post(f"/v1/permissions/{pid}", json={"action": "deny"}).status_code == 204
            thread.join(timeout=2.0)
            assert result["decision"] == "deny"
        finally:
            thread.join(timeout=2.0)


# --------------------------------------------------------------------------- #
# 8. additive workspace-grant config: both body shapes + write-gate toggle
# --------------------------------------------------------------------------- #


def test_grant_subset_probe_body_still_works(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.post("/v1/workspaces/ws_default/grants", json={"deny_mode": True})
        assert resp.status_code == 200
        assert resp.json()["deny_mode"] is True
        resp = c.post("/v1/workspaces/ws_default/grants", json={"domain": "example.com"})
        assert resp.status_code == 200
        assert resp.json()["domain"]["granted"] is True


def test_grant_kind_dispatch_tool(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.post(
            "/v1/workspaces/ws_default/grants",
            json={
                "kind": "tool",
                "pattern": "hdf5_*",
                "decision": "allow",
                "scope": "workspace",
            },
        )
        assert resp.status_code == 200
        grant = resp.json()["grant"]
        assert grant["granted"] is True and grant["kind"] == "tool"
        # The policy is actually stored (kind-dispatch appended it, enforceable).
        stored = [
            p
            for p in app.state.permission_policies
            if p.get("tool_name_pattern") == "hdf5_*" and p.get("action") == "allow"
        ]
        assert stored, "kind=tool grant must append an enforceable policy row"


def test_grant_kind_dispatch_domain(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.post(
            "/v1/workspaces/ws_default/grants",
            json={"kind": "domain", "pattern": "api.example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["grant"]["granted"] is True


def test_grant_network_write_gate_toggle(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from clio_agent.gact.runtime.grants import (
        NETWORK_WRITE_GATE_CONFIG_KEY,
        workspace_write_gate,
    )

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.post("/v1/workspaces/ws_default/grants", json={"network_write_gate": True})
        assert resp.status_code == 200
        assert resp.json()["network_write_gate"] is True
        ws = app.state.workspaces.get("ws_default")
        assert ws.config.get(NETWORK_WRITE_GATE_CONFIG_KEY) is True
        assert workspace_write_gate(app, "ws_default") is True


def test_grant_empty_body_is_rejected(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        assert c.post("/v1/workspaces/ws_default/grants", json={}).status_code == 400
        # kind without a pattern is a malformed grant, surfaced (not silently dropped).
        assert c.post("/v1/workspaces/ws_default/grants", json={"kind": "tool"}).status_code == 400
        # an unknown kind is rejected.
        assert (
            c.post(
                "/v1/workspaces/ws_default/grants",
                json={"kind": "wat", "pattern": "x"},
            ).status_code
            == 400
        )


# --------------------------------------------------------------------------- #
# 9. permission_default deletion breaks nothing
# --------------------------------------------------------------------------- #


def test_permission_default_field_is_gone() -> None:
    assert "permission_default" not in Tool.model_fields


def test_catalog_tools_have_no_permission_default(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.get("/v1/tools")
        assert resp.status_code == 200
        for tool in resp.json()["tools"]:
            assert "permission_default" not in tool
