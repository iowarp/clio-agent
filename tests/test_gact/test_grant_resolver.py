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
    KIND_HOOK,
    KIND_PLAN_ACL,
    KIND_ROOT,
    KIND_TOOL,
    GrantRecord,
    is_read_only,
    migrate_priorities,
    resolve,
)
from clio_agent.gact.runtime.permission_policies import (
    _host_action_for,
    _validate_permission_policies,
)

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


def test_catalog_readwrite_tags_are_annotation_projections() -> None:
    """#1061: the built-in read/write tags are PROJECTIONS of the declared MCP annotations.

    The declared annotations are the single source of truth; the catalog tag is derived from
    them via ``classification_tags``, not hand-authored — and the 4 known tools keep their
    pre-#1061 read/write classification (backward compatible).
    """
    from clio_agent.tools.catalog import (
        _BUILTIN_ANNOTATIONS,
        classification_tags,
        get_tool_entry,
    )

    for name, tag in (
        ("fs_read_file", "read"),
        ("fs_propose_edit", "read"),
        ("fs_apply_edit_write", "write"),
    ):
        assert classification_tags(_BUILTIN_ANNOTATIONS[name]) == frozenset({tag})
        assert tag in get_tool_entry(name).tags
    # shell_bash is open-world effectful: NEITHER read nor write (its writes/egress live behind
    # the OS fence, not a catalog write tag).
    assert classification_tags(_BUILTIN_ANNOTATIONS["shell_bash"]) == frozenset()
    shell_tags = get_tool_entry("shell_bash").tags
    assert "read" not in shell_tags
    assert "write" not in shell_tags


def test_unannotated_tool_fails_safe_to_not_read_only() -> None:
    """#1061 fail-safe: a tool with NO annotations is NOT read-only and projects effectful.

    ``classification_tags(None)`` yields no ``read`` tag (most-restrictive default), so
    ``is_read_only`` returns False for an unannotated/unknown tool — whether via the catalog
    signal (built-in path) or an external-MCP context carrying no valid ``readOnlyHint``.
    """
    from clio_agent.tools.catalog import classification_tags

    assert classification_tags(None) == frozenset()
    assert classification_tags({"readOnlyHint": "true"}) == frozenset()  # malformed -> fail closed
    assert is_read_only("tool", "totally_unknown_tool", {}, None) is False
    absent_ctx = {"kind": "external_mcp", "annotations": None}
    assert is_read_only("tool", "totally_unknown_tool", {}, absent_ctx) is False


def test_partial_annotations_missing_open_world_hint_are_not_write() -> None:
    """#1061 fail-safe: a mutating tool that OMITS ``openWorldHint`` is NOT catalog ``write``.

    Per the MCP spec ``openWorldHint`` defaults to ``True`` (open-world) when absent, so a
    bounded ``write`` (auto-approvable under ``auto-edits``) must POSITIVELY declare
    ``openWorldHint=False``. A present-but-partial block that declares an effect but omits
    ``openWorldHint`` must fall through to ``frozenset()`` — never auto-approve as bounded.
    """
    from clio_agent.tools.catalog import classification_tags

    assert classification_tags({"destructiveHint": True}) == frozenset()
    assert classification_tags({"readOnlyHint": False}) == frozenset()
    # An explicit open-world mutation is likewise not a bounded write.
    assert classification_tags({"destructiveHint": True, "openWorldHint": True}) == frozenset()
    # Positively-declared closed-world mutation still classifies write.
    assert classification_tags({"destructiveHint": True, "openWorldHint": False}) == frozenset(
        {"write"}
    )


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


# ---- P0.1 priority bands (#1059) -----------------------------------------------------------


def test_golden_legacy_no_priority_reproduces_first_match() -> None:
    """MIGRATION GOLDEN: legacy (no-priority) rows resolve IDENTICALLY to first-match.

    An allow-THEN-deny pair on the SAME tool must return the EARLIER row's action, and the
    reversed order must flip the result — proving the descending-by-index migration reproduces
    the old first-match scan exactly (the most-restrictive tie-break never fires for legacy rows).
    """
    allow_then_deny = [
        {"scope": "session", "scope_id": "s", "tool_name_pattern": "shell.exec", "action": "allow"},
        {"scope": "session", "scope_id": "s", "tool_name_pattern": "shell.exec", "action": "deny"},
    ]
    assert resolve("tool", "shell.exec", policies=allow_then_deny, session_id="s") == "allow"

    deny_then_allow = [
        {"scope": "session", "scope_id": "s", "tool_name_pattern": "shell.exec", "action": "deny"},
        {"scope": "session", "scope_id": "s", "tool_name_pattern": "shell.exec", "action": "allow"},
    ]
    assert resolve("tool", "shell.exec", policies=deny_then_allow, session_id="s") == "deny"


