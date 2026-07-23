"""B5 (#979): grants on the record — boundary events, root-grant API, host vocabulary, deny mode.

gact-level coverage: ``boundary.*`` emission on the three workspace mutations, the
``host_pattern`` permission vocabulary + atomic PUT rejection, the mid-session root-grant API
+ its typed per-platform reason, the deny-mode egress gate flow (unknown domain → interactive
gate → sticky ``host_pattern`` policy → subsequent allow), the ``policy_violation`` grant
affordance, the permission SSE-listing fix, and the fleet serving-child join (the deferred B4
WRITER) end to end.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.runtime import globals as gact_globals
from clio_agent.gact.runtime import grants
from clio_agent.runtime import sandbox_net, sandbox_roots


@pytest.fixture(autouse=True)
def _clean_registries():
    sandbox_roots.clear_write_root_grants()
    sandbox_net.clear_namespace_children()
    yield
    sandbox_roots.clear_write_root_grants()
    sandbox_net.clear_namespace_children()


@pytest.fixture
def captured_events(monkeypatch):
    """Capture every semantic event (grants lazily import ``_emit_semantic_event``)."""
    events: list[dict] = []

    def _capture(app, sid, event_type, **kw):
        events.append({"session_id": sid, "event_type": event_type, **kw})
        return {}

    monkeypatch.setattr(gact_globals, "_emit_semantic_event", _capture)
    return events


def _types(events: list[dict]) -> list[str]:
    return [e["event_type"] for e in events]


# --------------------------------------------------------------------------- #
# SSE listing fix (#979.8) + boundary types are UI events (#979.1)
# --------------------------------------------------------------------------- #


def test_boundary_and_permission_events_are_sse_listed() -> None:
    from clio_agent.gact.semantic_events import event_reaches_ui

    assert event_reaches_ui("boundary.granted")
    assert event_reaches_ui("boundary.revoked")
    assert event_reaches_ui("permission.requested")
    assert event_reaches_ui("permission.resolved")
    # The high-volume provenance substrate stays trace-only, even on failure status.
    assert not event_reaches_ui("net.egress")
    assert not event_reaches_ui("artifact.policy_violation", "failed")


# --------------------------------------------------------------------------- #
# host_pattern vocabulary + atomic PUT rejection (#979.4)
# --------------------------------------------------------------------------- #


def test_host_pattern_validation_and_atomic_rejection(tmp_path) -> None:
    from clio_agent.gact.runtime.permission_policies import _validate_permission_policies

    good = {"scope": "workspace", "scope_id": "ws1", "action": "allow", "host_pattern": "*.ndp.org"}
    bad = {"scope": "workspace", "action": "allow", "host_pattern": 123}  # non-string
    clean, errors = _validate_permission_policies([good, bad])
    assert any(e["field"] == "host_pattern" for e in errors)
    assert clean == [good]  # only the valid row survives validation

    # Route atomicity: a batch with the bad row rejects the WHOLE PUT (no partial apply).
    c = TestClient(build_app(sessions_path=tmp_path / "s.json"))
    before = c.get("/v1/policies").json()["policies"]
    resp = c.put("/v1/policies", json={"policies": [good, bad]})
    assert resp.status_code == 422
    assert c.get("/v1/policies").json()["policies"] == before  # unchanged
    # A clean host_pattern batch is accepted.
    assert c.put("/v1/policies", json={"policies": [good]}).status_code == 200


def test_host_action_for_matches_workspace_scope() -> None:
    from clio_agent.gact.runtime.permission_policies import _host_action_for

    app = SimpleNamespace(
        state=SimpleNamespace(
            permission_policies=[
                {"scope": "workspace", "scope_id": "ws1", "action": "allow", "host_pattern": "*.ndp.org"},
                {"scope": "workspace", "scope_id": "ws1", "action": "deny", "host_pattern": "evil.test"},
            ]
        )
    )
    assert _host_action_for(app, workspace_id="ws1", host="data.ndp.org") == "allow"
    assert _host_action_for(app, workspace_id="ws1", host="evil.test") == "deny"
    assert _host_action_for(app, workspace_id="ws1", host="unknown.test") == ""
    assert _host_action_for(app, workspace_id="ws2", host="data.ndp.org") == ""  # other workspace


def test_network_egress_resolution_derives_host_pattern() -> None:
    from clio_agent.gact.runtime.permission_policies import (
        _append_permission_policy_from_resolution,
    )

    app = SimpleNamespace(state=SimpleNamespace(permission_policies=[], sessions=SimpleNamespace(get=lambda _sid: None)))
    row = {
        "id": "perm_x",
        "session_id": "",
        "kind": "network_egress",
        "tool_call": {"tool_name": "network_egress", "input": {"host": "data.ndp.org", "port": 443}},
    }
    policy = _append_permission_policy_from_resolution(app, row=row, action="allow_workspace")
    assert policy is not None
    assert policy["host_pattern"] == "data.ndp.org"
    assert "path_pattern" not in policy
    assert policy["created_from_permission_id"] == "perm_x"


# --------------------------------------------------------------------------- #
# boundary.* on the three workspace mutations (#979.2)
# --------------------------------------------------------------------------- #


def test_workspace_create_session_attach_and_patch_emit_boundary(tmp_path, captured_events) -> None:
    c = TestClient(build_app(sessions_path=tmp_path / "s.json"))
    root = tmp_path / "proj"
    root.mkdir()

    ws = c.post("/v1/workspaces", json={"name": "p", "root_path": str(root)}).json()
    granted = [e for e in captured_events if e["event_type"] == "boundary.granted"]
    assert granted and granted[-1]["payload"]["kind"] == "root"
    assert granted[-1]["payload"]["scope"] == "workspace"
    assert granted[-1]["payload"]["grantor"] == "user"

    captured_events.clear()
    c.post("/v1/sessions", json={"workspace_id": ws["id"]})
    attach = [e for e in captured_events if e["event_type"] == "boundary.granted"]
    assert attach and attach[-1]["payload"]["scope"] == "session"

    captured_events.clear()
    new_root = tmp_path / "proj2"
    new_root.mkdir()
    c.patch(f"/v1/workspaces/{ws['id']}", json={"root_path": str(new_root)})
    kinds = _types(captured_events)
    assert "boundary.revoked" in kinds and "boundary.granted" in kinds  # honest territory change


# --------------------------------------------------------------------------- #
# mid-session root-grant API + typed per-platform reason (#979.3)
# --------------------------------------------------------------------------- #


def _fake_state(active: bool, mechanism: str):
    return SimpleNamespace(active=active, mechanism=mechanism)


def test_root_grant_endpoint_widens_write_roots_and_reports_reason(
    tmp_path, captured_events, monkeypatch
) -> None:
    from clio_agent.runtime import sandbox

    c = TestClient(build_app(sessions_path=tmp_path / "s.json"))
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    ws = c.post("/v1/workspaces", json={"name": "p", "root_path": str(root)}).json()

    # Floor: recorded, no active fence.
    monkeypatch.setattr(sandbox, "current_state", lambda: _fake_state(False, "none"))
    resp = c.post(f"/v1/workspaces/{ws['id']}/grants", json={"root": str(outside)}).json()
    assert resp["root"]["reason"] == grants.REASON_GRANT_RECORDED_NO_FENCE
    assert resp["root"]["pending_respawn"] is False
    # The grant widened the live territory registry (advisory + fence both consult it).
    roots = sandbox_roots.effective_write_roots(sandbox_roots.PROFILE_SHELL, workspace_root=str(root))
    assert outside.resolve() in {r.resolve() for r in roots}

    # Windows srt (session-wide fs policy): a live child needs a respawn — typed, not silent.
    monkeypatch.setattr(sandbox, "current_state", lambda: _fake_state(True, sandbox.MECHANISM_SRT_WINDOWS))
    resp2 = c.post(f"/v1/workspaces/{ws['id']}/grants", json={"root": str(tmp_path / "o2")}).json()
    assert resp2["root"]["reason"] == grants.REASON_GRANT_PENDING_RESPAWN
    assert resp2["root"]["pending_respawn"] is True

    # A per-spawn active fence (Landlock) applies live.
    monkeypatch.setattr(sandbox, "current_state", lambda: _fake_state(True, sandbox.MECHANISM_LANDLOCK))
    resp3 = c.post(f"/v1/workspaces/{ws['id']}/grants", json={"root": str(tmp_path / "o3")}).json()
    assert resp3["root"]["reason"] == grants.REASON_GRANT_LIVE

    assert "boundary.granted" in _types(captured_events)


def test_root_grant_persists_and_replays(tmp_path, monkeypatch) -> None:
    """A recorded root grant rides the workspace record and replays into the live registry."""
    from clio_agent.runtime import sandbox

    monkeypatch.setattr(sandbox, "current_state", lambda: _fake_state(False, "none"))
    app = build_app(sessions_path=tmp_path / "s.json")
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    ws = app.state.workspaces.create(name="p", root_path=str(root))
    grants.apply_root_grant(app, ws.id, str(outside), emit=False)

    # Persisted onto the workspace config (no new store — RULE 4).
    stored = app.state.workspaces.get(ws.id).config.get(grants.GRANTED_ROOTS_CONFIG_KEY)
    assert stored and str(outside.resolve()) in stored

    # A fresh process (cleared registry) replays it back into the live territory.
    sandbox_roots.clear_write_root_grants()
    assert outside.resolve() not in set(sandbox_roots.granted_write_roots(str(root)))
    grants.replay_persisted_root_grants(app)
    assert outside.resolve() in set(sandbox_roots.granted_write_roots(str(root)))


def test_grant_endpoint_requires_a_grantable_field(tmp_path) -> None:
    c = TestClient(build_app(sessions_path=tmp_path / "s.json"))
    ws = c.get("/v1/workspaces").json()["workspaces"][0]
    assert c.post(f"/v1/workspaces/{ws['id']}/grants", json={}).status_code == 400
    assert c.post("/v1/workspaces/ws_missing/grants", json={"root": "/x"}).status_code == 404


def test_domain_grant_endpoint_writes_host_pattern_policy(tmp_path, captured_events) -> None:
    c = TestClient(build_app(sessions_path=tmp_path / "s.json"))
    ws = c.get("/v1/workspaces").json()["workspaces"][0]
    c.post(f"/v1/workspaces/{ws['id']}/grants", json={"domain": "data.ndp.org"})
    policies = c.get("/v1/policies").json()["policies"]
    assert any(p.get("host_pattern") == "data.ndp.org" for p in policies)
    dom = [e for e in captured_events if e["event_type"] == "boundary.granted"]
    assert dom and dom[-1]["payload"]["kind"] == "domain"


# --------------------------------------------------------------------------- #
# grant affordance on policy_violation (#979.6)
# --------------------------------------------------------------------------- #


def test_policy_violation_carries_grant_affordance() -> None:
    from clio_agent.gact.artifacts.violations import PolicyViolation, _grant_affordance

    text = _grant_affordance("ws1", "/etc/passwd")
    assert "/v1/workspaces/ws1/grants" in text and "root" in text
    v = PolicyViolation(kind="prevented", mechanism="landlock", path="/x/y", next_action=text)
    assert v.to_payload()["next_action"] == text


# --------------------------------------------------------------------------- #
# deny-mode egress gate flow (#979.5) — the sabotage/unblock
# --------------------------------------------------------------------------- #


def _egress_record(host: str, workspace_root: str):
    from clio_agent.runtime.net_chokepoint import EgressRecord

    return EgressRecord(
        child_id="c1",
        host=host,
        port=443,
        resolved_ip="",
        transport="connect",
        mechanism="proxy-enforced",
        workspace_root=workspace_root,
        at="2026-07-23T00:00:00+00:00",
    )


def test_deny_mode_off_is_allow(tmp_path, captured_events) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    root = tmp_path / "proj"
    root.mkdir()
    ws = app.state.workspaces.create(name="p", root_path=str(root))
    rec = _egress_record("anything.test", str(root))
    assert grants._egress_gate_decision(app, rec) == "allow"  # default ALLOW + RECORD (B4)


def test_deny_mode_unknown_domain_prompts_then_grant_unblocks(tmp_path, captured_events) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    root = tmp_path / "proj"
    root.mkdir()
    ws = app.state.workspaces.create(
        name="p", root_path=str(root), metadata={"network_deny_mode": True}
    )
    app.state.sessions.create(workspace_id=ws.id)
    rec = _egress_record("data.ndp.org", str(root))

    # The unknown domain BLOCKS on the interactive gate — run it off-thread and resolve it.
    result: dict = {}

    def _run() -> None:
        result["decision"] = grants._egress_gate_decision(app, rec)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    # Poll for the pending network_egress permission row.
    deadline = time.time() + 5
    pid = ""
    while time.time() < deadline and not pid:
        for p, r in list(app.state.permissions.items()):
            if r.get("kind") == "network_egress" and r.get("status") == "pending":
                pid = p
                break
        time.sleep(0.02)
    assert pid, "an unknown deny-mode domain must open an interactive permission request"
    assert "permission.requested" in _types(captured_events)

    # Resolve it allow_workspace (the route path): derive the sticky host_pattern policy + wake.
    from clio_agent.gact.runtime.permission_policies import (
        _append_permission_policy_from_resolution,
    )

    row = app.state.permissions[pid]
    row["status"] = "resolved"
    row["action"] = "allow_workspace"
    _append_permission_policy_from_resolution(app, row=row, action="allow_workspace")
    app.state.permission_events[pid].set()
    t.join(timeout=5)
    assert result["decision"] == "allow"

    # Sticky: a SUBSEQUENT egress to the same domain is allowed with NO new gate.
    captured_events.clear()
    assert grants._egress_gate_decision(app, _egress_record("data.ndp.org", str(root))) == "allow"
    assert "permission.requested" not in _types(captured_events)

    # Sabotage: an UN-granted sibling domain is still blocked (its grant would unblock only it).
    app.state.permission_policies.append(
        {"scope": "workspace", "scope_id": ws.id, "action": "deny", "host_pattern": "evil.test"}
    )
    assert grants._egress_gate_decision(app, _egress_record("evil.test", str(root))) == "deny"


# --------------------------------------------------------------------------- #
# fleet serving-child join — the deferred B4 WRITER (#979.7)
# --------------------------------------------------------------------------- #


def test_fleet_serving_child_wired_join_mints_and_sibling_abstains(tmp_path) -> None:
    """The wired seam populates the call→child map and the step-2 mint fires end to end."""
    from clio_agent.gact.artifacts.ingest_edges import (
        attach_ingest_edges,
        join_call_to_serving_child,
        resolve_serving_child_id,
    )

    app = build_app(sessions_path=tmp_path / "s.json")
    root = tmp_path / "proj"
    root.mkdir()
    ws = app.state.workspaces.create(name="p", root_path=str(root))
    sess = app.state.sessions.create(workspace_id=ws.id)

    # Spawn-time registration (what mcp_config.transport_for does under an active fence).
    sandbox_net.register_namespace_child(str(root), "geo", "child_geo")

    # Observer started-phase: a call to the ``geo`` namespace joins call_id → child_geo.
    join_call_to_serving_child(app, sess.id, "geo_fetch", "call_1")
    assert resolve_serving_child_id(app, "call_1") == "child_geo"

    # The join mints a web edge for the SAME child's in-window egress...
    app.state.net_egress_records = [
        {
            "child_id": "child_geo",
            "host": "ndp.example",
            "port": 443,
            "resolved_ip": "203.0.113.1",
            "transport": "connect",
            "mechanism": "proxy-enforced",
            "workspace_root": str(root),
            "at": "2026-07-23T00:00:01+00:00",
        }
    ]
    out = attach_ingest_edges(
        app,
        [],
        workspace_id=ws.id,
        tool_name="fetch",
        started_at=1.0,
        serving_child_id=resolve_serving_child_id(app, "call_1"),
    )
    assert len(out) == 1 and out[0].net_domain == "ndp.example"

    # ...but a SIBLING child's egress is never minted onto this call (precision, #978 pt 5).
    join_call_to_serving_child(app, sess.id, "geo_fetch", "call_2")  # served by child_geo
    app.state.net_egress_records = [
        {**app.state.net_egress_records[0], "child_id": "child_other", "host": "x.example"}
    ]
    out2 = attach_ingest_edges(
        app,
        [],
        workspace_id=ws.id,
        tool_name="fetch",
        started_at=1.0,
        serving_child_id=resolve_serving_child_id(app, "call_2"),
    )
    assert out2 == []  # sibling egress abstains — no fabricated edge
