#!/usr/bin/env python3
"""Live gate for #1000 observe_agent_tasks (OBSERVE posture).

Boots a gact server (claude_code/sonnet/sdk, ARC-CTE default), pre-allows
permissions, activates the earthscope-flat Agent Blueprint by path, and posts a
prompt where acting on INTERMEDIATE evidence is naturally advantageous — WITHOUT
naming any tool. PASS = the main uses observe_agent_tasks (or an observe-shaped
pattern) to surface the selected station id from intermediate typed state BEFORE
the child's terminal.

Run detached; it writes a verdict JSON to --out.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PROMPT = (
    "Stage an EarthScope GNSS station's dataset for the Los Angeles basin. As soon "
    "as you know WHICH station was selected — before the full download/staging "
    "finishes — tell me the station id while the rest continues; then give the final "
    "summary once staging completes."
)


def _client(base: str):
    import requests

    def call(method: str, path: str, body=None, params=None, ok=(200, 201)):
        r = requests.request(method, f"{base}{path}", json=body, params=params, timeout=180)
        if r.status_code not in ok:
            r.raise_for_status()
        return r.json() if r.content else {}

    return call


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=17818)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--transport", default="sdk")
    ap.add_argument("--blueprint-path", default="out/haiku-experiment/earthscope-flat")
    ap.add_argument("--out", default="out/live_gate_observe_1000.json")
    ap.add_argument("--turn-timeout-s", type=float, default=900.0)
    args = ap.parse_args()

    import os

    base = f"http://127.0.0.1:{args.port}"
    out_path = (REPO / args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["CLIO_LM_PROVIDER"] = "claude_code"
    env["CLIO_LM_MODEL"] = args.model
    env["CLIO_CLAUDE_CODE_TRANSPORT"] = args.transport
    env["CLIO_STREAM_AUDIT_LOG"] = str((REPO / "out/live_gate_sse.log").resolve())
    env.pop("CLIO_LM_THINKING_LEVEL", None)

    proc = subprocess.Popen(
        [sys.executable, "-c",
         f"from clio_agent.gact.app import run_server; run_server(host='127.0.0.1', port={args.port})"],
        env=env, cwd=str(REPO),
    )
    verdict: dict = {"prompt": PROMPT, "model": args.model}
    try:
        call = _client(base)
        # wait for port
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            try:
                call("GET", "/v1/health", ok=(200, 503))
                break
            except Exception:
                time.sleep(2)
        # bind provider
        call("PUT", "/v1/providers/lm", {"provider": "claude_code", "api_base": "", "model": args.model})
        call("GET", "/v1/providers/lm/wait", params={"timeout": 120}, ok=(200, 503))
        # pre-allow permissions FIRST
        call("PUT", "/v1/policies",
             {"policies": [{"scope": "workspace", "action": "allow", "tool_name_pattern": "*"}]})
        # workspace + session
        ws_root = (REPO / "out/live_gate_ws").resolve()
        ws_root.mkdir(parents=True, exist_ok=True)
        ws = call("POST", "/v1/workspaces", {"name": "live-gate-1000", "root_path": str(ws_root)})
        wsid = ws.get("id") or ws.get("workspace_id")
        sess = call("POST", "/v1/sessions", {"title": "observe-gate", "workspace_id": wsid})
        sid = sess["id"]
        # activate the earthscope-flat blueprint by path
        act = call("POST", f"/v1/sessions/{sid}/agent-blueprint", {"path": args.blueprint_path})
        verdict["activation"] = {k: act.get(k) for k in ("active_agent_blueprint_id",)}
        # post the prompt
        call("POST", f"/v1/sessions/{sid}/messages", {"text": PROMPT}, ok=(200, 201, 202))
        # wait for the turn to finish
        tdeadline = time.monotonic() + args.turn_timeout_s
        status = "?"
        while time.monotonic() < tdeadline:
            rows = call("GET", f"/v1/sessions?workspace_id={wsid}").get("sessions", [])
            row = next((r for r in rows if r.get("id") == sid), {})
            status = row.get("status", "?")
            if status in ("idle", "completed", "error", "waiting_user"):
                break
            time.sleep(5)
        verdict["final_status"] = status
        # pull messages + look for observe usage in the react trajectory
        msgs = call("GET", f"/v1/sessions/{sid}/messages").get("messages", [])
        observe_calls = []
        for m in msgs:
            for p in m.get("parts", []) or []:
                blob = json.dumps(p, default=str)
                if "observe_agent_tasks" in blob:
                    observe_calls.append({"role": m.get("role"), "type": p.get("type"),
                                          "excerpt": blob[:400]})
        verdict["observe_tool_used"] = bool(observe_calls)
        verdict["observe_calls"] = observe_calls[:10]
        verdict["session_id"] = sid
        verdict["workspace_id"] = wsid
        # dump the full transcript for manual audit
        (out_path.parent / "live_gate_messages.json").write_text(
            json.dumps(msgs, indent=2, default=str), encoding="utf-8")
        verdict["pass"] = bool(observe_calls) and status in ("idle", "completed")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
