"""Unit coverage for the unified grant resolver + read-only predicate (#1032).

Asserts the load-bearing invariants of ``grant_resolver.resolve`` / ``is_read_only``:

* one policy row resolves IDENTICALLY through the tool-gate shim
  (``_policy_action_for_tool``) and the egress-gate shim (``_host_action_for``) and via
  ``resolve`` directly — the two former matchers are now one;
* ``is_read_only`` early-returns "allow" at the gate BEFORE the plan/architect lock, in
  every mode (no mode can gate a read — the structural invariant);
* legacy policy rows without a ``kind`` normalize (kind synthesized from the set pattern
  field) and round-trip;
* the session-scoped host leak guard is preserved verbatim by ``kind="domain"``;
* the RAW action vocabulary (``allow_session``/``allow_workspace``) is never collapsed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.gact.app import (
    _make_permission_gate,
    _tool_session_context,
    build_app,
)
from clio_agent.gact.permission_gate import _policy_action_for_tool
from clio_agent.gact.runtime.grant_resolver import (
    KIND_DOMAIN,
    KIND_ROOT,
    KIND_TOOL,
    GrantRecord,
    is_read_only,
    resolve,
)
from clio_agent.gact.runtime.permission_policies import _host_action_for

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def test_tool_gate_shim_matches_resolve_directly(tmp_path: Path) -> None:
    """``_policy_action_for_tool`` is a thin shim over ``resolve(kind="tool")``."""
    app = build_app(sessions_path=tmp_path / "s.json")
    policies = [
        {
            "scope": "session",
            "scope_id": "sess_a",
            "tool_name_pattern": "shell.*",
            "action": "deny",
        }
    ]
    app.state.permission_policies = policies
    from types import SimpleNamespace

    session = SimpleNamespace(id="sess_a", workspace_id="ws_a")
    shim = _policy_action_for_tool(
        app, session_id="sess_a", session=session, tool_name="shell.exec", args={}
    )
    direct = resolve(
        "tool", "shell.exec", policies=policies, session_id="sess_a", workspace_id="ws_a"
    )
    assert shim == direct == "deny"


def test_egress_gate_shim_matches_resolve_directly(tmp_path: Path) -> None:
    """``_host_action_for`` is a thin shim over ``resolve(kind="domain")`` — same row, same decision."""
    app = build_app(sessions_path=tmp_path / "s.json")
    policies = [
        {"scope": "workspace", "scope_id": "ws_a", "action": "allow", "host_pattern": "ok.test"}
    ]
    app.state.permission_policies = policies
    shim = _host_action_for(app, workspace_id="ws_a", host="ok.test")
    direct = resolve("domain", "ok.test", policies=policies, workspace_id="ws_a")
    assert shim == direct == "allow"


def test_resolve_returns_raw_action_vocabulary() -> None:
    """The raw ``allow_session``/``allow_workspace`` distinction is preserved (never collapsed)."""
    policies = [
        {"scope": "session", "tool_name_pattern": "a", "action": "allow_session"},
    ]
    assert resolve("tool", "a", policies=policies, session_id="s") == "allow_session"
    policies = [
        {"scope": "workspace", "tool_name_pattern": "b", "action": "allow_workspace"},
    ]
    assert resolve("tool", "b", policies=policies, workspace_id="w") == "allow_workspace"


def test_resolve_tool_honors_optional_path_glob() -> None:
    """A tool policy with a ``path_pattern`` matches only when the call's path matches."""
    policies = [
        {
            "scope": "session",
            "scope_id": "s",
            "tool_name_pattern": "shell.*",
            "path_pattern": "/tmp/*",
            "action": "allow",
        }
    ]
    assert resolve("tool", "shell.exec", policies=policies, session_id="s", path="/tmp/x") == "allow"
    assert resolve("tool", "shell.exec", policies=policies, session_id="s", path="/etc/x") == ""


@pytest.mark.parametrize("mode", ["plan", "architect", "edit", "chat"])
def test_read_only_fast_allows_before_mode_lock(tmp_path: Path, mode: str) -> None:
    """is_read_only is the gate's FIRST branch: a read fast-allows in EVERY mode.

    In plan/architect a destructive tool is hard-denied — but a provably read-only call
    must never reach that lock. Ordering test: no mode gates a read.
    """
    app = build_app(sessions_path=tmp_path / "s.json")
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t", "mode": mode}).json()["id"]
        gate = _make_permission_gate(app)
        with _tool_session_context(sid):
            assert gate("fs_read_file", {"filepath": "x"}) == "allow"
        # And no permission row was recorded for the read (it never reached the lock/prompt).
        assert app.state.permissions == {}