def test_priority_band_higher_number_wins_across_bands() -> None:
    """Highest-priority band wins: a lower-priority deny loses to a higher-priority allow."""
    policies = [
        {"scope": "session", "scope_id": "s", "tool_name_pattern": "*", "action": "deny", "priority": 40},
        {
            "scope": "session",
            "scope_id": "s",
            "tool_name_pattern": "fs_read_file",
            "action": "allow",
            "priority": 50,
        },
    ]
    # allow@50 outranks deny@40 for the read-only subject.
    assert resolve("tool", "fs_read_file", policies=policies, session_id="s") == "allow"


def test_priority_band_specific_write_deny_outranks_broad_allow() -> None:
    """A higher-priority write-scoped deny beats a lower-priority broad allow."""
    policies = [
        {"scope": "session", "scope_id": "s", "tool_name_pattern": "*", "action": "allow", "priority": 40},
        {
            "scope": "session",
            "scope_id": "s",
            "tool_name_pattern": "fs_apply_edit_write",
            "action": "deny",
            "priority": 65,
        },
    ]
    assert resolve("tool", "fs_apply_edit_write", policies=policies, session_id="s") == "deny"


def test_priority_band_highest_path_allow_wins() -> None:
    """An even-higher-priority path-scoped allow wins over a write deny for the matching path."""
    policies = [
        {
            "scope": "session",
            "scope_id": "s",
            "tool_name_pattern": "fs_apply_edit_write",
            "action": "deny",
            "priority": 65,
        },
        {
            "scope": "session",
            "scope_id": "s",
            "tool_name_pattern": "fs_apply_edit_write",
            "path_pattern": "/tmp/*",
            "action": "allow",
            "priority": 70,
        },
    ]
    assert (
        resolve(
            "tool",
            "fs_apply_edit_write",
            policies=policies,
            session_id="s",
            path="/tmp/x",
        )
        == "allow"
    )
    # A path outside the allow band still falls to the write deny.
    assert (
        resolve(
            "tool",
            "fs_apply_edit_write",
            policies=policies,
            session_id="s",
            path="/etc/x",
        )
        == "deny"
    )


def test_priority_tie_break_most_restrictive_wins() -> None:
    """A TIE at the highest band resolves to the MOST-RESTRICTIVE action (deny > allow)."""
    policies = [
        {"scope": "session", "scope_id": "s", "tool_name_pattern": "shell.exec", "action": "allow", "priority": 50},
        {"scope": "session", "scope_id": "s", "tool_name_pattern": "shell.exec", "action": "deny", "priority": 50},
    ]
    assert resolve("tool", "shell.exec", policies=policies, session_id="s") == "deny"
    # Order-independent: the tie-break, not insertion order, decides.
    assert resolve("tool", "shell.exec", policies=list(reversed(policies)), session_id="s") == "deny"


def test_grant_record_round_trips_priority() -> None:
    """A GrantRecord carries an integer ``priority`` through the policy-row round-trip."""
    row = {
        "scope": "session",
        "scope_id": "s",
        "tool_name_pattern": "shell.*",
        "action": "deny",
        "priority": 42,
    }
    grant = GrantRecord.from_policy_row(row)
    assert grant.priority == 42
    assert grant.to_policy_row()["priority"] == 42
    # A legacy row without a priority yields ``None`` (migration owns the default).
    assert GrantRecord.from_policy_row({"scope": "session", "action": "deny"}).priority is None


def test_validate_rejects_malformed_priority() -> None:
    """A non-integer (or bool) priority is REJECTED with a typed reason (no silent default)."""
    _clean, errors = _validate_permission_policies(
        [{"scope": "session", "action": "deny", "priority": "high"}]
    )
    assert any(e["field"] == "priority" for e in errors)
    _clean, bool_errors = _validate_permission_policies(
        [{"scope": "session", "action": "deny", "priority": True}]
    )
    assert any(e["field"] == "priority" for e in bool_errors)
    # A valid integer priority passes and is preserved.
    clean, ok_errors = _validate_permission_policies(
        [{"scope": "session", "action": "deny", "priority": 55}]
    )
    assert ok_errors == []
    assert clean[0]["priority"] == 55


