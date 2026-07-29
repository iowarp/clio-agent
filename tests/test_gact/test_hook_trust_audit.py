"""P2.7 #1075 — hook trust (content fingerprints), the ``GET /v1/hooks`` inspection
view, the ``allowManagedHooksOnly`` lockdown, and audit-on-the-semantic-highway.

Four invariants:

* a hook whose content fingerprint is UNCHANGED runs; a CHANGED fingerprint marks it
  ``untrusted`` and it does NOT run (never a silent run of rewritten content);
* ``allowManagedHooksOnly`` drops every non-managed (project/user) source, keeping
  only managed/admin hooks;
* ``GET /v1/hooks`` lists every loaded hook with its source scope, trust, and enabled
  state (the read-only debugging surface that replaced the deleted CRUD);
* every hook invocation (allow / deny / error / a pre-execution rejection) emits
  EXACTLY ONE ``hook.invoked`` audit event on the semantic highway — never a new store.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context as _ctx
from clio_agent.gact.app import _make_permission_gate, build_app
from clio_agent.gact.hooks import (
    PRE_TOOL_USE,
    SEMANTIC_EVENT,
    HookEnvelope,
    build_hook_dispatcher,
    compute_fingerprint,
    discover_hook_entries,
    install_global_dispatcher,
    install_hook_audit_emitter,
)
from clio_agent.gact.permission_gate import DenyDecision
from tests._config_layer import set_config
from tests.test_gact._hook_fixtures import make_command_dispatcher, write_hook_script

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def _pre_tool_env(name: str = "hdf5_write") -> HookEnvelope:
    return HookEnvelope(
        hook_event_name=PRE_TOOL_USE,
        session_id="sess-1",
        turn_id="turn-1",
        tool_name=name,
        tool_input={"path": "/tmp/x"},
        tool_annotations={"readOnly": False, "destructive": True, "openWorld": False},
    )


def _write_hooks_json(path: Path, script: Path, *, hook_id: str = "guard") -> None:
    path.write_text(
        json.dumps(
            {
                "hooks": [
                    {
                        "id": hook_id,
                        "on": [PRE_TOOL_USE],
                        "run": {"type": "command", "command": sys.executable, "args": [str(script)]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# TRUST — content fingerprint (unchanged runs; changed => untrusted, no run)     #
# --------------------------------------------------------------------------- #


def test_unchanged_fingerprint_stays_trusted_and_runs(tmp_path: Path) -> None:
    """A hook whose content is unchanged across two loads stays trusted and dispatches."""

    script = write_hook_script(
        tmp_path, "guard.py", "import json\nprint(json.dumps({'decision':'allow'}))\n"
    )
    hooks_json = tmp_path / "hooks.json"
    _write_hooks_json(hooks_json, script)
    trust_store = tmp_path / "hooks.trust.json"
    set_config("hooks.config", str(hooks_json))
    set_config("hooks.trust_store", str(trust_store))

    first = build_hook_dispatcher()
    assert first.inspect()[0]["trust"] == "trusted"
    assert first.matching(PRE_TOOL_USE, _pre_tool_env())  # runs
    assert trust_store.is_file()  # TOFU fingerprint persisted

    # A second load with the SAME content: still trusted, still runs.
    second = build_hook_dispatcher()
    assert second.inspect()[0]["trust"] == "trusted"
    assert second.matching(PRE_TOOL_USE, _pre_tool_env())


def test_changed_fingerprint_marks_untrusted_and_does_not_run(tmp_path: Path) -> None:
    """Editing the hook script (a git-pull rewrite) flips it to ``untrusted``; it no
    longer dispatches — the change never runs silently."""

    script = write_hook_script(
        tmp_path, "guard.py", "import json\nprint(json.dumps({'decision':'allow'}))\n"
    )
    hooks_json = tmp_path / "hooks.json"
    _write_hooks_json(hooks_json, script)
    trust_store = tmp_path / "hooks.trust.json"
    set_config("hooks.config", str(hooks_json))
    set_config("hooks.trust_store", str(trust_store))

    trusted = build_hook_dispatcher()
    assert trusted.matching(PRE_TOOL_USE, _pre_tool_env())  # trusted first-use runs

    # Rewrite the script body (deny instead of allow): the fingerprint changes.
    script.write_text(
        "import json\nprint(json.dumps({'decision':'deny','reason':'evil'}))\n", encoding="utf-8"
    )
    changed = build_hook_dispatcher()
    row = changed.inspect()[0]
    assert row["trust"] == "untrusted"
    assert row["runs"] is False
    assert changed.matching(PRE_TOOL_USE, _pre_tool_env()) == []  # does NOT run


def test_fingerprint_covers_resolved_script_content(tmp_path: Path) -> None:
    """The fingerprint hashes the RESOLVED script bytes, not just the argv string."""

    script = write_hook_script(tmp_path, "s.py", "print('{}')\n")
    hooks_json = tmp_path / "hooks.json"
    _write_hooks_json(hooks_json, script)
    set_config("hooks.config", str(hooks_json))
    set_config("hooks.trust_store", str(tmp_path / "t.json"))
    before = compute_fingerprint(build_hook_dispatcher().entries[0])
    script.write_text("print('changed')\n", encoding="utf-8")
    after = compute_fingerprint(build_hook_dispatcher().entries[0])
    assert before != after


# --------------------------------------------------------------------------- #
# allowManagedHooksOnly — drop non-managed sources                              #
# --------------------------------------------------------------------------- #


def test_allow_managed_only_drops_project_and_user_keeps_managed(tmp_path: Path) -> None:
    user = tmp_path / "user.json"
    project = tmp_path / "project.json"
    managed = tmp_path / "managed.json"
    for path, hid in ((user, "user-hook"), (project, "project-hook"), (managed, "admin-hook")):
        path.write_text(
            json.dumps(
                {"hooks": [{"id": hid, "on": [PRE_TOOL_USE], "run": {"type": "prompt", "prompt": "x"}}]}
            ),
            encoding="utf-8",
        )

    everything = discover_hook_entries(
        user_config_path=user, project_config_path=project, managed_config_path=managed
    )
    assert {e.scope for e in everything} == {"user", "project", "managed"}

    locked = discover_hook_entries(
        user_config_path=user,
        project_config_path=project,
        managed_config_path=managed,
        allow_managed_only=True,
    )
    assert [e.id for e in locked] == ["admin-hook"]
    assert {e.scope for e in locked} == {"managed"}


def test_build_dispatcher_honors_allow_managed_only_env(tmp_path: Path) -> None:
    managed = tmp_path / "managed.json"
    managed.write_text(
        json.dumps(
            {"hooks": [{"id": "admin", "on": [PRE_TOOL_USE], "run": {"type": "prompt", "prompt": "x"}}]}
        ),
        encoding="utf-8",
    )
    user = tmp_path / "user.json"
    user.write_text(
        json.dumps(
            {"hooks": [{"id": "user", "on": [PRE_TOOL_USE], "run": {"type": "prompt", "prompt": "x"}}]}
        ),
        encoding="utf-8",
    )
    set_config("hooks.config", str(user))
    set_config("hooks.managed_config", str(managed))
    set_config("hooks.allow_managed_only", True)
    set_config("hooks.trust_store", str(tmp_path / "t.json"))
    disp = build_hook_dispatcher()
    assert [e.id for e in disp.entries] == ["admin"]


# --------------------------------------------------------------------------- #
# GET /v1/hooks — read-only inspection                                          #
# --------------------------------------------------------------------------- #


def test_get_v1_hooks_lists_loaded_hooks_with_source_trust_enabled(tmp_path: Path) -> None:
    disp = make_command_dispatcher(
        tmp_path,
        event=PRE_TOOL_USE,
        body="import json\nprint(json.dumps({'decision':'allow'}))\n",
        hook_id="guard",
        match={"tool": "hdf5_write"},
    )
    install_global_dispatcher(disp)
    try:
        app = build_app(sessions_path=tmp_path / "s.json")
        client = TestClient(app)
        body = client.get("/v1/hooks").json()
        assert body["backend"] == "declarative"
        assert len(body["hooks"]) == 1
        row = body["hooks"][0]
        assert row["id"] == "guard"
        assert row["on"] == [PRE_TOOL_USE]
        assert row["enabled"] is True
        assert "trust" in row and "source" in row
        assert row["match"]["tool"] == "^hdf5_write$"
        assert "recent_invocations" in body
    finally:
        install_global_dispatcher(None)


# --------------------------------------------------------------------------- #
# AUDIT — exactly one hook.invoked per invocation (allow / deny / error)          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("body", "fail_closed", "want_decision", "want_status"),
    [
        ("import json\nprint(json.dumps({'decision':'allow'}))\n", False, "allow", "completed"),
        ("import json\nprint(json.dumps({'decision':'deny','reason':'no'}))\n", False, "deny", "denied"),
        ("import sys\nsys.exit(1)\n", True, "deny", "error"),
    ],
)
def test_audit_emits_exactly_one_event_per_invocation(
    tmp_path: Path, body: str, fail_closed: bool, want_decision: str, want_status: str
) -> None:
    captured: list[dict] = []
    install_hook_audit_emitter(captured.append)
    try:
        disp = make_command_dispatcher(
            tmp_path, event=PRE_TOOL_USE, body=body, hook_id="guard", fail_closed=fail_closed
        )
        disp.dispatch(PRE_TOOL_USE, _pre_tool_env())
        assert len(captured) == 1
        rec = captured[0]
        assert rec["hook_id"] == "guard"
        assert rec["event"] == PRE_TOOL_USE
        assert rec["decision"] == want_decision
        assert rec["status"] == want_status
        assert rec["session_id"] == "sess-1"
    finally:
        install_hook_audit_emitter(None)


def test_audit_emits_one_event_per_hook_for_multiple_hooks(tmp_path: Path) -> None:
    captured: list[dict] = []
    install_hook_audit_emitter(captured.append)
    try:
        from tests.test_gact._hook_fixtures import command_run, dispatcher_from_rows

        s1 = write_hook_script(tmp_path, "a.py", "import json\nprint(json.dumps({'decision':'allow'}))\n")
        s2 = write_hook_script(tmp_path, "b.py", "import json\nprint(json.dumps({'decision':'allow'}))\n")
        disp = dispatcher_from_rows(
            [
                {"id": "a", "on": [PRE_TOOL_USE], "run": command_run(s1)},
                {"id": "b", "on": [PRE_TOOL_USE], "run": command_run(s2)},
            ]
        )
        disp.dispatch(PRE_TOOL_USE, _pre_tool_env())
        assert sorted(r["hook_id"] for r in captured) == ["a", "b"]
    finally:
        install_hook_audit_emitter(None)


def test_semantic_event_invocations_are_not_audited(tmp_path: Path) -> None:
    """A SemanticEvent-hook invocation is NOT audited (recursion guard)."""

    captured: list[dict] = []
    install_hook_audit_emitter(captured.append)
    try:
        disp = make_command_dispatcher(
            tmp_path,
            event=SEMANTIC_EVENT,
            body="import json\nprint(json.dumps({'decision':'allow'}))\n",
            hook_id="obs",
        )
        disp.dispatch(SEMANTIC_EVENT, HookEnvelope(hook_event_name=SEMANTIC_EVENT, session_id="s"))
        assert captured == []
    finally:
        install_hook_audit_emitter(None)


# --------------------------------------------------------------------------- #
# AUDIT — a pre-execution rejection lands on the semantic highway (trace)         #
# --------------------------------------------------------------------------- #


def test_pre_execution_rejection_audits_on_the_trace(tmp_path: Path) -> None:
    """A PreToolUse hook deny (a pre-execution rejection driven through the gate) emits
    exactly one ``hook.invoked`` audit event on the durable semantic trace."""

    trace_dir = tmp_path / "traces"
    set_config("trace.backend", "file")
    set_config("trace.path", str(trace_dir))
    body = "import json\nprint(json.dumps({'decision':'deny','reason':'blocked'}))\n"
    install_global_dispatcher(
        make_command_dispatcher(tmp_path, event=PRE_TOOL_USE, body=body, hook_id="guard")
    )
    try:
        app = build_app(sessions_path=tmp_path / "s.json")
        client = TestClient(app)
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        gate = _make_permission_gate(app)
        token = _ctx.set_app(app)
        try:
            decision = gate("hdf5_write", {"path": "/tmp/x"})
        finally:
            _ctx.reset(token)
        assert isinstance(decision, DenyDecision)

        app.state.semantic_trace_backend.flush()
        trace_path = trace_dir / f"{sid}.semantic.jsonl"
        rows = [json.loads(line) for line in trace_path.read_text().splitlines()]
        invoked = [r for r in rows if r["event_type"] == "hook.invoked"]
        assert len(invoked) == 1
        assert invoked[0]["payload"]["decision"] == "deny"
        assert invoked[0]["payload"]["hook_id"] == "guard"
    finally:
        install_global_dispatcher(None)
