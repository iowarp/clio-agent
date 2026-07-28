#!/usr/bin/env python3
"""Composed governance live gate (campaign #1057 — P0-P4 on a REAL, self-contained CTE).

Boots ONE real gact server (claude_code/sonnet/sdk) backed by a REAL clio-core CTE that
this script stands up itself — an in-repo recipe on a PRIVATE port (9423), fully
independent of any host daemon (the WSL 9413 squatter, the host cte.yaml). It then
exercises every governance surface END-TO-END against the live backend, writes a verdict
JSON, and tears BOTH the gact server and the CTE daemon it spawned down.

Real CTE recipe (mirrors the shipped product template — see
``clio_agent.arc.clio_core_config._DEFAULT_CTE_CONFIG_TEMPLATE`` and the proven private
recipe in ``tests/_cte_isolation.py``): the pool names/ids ``cte_main``/512.0 and
``ram::chi_default_bdev``/301.0 are LOAD-BEARING and copied verbatim; only the port
(9423), the repo-local storage/metadata paths, and the bounded caps (file tier 8GB, ram
1GB) change. The gact server is pointed at it via ``CLIO_ARC_STORE=cte`` +
``CLIO_ARC_STORE_CONFIG`` / ``CLIO_SERVER_CONF`` (the config the client store init AND the
spawned daemon both compose) + ``CLIO_CORE_PORT=9423`` (liveness probes match the private
bind) + ``CLIO_RUNTIME_STATE_DIR`` (private spawn-lock / pidfile / registry / daemon log).
A LocalFS degrade is an automatic FAIL (the arc-local-never-for-gates rule).

Gates (P0-P4 composed, incl. the two blocker repros):
  CTE      — /v1/health shows the ARC clio-core backend LIVE (never the LocalFS degrade)
             AND the clio_core row live; the process listening on 9423 is python/clio_run
             (NEVER wslrelay.exe); ZERO stream_fallback/degrade reasons for the run.
  P0/P1 PLAN     — a ``mode=plan`` session asked to WRITE a file: the write is DENIED live
                   (the file never appears) + a plan denial rides the transcript.
  P1 PLAN-EXIT   — (blocker B1 repro) drive a plan turn to a ``plan_exit`` approval
                   question, approve it via the answers API, assert the resume turn
                   proceeds WITHOUT a phantom second plan-exit question AND the session
                   lands in ``edit`` mode.
  P2 HOOK        — a project ``.clio/hooks.json`` PreToolUse marker hook fires on a real
                   shell turn + ``GET /v1/hooks`` discovery. PLUS (blocker B2 repro): POST
                   a message with ``metadata:{hook_defer_resume:true}`` -> 400
                   ``reserved_metadata_key`` (this lands from Lane B via a develop merge;
                   the gate records FAIL until then).
  P4 CRON        — a near-future ONE-SHOT actually FIRES a scheduled turn through the live
                   server (a new turn carrying the schedule id appears afterward).
  P4 LOOP        — /loop arms bounded (max_iters=2 + a short wall-clock); the loop runs an
                   iteration and ends with a typed bound stop_reason; the sticky-refusal
                   sub-assert (stop_reason sticky + a live loop_bound_tripped_rearm_denied)
                   is recorded (optional — model-tool dependent).
  P4 GOAL        — /goal arms an NL condition the next turn plainly satisfies; the bounded
                   LLM judge settles it (goal cleared, ``goal_met`` outcome on metadata).

Verdict JSON ``pass`` requires ALL required gates. Transcripts + the SSE audit log are
retained for audit.

    uv run python scripts/live_gate_governance_1057.py --port 17851 --core-port 9423

ENVIRONMENT: an accepted live gate holds the REAL CTE. Run against a CLEAN process tree —
each claude_code turn spawns ``claude.exe`` SDK subprocesses, and accumulated orphans
contend and time out the provider bind. The gate confirms ``/v1/providers/lm/wait`` is
ready before posting any turn.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parent.parent

# The CTE pool names/ids are LOAD-BEARING (copied verbatim from the shipped
# _DEFAULT_CTE_CONFIG_TEMPLATE). Only the port, the repo-local paths, and the bounded caps
# change. File tier caps at 8GB (clio-core PREALLOCATES the file tier at its full cap, so
# never a huge number); the ram bdev caps working memory at 1GB (the #906 memory budget).
_CTE_CONFIG_TEMPLATE = """\
networking:
  port: {port}