def test_migrate_priorities_stamps_descending_by_index() -> None:
    """Migration stamps unique DESCENDING priorities (first row highest) on legacy rows only."""
    rows = [
        {"scope": "session", "action": "allow"},
        {"scope": "session", "action": "deny"},
        {"scope": "session", "action": "ask", "priority": 999},
    ]
    migrate_priorities(rows)
    assert rows[0]["priority"] == 3
    assert rows[1]["priority"] == 2
    assert rows[2]["priority"] == 999  # explicit priority left untouched


# ---- P0.1 follow-up: sticky runtime appends must not collide with migrated rows -------------


def test_sticky_append_does_not_collide_with_migrated_legacy_row(tmp_path: Path) -> None:
    """REGRESSION: a sticky runtime append must keep appended-last (lowest) precedence.

    Reproduces the confirmed review probe: a store already loaded/migrated with a single legacy
    ``allow(tool='git.*')`` row (migrated to ``priority=1``, since it is the sole row) THEN gets a
    runtime sticky ``deny(tool='git.push')`` row appended through the REAL app code path
    (``_apply_kind_grant`` -> ``_grant_workspace_tool``, exactly what
    ``POST /v1/workspaces/{wid}/grants`` with ``kind="tool"`` calls) — not a hand-built dict.

    Before the fix, the appended row got no explicit ``priority``, so ``resolve()``'s live
    ``_effective_priority`` computed it as ``total - index = 2 - 1 = 1`` -- colliding with the
    legacy row's migrated ``priority=1`` -- and the most-restrictive tie-break fired, flipping the
    result to ``deny``. The fix must keep first-match (appended-last = lowest precedence): the
    legacy ``allow`` row, matched first under the OLD scan order, must still win.
    """
    from clio_agent.gact.routes.workspaces import _apply_kind_grant

    app = build_app(sessions_path=tmp_path / "s.json")
    root = tmp_path / "proj"
    root.mkdir()
    ws = app.state.workspaces.create(name="p", root_path=str(root))

    legacy_row = {
        "scope": "workspace",
        "scope_id": ws.id,
        "tool_name_pattern": "git.*",
        "action": "allow",
    }
    migrate_priorities([legacy_row])
    assert legacy_row["priority"] == 1  # sole row -> migrated to priority=1
    app.state.permission_policies = [legacy_row]

    _apply_kind_grant(app, ws.id, "tool", {"pattern": "git.push", "decision": "deny"})

    assert len(app.state.permission_policies) == 2
    appended = app.state.permission_policies[1]
    assert appended["action"] == "deny"
    assert appended["priority"] != legacy_row["priority"]  # no collision

    result = resolve(
        "tool", "git.push", policies=app.state.permission_policies, workspace_id=ws.id
    )
    assert result == "allow"  # appended-last preserved: first-match (allow) still wins


def test_two_successive_sticky_appends_both_preserve_first_match(tmp_path: Path) -> None:
    """Two successive sticky appends each get a strictly-lower unique priority (monotonic)."""
    from clio_agent.gact.routes.workspaces import _apply_kind_grant

    app = build_app(sessions_path=tmp_path / "s.json")
    root = tmp_path / "proj"
    root.mkdir()
    ws = app.state.workspaces.create(name="p", root_path=str(root))

    legacy_row = {
        "scope": "workspace",
        "scope_id": ws.id,
        "tool_name_pattern": "git.*",
        "action": "allow",
    }
    migrate_priorities([legacy_row])
    app.state.permission_policies = [legacy_row]

    _apply_kind_grant(app, ws.id, "tool", {"pattern": "git.push", "decision": "deny"})
    _apply_kind_grant(app, ws.id, "tool", {"pattern": "git.rebase", "decision": "deny"})

    priorities = [row["priority"] for row in app.state.permission_policies]
    assert len(set(priorities)) == 3  # all three rows uniquely prioritized, no collisions

    # First-match preserved for BOTH appended tools: the legacy allow still wins for each.
    assert (
        resolve("tool", "git.push", policies=app.state.permission_policies, workspace_id=ws.id)
        == "allow"
    )
    assert (
        resolve("tool", "git.rebase", policies=app.state.permission_policies, workspace_id=ws.id)
        == "allow"
    )


# ---- P0.2 modes axis + event axis + plan_acl/hook kinds (#1060) ----------------------------


