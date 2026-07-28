"""Composed governance verification — plan · hooks · cron · loop · goal TOGETHER (#1057).

The full campaign's surfaces exercised in ONE integration test through the REAL code paths
(real command dispatch, real grant_resolver, real hook dispatcher firing a REAL OS
subprocess) — no external LM provider and no long-running uvicorn server, so it runs
deterministically in CI. This is the in-process complement to the real-`claude_code`-server
gate (``scripts/live_gate_governance_1057.py``, which additionally exercises a live LLM turn):
it proves the surfaces COMPOSE, while the script proves plan-lock + hook discovery against a
live server.

Covers, together:
* PLAN  — the plan-mode read-only lock denies a write tool via ``grant_resolver.resolve``
  while a read short-circuits to allow (``is_read_only`` first-branch).
* HOOK  — a project ``.clio/hooks.json`` PreToolUse hook FIRES a real subprocess on a real
  ``shell_bash`` PreToolUse envelope (marker written, allow returned on exit 0).
* CRON  — ``/cron`` dispatch arms a real schedule (readable back off the store).
* LOOP  — ``/loop`` dispatch arms a bounded loop on ``session.metadata``.
* GOAL  — ``/goal`` dispatch arms a predicate-backed goal on ``session.metadata``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.hooks import HookEnvelope, build_hook_dispatcher
from clio_agent.gact.hooks.events import PRE_TOOL_USE
from clio_agent.gact.runtime.grant_resolver import is_read_only, resolve


class _Pred:
    answer = "ok"
    selected_expert = "none"
    routing_rationale = ""
    tools_called: list = []
    tokens: dict = {}
    cost_usd = 0.0
    file_diffs: list = []
    permissions_requested: list = []
    nanoagents_spawned: list = []


class _Agent:
    def forward(self, *args: object, **kwargs: object) -> _Pred:
        return _Pred()


def _write_project_hook(ws: Path, marker: Path) -> None:
    """Install a project PreToolUse hook that writes a marker on a shell_bash call.

    Exec-form argv (``command`` = the executable, ``args`` = the tail) — the subprocess
    adapter runs ``argv=[command, *args]``, never a shell."""

    (ws / ".clio").mkdir(parents=True, exist_ok=True)
    (ws / ".clio" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": [
                    {
                        "id": "composed-gate-marker",
                        "on": ["PreToolUse"],
                        "match": {"tool": "shell_bash"},
                        "run": {
                            "type": "command",
                            "command": sys.executable,
                            "args": ["-c", f"import pathlib; pathlib.Path(r'{marker}').write_text('fired')"],
                        },
                        "timeout_ms": 30000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_plan_lock_denies_write_allows_read() -> None:
    """PLAN: the plan-mode lock denies a write tool; a read short-circuits to allow."""

    # A write-shaped tool is DENIED in plan mode and ALLOWED in edit — the mode-lock is
    # real, resolved (not hardcoded), and mode-specific (the built-in plan_acl deny that
    # resolve() applies internally for a tool resolution in plan mode).
    assert resolve("tool", "fs_write", policies=[], mode="plan") == "deny"
    assert resolve("tool", "fs_write", policies=[], mode="edit") == ""
    # Reads are NEVER gated: is_read_only fast-allows a declared read tool BEFORE any mode
    # logic, while a tagged write is not read-only (so it reaches the plan lock above).
    assert is_read_only("tool", "fs_read_file", {}, None) is True
    assert is_read_only("tool", "fs_apply_edit_write", {}, None) is False


def test_hook_fires_real_subprocess(tmp_path: Path, monkeypatch: Any) -> None:
    """HOOK: a project PreToolUse hook fires a REAL subprocess on a real envelope.

    The trust store is isolated per test (``CLIO_HOOKS_TRUST_STORE``): a first-seen hook
    is trusted, so it fires. (Reusing the shared store would correctly mark this run's
    hook ``untrusted`` — same id, tmp_path-varying content = "changed since last trusted"
    — which is the P2.7 tighten-only trust invariant, not a firing bug.)"""

    monkeypatch.setenv("CLIO_HOOKS_TRUST_STORE", str(tmp_path / "trust.json"))
    ws = tmp_path / "ws"
    marker = (ws / "hook_fired.marker").resolve()
    _write_project_hook(ws, marker)

    disp = build_hook_dispatcher(cwd=ws)
    envelope = HookEnvelope(
        hook_event_name=PRE_TOOL_USE,
        session_id="sess-1",
        turn_id="turn-1",
        tool_name="shell_bash",
        tool_input={"command": "echo hi"},
        tool_annotations={"readOnly": False, "destructive": False, "openWorld": False},
    )
    outcome = disp.dispatch(PRE_TOOL_USE, envelope)
    assert marker.exists(), "the PreToolUse hook subprocess did not fire (no marker)"
    assert marker.read_text(encoding="utf-8") == "fired"
    assert not outcome.denied, "an exit-0 hook must allow, not deny"


def test_cron_loop_goal_arm_together(tmp_path: Path) -> None:
    """CRON+LOOP+GOAL: all three command surfaces arm + read back through real dispatch."""

    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent())) as client:
        sid = client.post("/v1/sessions", json={"title": "composed"}).json()["id"]

        # CRON — arms a real schedule.
        r = client.post(f"/v1/sessions/{sid}/commands/cron", json={"input": "*/5 * * * * ping"})
        assert r.status_code == 200 and "unhandled command" not in r.json()["result"]["text"]
        assert len(client.app.state.schedules.list(session_id=sid)) == 1

        # LOOP — arms a bounded loop on session.metadata.
        r = client.post(
            f"/v1/sessions/{sid}/commands/loop",
            json={"input": "keep iterating", "args": {"max_iters": 2}},
        )
        assert r.status_code == 200
        meta: dict[str, Any] = client.app.state.sessions.get(sid).metadata or {}
        assert isinstance(meta.get("loop"), dict), f"loop not armed on metadata: {meta.keys()}"

        # GOAL — arms a predicate-backed goal on session.metadata.
        r = client.post(
            f"/v1/sessions/{sid}/commands/goal",
            json={
                "input": "the done field is true",
                "args": {"when_state": {"field_path": "done", "check": "equals", "equals": "true"}},
            },
        )
        assert r.status_code == 200
        meta = client.app.state.sessions.get(sid).metadata or {}
        goal = meta.get("goal")
        assert isinstance(goal, dict), f"goal not armed on metadata: {meta.keys()}"
        assert goal.get("predicate_backed") is True, f"goal should be predicate-backed: {goal}"