runtime:
  num_threads: 4
  conf_dir: "{conf_dir}"
compose:
  - mod_name: clio_bdev
    pool_name: "ram::chi_default_bdev"
    pool_query: local
    pool_id: "301.0"
    bdev_type: ram
    capacity: "1GB"
  - mod_name: clio_cte_core
    pool_name: cte_main
    pool_query: local
    pool_id: "512.0"
    restart: true
    storage:
      - path: "{file_tier}"
        bdev_type: "file"
        capacity_limit: "8GB"
        score: 1.0
    dpe:
      dpe_type: "max_bw"
    performance:
      metadata_log_path: "{metadata_log}"
      transaction_log_capacity: "32MB"
"""


def _client(base: str) -> Callable[..., Any]:
    """Build a thin JSON HTTP caller bound to ``base`` (raises on unexpected status)."""

    import requests

    def call(
        method: str,
        path: str,
        body: Any = None,
        params: Any = None,
        ok: tuple[int, ...] = (200, 201, 202),
        timeout: float = 300,
    ) -> Any:
        # 300s default: the cold claude_code SDK + MCP-fleet boot (uv resolve + geo/etc.
        # stdio servers) can exceed a 180s read on the first provider-bind call.
        r = requests.request(method, f"{base}{path}", json=body, params=params, timeout=timeout)
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    return call


def _raw(base: str) -> Callable[..., Any]:
    """Build a caller that returns the raw ``requests.Response`` (for status inspection)."""

    import requests

    def call(method: str, path: str, body: Any = None, timeout: float = 60) -> Any:
        return requests.request(method, f"{base}{path}", json=body, timeout=timeout)

    return call


def _write_cte_yaml(core_dir: Path, port: int) -> Path:
    """Write the private CTE config under ``core_dir`` and return the yaml path.

    The pool names/ids are load-bearing (copied verbatim); only the port, repo-local
    storage/metadata paths, and the bounded caps are substituted. Storage + metadata live
    beside the yaml so tearing down ``core_dir`` removes every artifact.
    """

    conf_dir = core_dir / "conf"
    store_dir = core_dir / "store"
    conf_dir.mkdir(parents=True, exist_ok=True)
    store_dir.mkdir(parents=True, exist_ok=True)
    cfg = core_dir / "cte.yaml"
    cfg.write_text(
        _CTE_CONFIG_TEMPLATE.format(
            port=port,
            conf_dir=conf_dir.as_posix(),
            file_tier=(store_dir / "storage.bin").as_posix(),
            metadata_log=(store_dir / "metadata.log").as_posix(),
        ),
        encoding="utf-8",
    )
    return cfg


def _listener_pids(port: int) -> list[int]:
    """Return the PIDs of processes LISTENING on ``127.0.0.1:port`` (Windows netstat)."""

    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        # e.g.  TCP    127.0.0.1:9423   0.0.0.0:0   LISTENING   12345
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            local = parts[1]
            if local.endswith(f":{port}"):
                try:
                    pids.append(int(parts[4]))
                except ValueError:
                    continue
    return sorted(set(pids))


def _proc_name(pid: int) -> str:
    """Return the image name for ``pid`` (Windows tasklist), or '' if not found."""

    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    if not out or out.upper().startswith("INFO:"):
        return ""
    # CSV: "image.exe","pid","session","sessname","memusage"
    first = out.splitlines()[0]
    m = re.match(r'"([^"]+)"', first)
    return m.group(1) if m else ""


def _terminate_pid(pid: int) -> None:
    """Best-effort terminate->kill of ``pid`` (psutil, PID-reuse tolerant for a teardown)."""

    try:
        import psutil

        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=8.0)
        except psutil.TimeoutExpired:
            proc.kill()
    except Exception:  # noqa: BLE001 - already gone / no permission: best-effort
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )


def _await_turn(call: Callable[..., Any], wsid: str, sid: str, deadline: float) -> str:
    """Poll a session's status until terminal or ``deadline`` (monotonic); return status."""

    status = "?"
    while time.monotonic() < deadline:
        rows = call("GET", f"/v1/sessions?workspace_id={wsid}").get("sessions", [])
        row = next((r for r in rows if r.get("id") == sid), {})
        status = row.get("status", "?")
        if status in ("idle", "completed", "error", "waiting_user"):
            return status
        time.sleep(5)
    return status