def test_modes_axis_narrows_to_matching_mode() -> None:
    """A row with a non-empty ``modes`` matches ONLY when ``mode`` is one of its entries.

    A deny scoped ``modes=[plan]`` fires under ``mode="plan"`` but is skipped under ``mode="edit"``
    and the default ``mode=""`` — for the latter cases a lower-priority allow row (or ``""``) wins.
    """
    policies = [
        {
            "scope": "session",
            "scope_id": "s",
            "tool_name_pattern": "shell.exec",
            "action": "deny",
            "modes": ["plan"],
            "priority": 50,
        },
        {
            "scope": "session",
            "scope_id": "s",
            "tool_name_pattern": "shell.exec",
            "action": "allow",
            "priority": 10,
        },
    ]
    # plan: the higher-priority plan-scoped deny is in scope and wins.
    assert resolve("tool", "shell.exec", policies=policies, session_id="s", mode="plan") == "deny"
    # edit: the plan-scoped deny is skipped; the lower allow is the only match.
    assert resolve("tool", "shell.exec", policies=policies, session_id="s", mode="edit") == "allow"
    # default mode="": the plan-scoped deny is skipped; allow wins.
    assert resolve("tool", "shell.exec", policies=policies, session_id="s") == "allow"

    # With ONLY the plan-scoped deny present, a non-plan mode matches nothing -> "".
    only_plan = [policies[0]]
    assert resolve("tool", "shell.exec", policies=only_plan, session_id="s", mode="edit") == ""
    assert resolve("tool", "shell.exec", policies=only_plan, session_id="s") == ""


def test_on_event_axis_narrows_to_matching_event() -> None:
    """A row with a non-empty ``on`` matches ONLY the named event; absent ``on`` matches any."""
    scoped = [
        {
            "scope": "session",
            "scope_id": "s",
            "tool_name_pattern": "shell.exec",
            "action": "deny",
            "on": ["PreToolUse"],
        }
    ]
    assert (
        resolve("tool", "shell.exec", policies=scoped, session_id="s", event="PreToolUse")
        == "deny"
    )
    # A different event (and the default empty event) does not match the on-scoped row.
    assert (
        resolve("tool", "shell.exec", policies=scoped, session_id="s", event="PostToolUse") == ""
    )
    assert resolve("tool", "shell.exec", policies=scoped, session_id="s") == ""

    # A row with NO ``on`` matches any event (backward compat).
    unscoped = [
        {"scope": "session", "scope_id": "s", "tool_name_pattern": "shell.exec", "action": "deny"}
    ]
    assert (
        resolve("tool", "shell.exec", policies=unscoped, session_id="s", event="PreToolUse")
        == "deny"
    )
    assert resolve("tool", "shell.exec", policies=unscoped, session_id="s") == "deny"


def test_axis_backward_compat_rows_without_modes_or_on() -> None:
    """Rows carrying NO ``modes``/``on`` resolve IDENTICALLY to P0.1, with the new params defaulted."""
    # Re-assert the priority-band and tie-break cases with mode/event supplied — no change.
    banded = [
        {"scope": "session", "scope_id": "s", "tool_name_pattern": "*", "action": "deny", "priority": 40},
        {
            "scope": "session",
            "scope_id": "s",
            "tool_name_pattern": "fs_read_file",
            "action": "allow",
            "priority": 50,
        },
    ]
    assert (
        resolve("tool", "fs_read_file", policies=banded, session_id="s", mode="plan", event="X")
        == "allow"
    )
    tie = [
        {"scope": "session", "scope_id": "s", "tool_name_pattern": "shell.exec", "action": "allow", "priority": 50},
        {"scope": "session", "scope_id": "s", "tool_name_pattern": "shell.exec", "action": "deny", "priority": 50},
    ]
    assert (
        resolve("tool", "shell.exec", policies=tie, session_id="s", mode="edit", event="Y")
        == "deny"
    )


def test_plan_acl_and_hook_kinds_round_trip_through_grant_record() -> None:
    """The new ``plan_acl``/``hook`` kinds round-trip through GrantRecord (kind preserved)."""
    for kind in (KIND_PLAN_ACL, KIND_HOOK):
        row = {
            "kind": kind,
            "scope": "session",
            "scope_id": "s",
            "tool_name_pattern": "shell.*",
            "action": "deny",
        }
        grant = GrantRecord.from_policy_row(row)
        assert grant.kind == kind
        assert grant.pattern == "shell.*"
        assert grant.decision == "deny"
        round_tripped = grant.to_policy_row()
        assert round_tripped["kind"] == kind
        assert round_tripped["tool_name_pattern"] == "shell.*"
        assert round_tripped["scope"] == "session"
        assert round_tripped["action"] == "deny"


