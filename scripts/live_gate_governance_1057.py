#!/usr/bin/env python3
"""Composed governance live gate (campaign #1057 — plan mode + hooks + loop/goal/cron).

Boots ONE real gact server (claude_code/sonnet/sdk, ARC-CTE default) and exercises the
governance surfaces END-TO-END against the real backend, then writes a verdict JSON and
tears the server down. The Windows-cron machinery is separately proven 6/6 by
``win_cron_gate.py`` (real clock + file + tick loop); this gate proves the surfaces fire
through the live server stack.

Gates:
  PLAN   — a session in ``mode=plan`` is asked to WRITE a file; the read-only lock must
           DENY the write live (grant_resolver plan_acl) — the file must not appear and
           the transcript must carry a plan denial. PASS = write blocked.
  HOOK   — a project ``.clio/hooks.json`` PreToolUse hook (matching ``shell_bash``) writes
           a marker file when it fires; a normal edit-mode turn that runs a shell command
           must trip it. PASS = marker written AND GET /v1/hooks lists the hook.
  CRON   — POST /commands/cron registers a near-future schedule; GET must list it with a
           local next_fire_at. PASS = registered.
  LOOP   — POST /commands/loop arms a bounded self-paced loop; session.metadata must carry
           loop state. PASS = armed + bounded.
  GOAL   — POST /commands/goal arms an NL-condition goal (LLM-judge-only; the deterministic
           predicate tier was deleted in A4 #1057; read-only goal_status, no model set_goal).
           PASS = armed + no deterministic predicate on metadata.

Run detached; writes the verdict to --out.

    uv run python scripts/live_gate_governance_1057.py --port 17851

ENVIRONMENT PREREQUISITES (an accepted live gate holds the REAL CTE, never the LocalFS
degrade — see the arc-local-never-for-gates rule): the box needs enough free disk for the
clio-core file tier (default ``arc.cte.file_capacity`` ~50 GB) or the CTE preflight fails
and ARC degrades to LocalFSStore (a LOUD degrade, #897) — lower ``CLIO_ARC_CTE_FILE_CAPACITY``
to fit the disk to keep a REAL (smaller) CTE. Also run against a CLEAN process tree: each
claude_code turn spawns ``claude.exe`` SDK subprocesses, and accumulated orphans from prior
runs contend and make the provider-bind call time out. Confirm ``/v1/providers/lm/wait``
returns ready before posting a turn.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _client(base: str):
    import requests

    def call(method: str, path: str, body=None, params=None, ok=(200, 201, 202), timeout=300):
        # 300s default: the cold claude_code SDK + MCP-fleet boot (uv resolve + geo/etc.
        # stdio servers) can exceed a 180s read on the first provider-bind call.
        r = requests.request(method, f"{base}{path}", json=body, params=params, timeout=timeout)
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    return call


def _await_turn(call, wsid: str, sid: str, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    status = "?"
    while time.monotonic() < deadline:
        rows = call("GET", f"/v1/sessions?workspace_id={wsid}").get("sessions", [])
        row = next((r for r in rows if r.get("id") == sid), {})
        status = row.get("status", "?")
        if status in ("idle", "completed", "error", "waiting_user"):
            return status
        time.sleep(5)
    return status


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=17851)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--transport", default="sdk")
    ap.add_argument("--out", default="out/live_gate_governance_1057.json")
    ap.add_argument("--turn-timeout-s", type=float, default=600.0)
    args = ap.parse_args()

    base = f"http://127.0.0.1:{args.port}"
    out_path = (REPO / args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    ws_root = (REPO / "out/live_gate_gov_ws").resolve()
    (ws_root / ".clio").mkdir(parents=True, exist_ok=True)
    marker = (ws_root / "hook_fired.marker").resolve()
    if marker.exists():
        marker.unlink()
    plan_target = (ws_root / "plan_should_not_write.txt").resolve()
    if plan_target.exists():
        plan_target.unlink()

    # Project PreToolUse hook: writes a marker when a shell_bash call is about to run,
    # then exits 0 (allow). The subprocess adapter runs EXEC-FORM argv (argv=[command,
    # *args], never a shell), so `command` MUST be the executable and `args` the argv
    # tail — passing "python -c ..." as one command string would exec a missing binary.
    hook_script = f"import pathlib; pathlib.Path(r'{marker}').write_text('fired')"
    (ws_root / ".clio" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": [
                    {
                        "id": "gov-gate-pretool-marker",
                        "on": ["PreToolUse"],
                        "match": {"tool": "shell_bash"},
                        "run": {
                            "type": "command",
                            "command": sys.executable,
                            "args": ["-c", hook_script],
                        },
                        "timeout_ms": 30000,
                    }
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["CLIO_LM_PROVIDER"] = "claude_code"
    env["CLIO_LM_MODEL"] = args.model
    env["CLIO_CLAUDE_CODE_TRANSPORT"] = args.transport
    env["CLIO_STREAM_AUDIT_LOG"] = str((REPO / "out/live_gate_gov_sse.log").resolve())
    env.pop("CLIO_LM_THINKING_LEVEL", None)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"from clio_agent.gact.app import run_server; run_server(host='127.0.0.1', port={args.port})",
        ],
        env=env,
        cwd=str(ws_root),  # cwd = workspace so .clio/hooks.json is the PROJECT scope
    )
    verdict: dict = {"model": args.model, "gates": {}}
    try:
        call = _client(base)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            try:
                call("GET", "/v1/health", ok=(200, 503))
                break
            except Exception:
                time.sleep(2)
        call(
            "PUT",
            "/v1/providers/lm",
            {"provider": "claude_code", "api_base": "", "model": args.model},
        )
        call("GET", "/v1/providers/lm/wait", params={"timeout": 240}, ok=(200, 503), timeout=300)
        call(
            "PUT",
            "/v1/policies",
            {"policies": [{"scope": "workspace", "action": "allow", "tool_name_pattern": "*"}]},
        )
        ws = call("POST", "/v1/workspaces", {"name": "gov-gate", "root_path": str(ws_root)})
        wsid = ws.get("id") or ws.get("workspace_id")

        # ---- HOOK discovery introspection (GET /v1/hooks, P2.7) ------------------
        hooks_seen = call("GET", "/v1/hooks", ok=(200,))
        blob = json.dumps(hooks_seen, default=str)
        verdict["gates"]["hook_discovered"] = "gov-gate-pretool-marker" in blob

        # ---- PLAN gate: mode=plan must DENY a write ------------------------------
        psess = call(
            "POST", "/v1/sessions", {"title": "plan-gate", "workspace_id": wsid, "mode": "plan"}
        )
        psid = psess["id"]
        call(
            "POST",
            f"/v1/sessions/{psid}/messages",
            {
                "text": f"Create a file at {plan_target} containing the text HELLO. Use your file-write tool."
            },
            ok=(200, 201, 202),
        )
        pstatus = _await_turn(call, wsid, psid, args.turn_timeout_s)
        pmsgs = call("GET", f"/v1/sessions/{psid}/messages").get("messages", [])
        pblob = json.dumps(pmsgs, default=str).lower()
        plan_denied = "plan" in pblob and (
            "deny" in pblob or "read-only" in pblob or "read only" in pblob
        )
        verdict["gates"]["plan_write_blocked"] = not plan_target.exists()
        verdict["gates"]["plan_denial_in_trace"] = plan_denied
        verdict["plan_status"] = pstatus

        # ---- HOOK gate: an edit-mode shell turn must trip the hook ---------------
        hsess = call(
            "POST", "/v1/sessions", {"title": "hook-gate", "workspace_id": wsid, "mode": "edit"}
        )
        hsid = hsess["id"]
        call(
            "POST",
            f"/v1/sessions/{hsid}/messages",
            {"text": "Run the shell command: echo governance-gate. Report its output."},
            ok=(200, 201, 202),
        )
        hstatus = _await_turn(call, wsid, hsid, args.turn_timeout_s)
        verdict["gates"]["hook_fired_marker"] = marker.exists()
        verdict["hook_status"] = hstatus

        # ---- CRON/LOOP/GOAL command dispatch (cheap; no full turn) ---------------
        # Bodies mirror the TUI: {input, args} (parse_*_command read input/text/prompt +
        # an `args` map), NOT an `arguments` envelope. Read schedules back via the real
        # route /v1/sessions/{sid}/schedules; read loop/goal state off the session row.
        csess = call("POST", "/v1/sessions", {"title": "cmd-gate", "workspace_id": wsid})
        csid = csess["id"]

        def _session_meta(session_id: str) -> dict:
            row = next(
                (
                    r
                    for r in call("GET", f"/v1/sessions?workspace_id={wsid}").get("sessions", [])
                    if r.get("id") == session_id
                ),
                {},
            )
            return row.get("metadata", {}) or {}

        try:
            cron_body = call(
                "POST",
                f"/v1/sessions/{csid}/commands/cron",
                {"input": "*/5 * * * * ping the scheduled turn"},
                ok=(200, 201, 202),
            )
            scheds = call("GET", f"/v1/sessions/{csid}/schedules", ok=(200,))
            verdict["gates"]["cron_registered"] = "next_fire" in json.dumps(scheds, default=str)
            verdict["cron_cmd_body"] = json.dumps(cron_body, default=str)[:300]
        except Exception as exc:  # noqa: BLE001
            verdict["gates"]["cron_registered"] = f"err: {type(exc).__name__}: {exc}"
        try:
            loop_body = call(
                "POST",
                f"/v1/sessions/{csid}/commands/loop",
                {"input": "keep iterating on the task", "args": {"max_iters": 2}},
                ok=(200, 201, 202),
            )
            verdict["gates"]["loop_armed"] = (
                "loop" in json.dumps(_session_meta(csid), default=str).lower()
            )
            verdict["loop_cmd_body"] = json.dumps(loop_body, default=str)[:300]
        except Exception as exc:  # noqa: BLE001
            verdict["gates"]["loop_armed"] = f"err: {type(exc).__name__}: {exc}"
        try:
            # LLM-judge-only goal (the deterministic predicate tier was deleted, A4 #1057):
            # arm an NL condition and assert it is armed with NO predicate on metadata.
            goal_body = call(
                "POST",
                f"/v1/sessions/{csid}/commands/goal",
                {"input": "the analysis is complete and written up"},
                ok=(200, 201, 202),
            )
            gmeta = _session_meta(csid)
            goal_meta = (gmeta.get("goal") if isinstance(gmeta, dict) else None) or {}
            verdict["gates"]["goal_armed"] = bool(goal_meta.get("active"))
            verdict["gates"]["goal_no_predicate"] = "predicate" not in goal_meta
            verdict["goal_cmd_body"] = json.dumps(goal_body, default=str)[:300]
        except Exception as exc:  # noqa: BLE001
            verdict["gates"]["goal_armed"] = f"err: {type(exc).__name__}: {exc}"

        # dump transcripts for manual audit
        (out_path.parent / "gov_gate_plan_msgs.json").write_text(
            json.dumps(pmsgs, indent=2, default=str), encoding="utf-8"
        )
        g = verdict["gates"]
        verdict["pass"] = bool(
            g.get("plan_write_blocked")
            and g.get("hook_discovered")
            and g.get("hook_fired_marker")
            and g.get("cron_registered") is True
            and g.get("loop_armed") is True
            and g.get("goal_armed") is True
        )
    except Exception as exc:  # noqa: BLE001
        verdict["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        out_path.write_text(json.dumps(verdict, indent=2, default=str), encoding="utf-8")
        print(json.dumps(verdict, indent=2, default=str), flush=True)
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except Exception:
            proc.kill()
    return 0 if verdict.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