def test_is_read_only_signals() -> None:
    """The positive allowlist: catalog ``read`` tag OR a real ``readOnlyHint`` annotation."""
    assert is_read_only("tool", "fs_read_file", {}, None) is True
    assert is_read_only("tool", "fs_propose_edit", {}, None) is True  # data fix: tagged read
    assert is_read_only("tool", "fs_apply_edit_write", {}, None) is False  # tagged write
    assert is_read_only("tool", "shell_bash", {"command": "date"}, None) is False
    assert is_read_only("tool", "not_in_catalog", {}, None) is False
    ctx = {"kind": "external_mcp", "annotations": {"readOnlyHint": True, "destructiveHint": False}}
    assert is_read_only("tool", "remote.lookup", {}, ctx) is True
    bad = {"kind": "external_mcp", "annotations": {"readOnlyHint": True, "destructiveHint": True}}
    assert is_read_only("tool", "remote.lookup", {}, bad) is False


def test_grant_record_synthesizes_kind_for_legacy_rows() -> None:
    """A legacy row without ``kind`` normalizes: kind synthesized from the set pattern field."""
    assert GrantRecord.from_policy_row(
        {"scope": "session", "tool_name_pattern": "shell.*", "action": "deny"}
    ).kind == KIND_TOOL
    assert GrantRecord.from_policy_row(
        {"scope": "workspace", "scope_id": "w", "host_pattern": "ok.test", "action": "allow"}
    ).kind == KIND_DOMAIN
    assert GrantRecord.from_policy_row(
        {"scope": "session", "path_pattern": "/tmp/*", "action": "ask"}
    ).kind == KIND_ROOT


def test_grant_record_round_trips_through_policy_row() -> None:
    """from_policy_row → to_policy_row preserves kind, pattern, scope, and action semantics."""
    row = {
        "scope": "workspace",
        "scope_id": "ws_a",
        "host_pattern": "ok.test",
        "action": "allow",
        "created_from_permission_id": "perm_1",
    }
    grant = GrantRecord.from_policy_row(row)
    assert grant.kind == KIND_DOMAIN
    assert grant.pattern == "ok.test"
    assert grant.decision == "allow"
    round_tripped = grant.to_policy_row()
    assert round_tripped["kind"] == KIND_DOMAIN
    assert round_tripped["host_pattern"] == "ok.test"
    assert round_tripped["scope"] == "workspace"
    assert round_tripped["scope_id"] == "ws_a"
    assert round_tripped["action"] == "allow"
    # The reconstructed row resolves to the same decision through the one matcher.
    assert resolve("domain", "ok.test", policies=[round_tripped], workspace_id="ws_a") == "allow"


def test_domain_kind_preserves_session_scope_leak_guard() -> None:
    """resolve(kind="domain") honours ONLY an explicit workspace-scoped host row (leak guard)."""
    policies = [
        # session-scoped host grant — must NEVER match at the chokepoint for any workspace.
        {"scope": "session", "scope_id": "sess_a", "action": "allow", "host_pattern": "leak.test"},
        # empty workspace scope_id — must NOT act as a wildcard.
        {"scope": "workspace", "scope_id": "", "action": "allow", "host_pattern": "wild.test"},
        # a proper workspace-scoped grant — honoured only for ws_a.
        {"scope": "workspace", "scope_id": "ws_a", "action": "allow", "host_pattern": "ok.test"},
    ]
    assert resolve("domain", "leak.test", policies=policies, workspace_id="ws_a") == ""
    assert resolve("domain", "leak.test", policies=policies, workspace_id="ws_b") == ""
    assert resolve("domain", "wild.test", policies=policies, workspace_id="ws_a") == ""
    assert resolve("domain", "ok.test", policies=policies, workspace_id="ws_a") == "allow"
    assert resolve("domain", "ok.test", policies=policies, workspace_id="ws_b") == ""


def test_tool_kind_treats_empty_scope_id_as_wildcard() -> None:
    """Per-kind divergence: unlike domain, a tool policy's empty scope_id IS a scope wildcard."""
    policies = [{"scope": "session", "scope_id": "", "tool_name_pattern": "*", "action": "deny"}]
    assert resolve("tool", "anything", policies=policies, session_id="sess_x") == "deny"