def test_plan_acl_and_hook_grants_preserve_axis_fields_through_grant_record() -> None:
    """REGRESSION: GrantRecord must NOT silently drop ``modes``/``on`` on round-trip (review finding).

    Before the fix, ``from_policy_row`` never captured the axis fields and ``to_policy_row`` never
    re-emitted them, so a ``plan_acl`` grant scoped ``modes=["plan"]`` (or a ``hook`` grant scoped
    ``on=["PreToolUse"]``) built through ``GrantRecord`` would silently WIDEN to match any
    mode/event the moment it was persisted via ``to_policy_row`` -- exactly the latent
    grant-widening P1.1/P2's plan-ACL/hook authoring would hit.
    """
    plan_row = {
        "kind": KIND_PLAN_ACL,
        "scope": "session",
        "scope_id": "s",
        "tool_name_pattern": "shell.*",
        "action": "deny",
        "modes": ["plan"],
    }
    plan_grant = GrantRecord.from_policy_row(plan_row)
    assert plan_grant.modes == ("plan",)
    assert plan_grant.on == ()
    plan_round_tripped = plan_grant.to_policy_row()
    assert plan_round_tripped["modes"] == ["plan"]
    assert "on" not in plan_round_tripped  # empty axis: no key added (backward-compat row shape)

    hook_row = {
        "kind": KIND_HOOK,
        "scope": "session",
        "scope_id": "s",
        "tool_name_pattern": "shell.*",
        "action": "deny",
        "on": ["PreToolUse"],
    }
    hook_grant = GrantRecord.from_policy_row(hook_row)
    assert hook_grant.on == ("PreToolUse",)
    assert hook_grant.modes == ()
    hook_round_tripped = hook_grant.to_policy_row()
    assert hook_round_tripped["on"] == ["PreToolUse"]
    assert "modes" not in hook_round_tripped

    # The round-tripped rows still enforce the SAME axis-narrowed decision through resolve().
    assert (
        resolve(
            "tool",
            "shell.exec",
            policies=[plan_round_tripped],
            session_id="s",
            mode="plan",
        )
        == "deny"
    )
    assert (
        resolve(
            "tool",
            "shell.exec",
            policies=[plan_round_tripped],
            session_id="s",
            mode="edit",
        )
        == ""
    )
    assert (
        resolve(
            "tool",
            "shell.exec",
            policies=[hook_round_tripped],
            session_id="s",
            event="PreToolUse",
        )
        == "deny"
    )
    assert (
        resolve(
            "tool",
            "shell.exec",
            policies=[hook_round_tripped],
            session_id="s",
            event="PostToolUse",
        )
        == ""
    )

    # A row with NO axis fields at all still round-trips to a row with NO axis keys (no accretion).
    plain_row = {
        "kind": KIND_TOOL,
        "scope": "session",
        "scope_id": "s",
        "tool_name_pattern": "shell.*",
        "action": "deny",
    }
    plain_round_tripped = GrantRecord.from_policy_row(plain_row).to_policy_row()
    assert "modes" not in plain_round_tripped
    assert "on" not in plain_round_tripped


def test_validate_rejects_malformed_modes_and_on() -> None:
    """A non-list (or non-string entry) ``modes``/``on`` is REJECTED with a typed reason."""
    _clean, modes_not_list = _validate_permission_policies(
        [{"scope": "session", "action": "deny", "modes": "plan"}]
    )
    assert any(e["field"] == "modes" for e in modes_not_list)

    _clean, modes_bad_entry = _validate_permission_policies(
        [{"scope": "session", "action": "deny", "modes": ["plan", 3]}]
    )
    assert any(e["field"] == "modes" for e in modes_bad_entry)

    _clean, on_not_list = _validate_permission_policies(
        [{"scope": "session", "action": "deny", "on": {"PreToolUse": True}}]
    )
    assert any(e["field"] == "on" for e in on_not_list)

    _clean, on_bad_entry = _validate_permission_policies(
        [{"scope": "session", "action": "deny", "on": ["PreToolUse", None]}]
    )
    assert any(e["field"] == "on" for e in on_bad_entry)

    # Well-formed axis fields (and the new kinds) pass validation and are preserved.
    clean, ok_errors = _validate_permission_policies(
        [
            {
                "kind": KIND_PLAN_ACL,
                "scope": "session",
                "action": "deny",
                "modes": ["plan"],
                "on": ["PreToolUse"],
            }
        ]
    )
    assert ok_errors == []
    assert clean[0]["modes"] == ["plan"]
    assert clean[0]["on"] == ["PreToolUse"]
    assert clean[0]["kind"] == KIND_PLAN_ACL