def _session_row(call: Callable[..., Any], wsid: str, sid: str) -> dict[str, Any]:
    """Return the session's list row (``{}`` when absent)."""

    rows = call("GET", f"/v1/sessions?workspace_id={wsid}").get("sessions", [])
    return next((r for r in rows if r.get("id") == sid), {}) or {}


def _session_meta(call: Callable[..., Any], wsid: str, sid: str) -> dict[str, Any]:
    """Return ``session.metadata`` off the session list row (``{}`` when absent)."""

    return _session_row(call, wsid, sid).get("metadata", {}) or {}


def _messages(call: Callable[..., Any], sid: str) -> list[dict[str, Any]]:
    """Return a session's message ledger (newest-first per the route)."""

    return call("GET", f"/v1/sessions/{sid}/messages").get("messages", [])


def _scan_stream_fallbacks(sse_log: Path, msg_blobs: list[str]) -> list[str]:
    """Collect distinct stream_fallback/degrade reason tokens from the SSE log + messages.

    The stream_fallback catalog (``gact/streaming.py``) records typed reasons per session,
    emitted over the SSE channel (captured in the audit log) and folded into message
    metadata. A clean run yields ZERO. LocalFS init degrade is caught separately by the
    /v1/health arc row.
    """

    found: set[str] = set()
    text_sources: list[str] = list(msg_blobs)
    try:
        text_sources.append(sse_log.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        pass
    reason_re = re.compile(r'"stream_fallback"\s*:\s*\{[^}]*"reason"\s*:\s*"([^"]+)"')
    for text in text_sources:
        for m in reason_re.finditer(text):
            found.add(m.group(1))
        # Also catch a bare marker even if the shape shifts (never silently miss a degrade).
        if '"stream_fallback"' in text and not reason_re.search(text):
            found.add("stream_fallback_present_unparsed")
    return sorted(found)


def main() -> int:  # noqa: C901, PLR0912, PLR0915 - a live gate is an inherently linear script
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=17851, help="gact HTTP server port")
    ap.add_argument("--core-port", type=int, default=9423, help="private clio-core CTE port")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--transport", default="sdk")
    ap.add_argument("--out", default="out/live_gate_governance_1057.json")
    ap.add_argument("--turn-timeout-s", type=float, default=600.0)
    ap.add_argument("--budget-s", type=float, default=1800.0, help="overall wall-clock guard")
    args = ap.parse_args()

    overall_deadline = time.monotonic() + args.budget_s
    base = f"http://127.0.0.1:{args.port}"
    out_path = (REPO / args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sse_log = (REPO / "out/live_gate_gov_sse.log").resolve()

    ws_root = (REPO / "out/live_gate_gov_ws").resolve()
    core_dir = ws_root / ".clio" / "core"
    state_dir = core_dir / "state"
    (ws_root / ".clio").mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    # Fresh CTE recipe for THIS run (private port, repo-local paths, bounded caps).
    cte_yaml = _write_cte_yaml(core_dir, args.core_port)

    marker = (ws_root / "hook_fired.marker").resolve()
    if marker.exists():
        marker.unlink()
    plan_target = (ws_root / "plan_should_not_write.txt").resolve()
    if plan_target.exists():
        plan_target.unlink()

    # Project PreToolUse hook: writes a marker when a shell_bash call is about to run, then
    # exits 0 (allow). The subprocess adapter runs EXEC-FORM argv, so `command` MUST be the
    # executable and `args` the argv tail.
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
    env["CLIO_STREAM_AUDIT_LOG"] = str(sse_log)
    # REAL, self-contained CTE on the private port — independent of any host daemon.
    env["CLIO_ARC_STORE"] = "cte"
    env["CLIO_ARC_STORE_CONFIG"] = str(cte_yaml)
    env["CLIO_SERVER_CONF"] = str(cte_yaml)
    env["CLIO_CORE_PORT"] = str(args.core_port)
    env["CLIO_RUNTIME_STATE_DIR"] = str(state_dir)
    # Belt-and-suspenders: these only govern GENERATED configs (we ship the file), but keep
    # any generated-path code consistent with the recipe's bounded caps.
    env["CLIO_ARC_CTE_RAM_CAPACITY"] = "1GB"
    env["CLIO_ARC_CTE_FILE_CAPACITY"] = "8GB"
    env.pop("CLIO_LM_THINKING_LEVEL", None)

    if sse_log.exists():
        sse_log.unlink()

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"from clio_agent.gact.app import run_server; run_server(host='127.0.0.1', port={args.port})",
        ],
        env=env,
        cwd=str(ws_root),  # cwd = workspace so .clio/hooks.json is the PROJECT scope
    )

    verdict: dict[str, Any] = {
        "model": args.model,
        "core_port": args.core_port,
        "cte_yaml": str(cte_yaml),
        "gates": {},
    }
    msg_blobs: list[str] = []

    def _record(name: str, value: Any) -> None:
        verdict["gates"][name] = value

    try:
        call = _client(base)
        raw = _raw(base)

        # ---- boot: wait for /v1/health to answer ----------------------------------
        deadline = min(time.monotonic() + 240, overall_deadline)
        while time.monotonic() < deadline:
            try:
                call("GET", "/v1/health", ok=(200, 503))
                break
            except Exception:  # noqa: BLE001 - server still binding
                time.sleep(2)

        # ---- CTE health: REAL clio-core, never the LocalFS degrade -----------------
        health = call("GET", "/v1/health", ok=(200, 503))
        integrations = {row.get("name"): row for row in (health.get("integrations") or [])}
        arc_row = integrations.get("arc", {})
        core_row = integrations.get("clio_core", {})
        arc_summary = (arc_row.get("summary") or arc_row.get("detail") or "").lower()
        verdict["arc_row"] = arc_row
        verdict["clio_core_row"] = core_row
        _record(
            "cte_arc_live",
            arc_row.get("status") == "ready"
            and "clio-core backend is live" in arc_summary
            and "degraded to local" not in arc_summary,
        )
        _record("cte_clio_core_live", core_row.get("status") == "ready")
        _record(
            "cte_endpoint_on_core_port",
            str(arc_row.get("endpoint") or "").endswith(f":{args.core_port}"),
        )

        # ---- CTE listener process: python/clio_run, NEVER wslrelay.exe -------------
        listener_pids = _listener_pids(args.core_port)
        listener_names = {pid: _proc_name(pid) for pid in listener_pids}
        verdict["cte_listener"] = {str(p): n for p, n in listener_names.items()}
        names_lower = {n.lower() for n in listener_names.values() if n}
        _record(
            "cte_listener_is_clio_process",
            bool(names_lower)
            and "wslrelay.exe" not in names_lower
            and all(
                n.startswith("python") or n.startswith("clio_run") for n in names_lower
            ),
        )

        # ---- provider bind + pre-allow policies BEFORE any turn --------------------
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

        # ---- P2 HOOK discovery (GET /v1/hooks) ------------------------------------
        hooks_seen = call("GET", "/v1/hooks", ok=(200,))
        _record("hook_discovered", "gov-gate-pretool-marker" in json.dumps(hooks_seen, default=str))

        # ---- P0/P1 PLAN: mode=plan must DENY a write ------------------------------
        psess = call(
            "POST", "/v1/sessions", {"title": "plan-gate", "workspace_id": wsid, "mode": "plan"}
        )
        psid = psess["id"]
        call(
            "POST",
            f"/v1/sessions/{psid}/messages",
            {
                "text": (
                    f"Create a file at {plan_target} containing the text HELLO. "
                    "Use your file-write tool."
                )
            },
        )
        pstatus = _await_turn(
            call, wsid, psid, min(time.monotonic() + args.turn_timeout_s, overall_deadline)
        )
        pmsgs = _messages(call, psid)
        msg_blobs.append(json.dumps(pmsgs, default=str))
        pblob = json.dumps(pmsgs, default=str).lower()
        _record("plan_write_blocked", not plan_target.exists())
        _record(
            "plan_denial_in_trace",
            "plan" in pblob and ("deny" in pblob or "read-only" in pblob or "read only" in pblob),
        )
        verdict["plan_status"] = pstatus

        # ---- P1 PLAN-EXIT (blocker B1 repro): approve -> no phantom re-approval ----
        pxsess = call(
            "POST", "/v1/sessions", {"title": "plan-exit-gate", "workspace_id": wsid, "mode": "plan"}
        )
        pxid = pxsess["id"]
        call(
            "POST",
            f"/v1/sessions/{pxid}/messages",
            {
                "text": (
                    "Write a brief plan to a new markdown file in the plans directory (the only "
                    "writable path in plan mode). The plan's single step should be: reply with the "
                    "word EXECUTED (no file edits are needed to carry it out). After writing the "
                    "plan file, call your plan_exit tool with a one-sentence summary and "
                    "recommendedMode auto to hand the plan back for approval."
                )
            },
        )

        def _plan_exit_questions() -> list[dict[str, Any]]:
            rows = call("GET", f"/v1/sessions/{pxid}/questions").get("questions", [])
            return [q for q in rows if q.get("source") == "plan_exit"]

        # Poll for the plan-exit approval question to surface (session -> waiting_user).
        px_deadline = min(time.monotonic() + args.turn_timeout_s, overall_deadline)
        pending_q: dict[str, Any] = {}
        while time.monotonic() < px_deadline:
            pending = [q for q in _plan_exit_questions() if q.get("status") == "pending"]
            if pending:
                pending_q = pending[0]
                break
            row = _session_row(call, wsid, pxid)
            if row.get("status") in ("error",):
                break
            time.sleep(5)

        if pending_q:
            # Approve (auto = auto-edits, so the resume turn runs without an approval prompt)
            # and drive the resume so the finalize seam re-evaluates (the B1 phantom repro).
            call(
                "POST",
                f"/v1/sessions/{pxid}/questions/{pending_q['id']}/answer",
                {"answer": "approved", "selected_options": ["auto"]},
                ok=(200,),
            )
            _await_turn(
                call, wsid, pxid, min(time.monotonic() + args.turn_timeout_s, overall_deadline)
            )
            px_all = _plan_exit_questions()
            px_pending_after = [q for q in px_all if q.get("status") == "pending"]
            px_mode = call("GET", f"/v1/sessions/{pxid}").get("mode")
            msg_blobs.append(json.dumps(_messages(call, pxid), default=str))
            # B1: exactly ONE plan-exit question ever (no phantom re-approval) + edit mode.
            _record(
                "plan_exit_no_phantom",
                len(px_all) == 1 and not px_pending_after,
            )
            _record("plan_exit_mode_edit", px_mode == "edit")
            verdict["plan_exit_question_count"] = len(px_all)
        else:
            _record("plan_exit_no_phantom", "err: plan_exit question never surfaced")
            _record("plan_exit_mode_edit", False)

        # ---- P2 HOOK: an edit-mode shell turn must trip the hook -------------------
        hsess = call(
            "POST", "/v1/sessions", {"title": "hook-gate", "workspace_id": wsid, "mode": "edit"}
        )
        hsid = hsess["id"]
        call(
            "POST",
            f"/v1/sessions/{hsid}/messages",
            {"text": "Run the shell command: echo governance-gate. Report its output."},
        )
        hstatus = _await_turn(
            call, wsid, hsid, min(time.monotonic() + args.turn_timeout_s, overall_deadline)
        )
        msg_blobs.append(json.dumps(_messages(call, hsid), default=str))
        _record("hook_fired_marker", marker.exists())
        verdict["hook_status"] = hstatus

        # ---- P2 RESERVED-METADATA (blocker B2 repro): 400 reserved_metadata_key ----
        # This guard lands from Lane B via a develop merge; until then the POST is accepted
        # and this gate records FAIL (with the observed status), never a false pass.
        rmsess = call("POST", "/v1/sessions", {"title": "reserved-meta-gate", "workspace_id": wsid})
        rmid = rmsess["id"]
        r = raw(
            "POST",
            f"/v1/sessions/{rmid}/messages",
            {"text": "noop", "metadata": {"hook_defer_resume": True}},
        )
        reserved_ok = r.status_code == 400 and "reserved_metadata_key" in r.text
        _record(
            "reserved_metadata_rejected",
            True if reserved_ok else f"err: status={r.status_code} body={r.text[:200]}",
        )
        if not reserved_ok:
            # The POST was (wrongly) accepted -> a turn started; cancel it to save tokens.
            try:
                call("POST", f"/v1/sessions/{rmid}/cancel", ok=(200, 202, 204, 404, 409))
            except Exception:  # noqa: BLE001 - teardown convenience
                pass

        # ---- P4 CRON: a near-future one-shot must ACTUALLY FIRE --------------------
        csess = call("POST", "/v1/sessions", {"title": "cron-gate", "workspace_id": wsid})
        csid = csess["id"]
        try:
            call(
                "POST",
                f"/v1/sessions/{csid}/commands/cron",
                {
                    "input": "",
                    "args": {
                        "delay_s": 90,
                        "recurring": False,
                        "prompt": "Reply with the single word TICK.",
                    },
                },
            )
            scheds = call("GET", f"/v1/sessions/{csid}/schedules", ok=(200,)).get("schedules", [])
            sched_id = scheds[0]["id"] if scheds else ""
            verdict["cron_schedule_id"] = sched_id
            _record("cron_registered", bool(sched_id) and bool(scheds and scheds[0].get("next_fire_at")))
            # The tick loop is minute-aligned; a delay_s=90 one-shot fires within ~2 ticks,
            # then stages a real turn (a new user message carrying schedule_id + an assistant
            # reply). Poll for the fired turn.
            cron_deadline = min(time.monotonic() + args.turn_timeout_s + 180, overall_deadline)
            cron_fired = False
            while time.monotonic() < cron_deadline:
                cmsgs = _messages(call, csid)
                blob = json.dumps(cmsgs, default=str)
                if sched_id and sched_id in blob and any(
                    m.get("role") == "assistant"
                    and m.get("metadata", {}).get("synthetic") != "command_result"
                    for m in cmsgs
                ):
                    cron_fired = True
                    break
                time.sleep(10)
            msg_blobs.append(json.dumps(_messages(call, csid), default=str))
            _record("cron_fired", cron_fired)
        except Exception as exc:  # noqa: BLE001 - typed into the verdict, never a crash
            _record("cron_registered", f"err: {type(exc).__name__}: {exc}")
            _record("cron_fired", f"err: {type(exc).__name__}: {exc}")

        # ---- P4 LOOP: bounded loop runs, ends on a typed bound, sticky refusal -----
        lsess = call("POST", "/v1/sessions", {"title": "loop-gate", "workspace_id": wsid})
        lsid = lsess["id"]
        try:
            from clio_agent.gact.autonomous_loop import (  # noqa: PLC0415
                LOOP_STICKY_STOP_REASONS,
                LOOP_STOP_REASONS,
            )

            # max_iters=2 per the plan + a short wall-clock so a HARD (sticky) bound reliably
            # trips after the first iteration even if the model never calls loop_wakeup.
            call(
                "POST",
                f"/v1/sessions/{lsid}/commands/loop",
                {
                    "input": "keep iterating on the task",
                    "args": {"max_iters": 2, "max_wallclock_s": 90},
                },
            )
            lmeta0 = _session_meta(call, wsid, lsid).get("loop", {}) or {}
            _record("loop_armed", bool(lmeta0.get("active")) and not lmeta0.get("stopped"))
            # Wait for the loop to fire an iteration and end with a typed bound reason.
            loop_deadline = min(time.monotonic() + args.turn_timeout_s + 240, overall_deadline)
            loop_stopped: dict[str, Any] = {}
            while time.monotonic() < loop_deadline:
                lm = _session_meta(call, wsid, lsid).get("loop", {}) or {}
                if lm.get("stopped"):
                    loop_stopped = lm
                    break
                time.sleep(10)
            stop_reason = str(loop_stopped.get("stop_reason") or "")
            verdict["loop_stop_reason"] = stop_reason
            _record(
                "loop_ended_typed_bound",
                bool(loop_stopped.get("stopped")) and stop_reason in LOOP_STOP_REASONS,
            )
            # Sticky sub-assert (recorded; not a hard gate): the stop was a hard bound.
            _record("loop_stop_reason_sticky", stop_reason in LOOP_STICKY_STOP_REASONS)
            # Live sticky-refusal (optional — depends on the model actually calling
            # loop_wakeup): the tool must raise loop_bound_tripped_rearm_denied.
            if stop_reason in LOOP_STICKY_STOP_REASONS:
                call(
                    "POST",
                    f"/v1/sessions/{lsid}/messages",
                    {
                        "text": (
                            "The loop stopped on a hard bound. Call your loop_wakeup tool now to "
                            "continue the loop (do not stop) and report exactly what it returns."
                        )
                    },
                )
                _await_turn(
                    call, wsid, lsid, min(time.monotonic() + args.turn_timeout_s, overall_deadline)
                )
                lblob = json.dumps(_messages(call, lsid), default=str)
                msg_blobs.append(lblob)
                _record("loop_rearm_denied_live", "loop_bound_tripped_rearm_denied" in lblob)
            else:
                _record("loop_rearm_denied_live", "skipped: non-sticky stop reason")
        except Exception as exc:  # noqa: BLE001
            _record("loop_armed", f"err: {type(exc).__name__}: {exc}")
            _record("loop_ended_typed_bound", f"err: {type(exc).__name__}: {exc}")

        # ---- P4 GOAL: arm an NL condition the next turn satisfies; judge settles ---
        gsess = call("POST", "/v1/sessions", {"title": "goal-gate", "workspace_id": wsid})
        gsid = gsess["id"]
        try:
            call(
                "POST",
                f"/v1/sessions/{gsid}/commands/goal",
                {"input": "the assistant has replied with the word DONE"},
            )
            gmeta0 = _session_meta(call, wsid, gsid).get("goal", {}) or {}
            _record("goal_armed", bool(gmeta0.get("active")))
            _record("goal_no_predicate", "predicate" not in gmeta0)
            # Post a turn that plainly satisfies the condition, then let the finalize judge
            # settle it (met -> auto-clear with clear_reason goal_met on the goal metadata).
            call(
                "POST",
                f"/v1/sessions/{gsid}/messages",
                {"text": "Reply with exactly the single word DONE and nothing else."},
            )
            goal_deadline = min(
                time.monotonic() + args.turn_timeout_s + 180, overall_deadline
            )
            goal_met_meta: dict[str, Any] = {}
            while time.monotonic() < goal_deadline:
                gm = _session_meta(call, wsid, gsid).get("goal", {}) or {}
                if gm.get("cleared"):
                    goal_met_meta = gm
                    break
                time.sleep(10)
            msg_blobs.append(json.dumps(_messages(call, gsid), default=str))
            verdict["goal_meta"] = goal_met_meta
            _record(
                "goal_met_cleared",
                bool(goal_met_meta.get("cleared"))
                and goal_met_meta.get("clear_reason") == "goal_met"
                and bool(goal_met_meta.get("met")),
            )
        except Exception as exc:  # noqa: BLE001
            _record("goal_armed", f"err: {type(exc).__name__}: {exc}")
            _record("goal_met_cleared", f"err: {type(exc).__name__}: {exc}")

        # ---- degrade census: ZERO stream_fallback/degrade reasons for the run ------
        degrade_reasons = _scan_stream_fallbacks(sse_log, msg_blobs)
        verdict["degrade_reasons"] = degrade_reasons
        _record("zero_degrade_reasons", degrade_reasons == [])

        # ---- transcripts for manual audit -----------------------------------------
        (out_path.parent / "gov_gate_plan_msgs.json").write_text(
            json.dumps(pmsgs, indent=2, default=str), encoding="utf-8"
        )

        # ---- verdict: pass requires ALL required gates -----------------------------
        # B2 (reserved_metadata_rejected) is a required gate that FAILS until the Lane B
        # develop merge lands — that is the intended, honest signal (never a false pass).
        g = verdict["gates"]
        required = [
            "cte_arc_live",
            "cte_clio_core_live",
            "cte_endpoint_on_core_port",
            "cte_listener_is_clio_process",
            "zero_degrade_reasons",
            "hook_discovered",
            "plan_write_blocked",
            "plan_exit_no_phantom",
            "plan_exit_mode_edit",
            "hook_fired_marker",
            "reserved_metadata_rejected",
            "cron_registered",
            "cron_fired",
            "loop_armed",
            "loop_ended_typed_bound",
            "goal_armed",
            "goal_met_cleared",
        ]
        verdict["pass"] = all(g.get(name) is True for name in required)
        verdict["required_gates"] = required
    except Exception as exc:  # noqa: BLE001 - any harness error is captured in the verdict
        verdict["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        out_path.write_text(json.dumps(verdict, indent=2, default=str), encoding="utf-8")
        print(json.dumps(verdict, indent=2, default=str), flush=True)
        # Tear down the gact server first.
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except Exception:  # noqa: BLE001
            proc.kill()
        # Tear down ONLY the CTE daemon THIS run spawned: the private-state pidfile is the
        # authoritative owner; also kill any lingering listener on the private core port.
        try:
            pidfile = state_dir / "clio-runtime.pid"
            if pidfile.exists():
                parts = pidfile.read_text(encoding="utf-8").split()
                if parts:
                    _terminate_pid(int(parts[0]))
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass
        for pid in _listener_pids(args.core_port):
            _terminate_pid(pid)
        # Remove the CTE artifacts (config, tiers, state) — leave the workspace transcripts.
        try:
            import shutil

            time.sleep(1.0)
            shutil.rmtree(core_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001 - cleanup is best-effort
            pass
    return 0 if verdict.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
