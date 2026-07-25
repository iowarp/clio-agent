#!/usr/bin/env python3
"""Live gates for epic #1031 (post-#974 three-pillar redesign) on the REAL box.

Reuses the boot/drive pattern of ``live_gate_observe_1000.py`` (subprocess
``run_server`` + health poll + ``requests`` client) and adds per-pillar gates
driven against a real gact server bound to the ``claude_code`` provider (haiku
by default — owner: cost is not a concern, run many angles).

Subcommands:
    p3   Provenance cross-JOB lineage bind + reproduce (headline).
    p1   Unified permissions: reads-never-gated, advisory write floor, live grant.
    p2   Loop-inbox: mid-turn steer (202) + fire-and-forget completion injection.

Each writes a verdict JSON to ``out/live_gate_1031_<name>.json`` and prints it.
Run detached; the CTE daemon (127.0.0.1:9413) must already be up.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Shared harness                                                              #
# --------------------------------------------------------------------------- #


def _client(base: str) -> Callable[..., Any]:
    import requests

    def call(method: str, path: str, body=None, params=None, ok=(200, 201), raw=False):
        r = requests.request(method, f"{base}{path}", json=body, params=params, timeout=300)
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
        if raw:
            return r
        return r.json() if r.content else {}

    return call


def _boot(
    port: int,
    model: str,
    transport: str,
    sse_log: str,
    boot_script: str = "",
    extra_env: dict[str, str] | None = None,
) -> subprocess.Popen:
    env = dict(os.environ)
    env["CLIO_LM_PROVIDER"] = "claude_code"
    env["CLIO_LM_MODEL"] = model
    env["CLIO_CLAUDE_CODE_TRANSPORT"] = transport
    env["CLIO_STREAM_AUDIT_LOG"] = str((REPO / sse_log).resolve())
    env.pop("CLIO_LM_THINKING_LEVEL", None)
    if extra_env:
        env.update(extra_env)
    # A boot_script (e.g. live_gate_boot.py) mounts extra in-process tools before
    # starting the server; the default is a bare run_server.
    if boot_script:
        cmd = [sys.executable, str((REPO / boot_script).resolve()), str(port)]
    else:
        cmd = [
            sys.executable,
            "-c",
            f"from clio_agent.gact.app import run_server; run_server(host='127.0.0.1', port={port})",
        ]
    return subprocess.Popen(cmd, env=env, cwd=str(REPO))


def _wait_health(call: Callable[..., Any], timeout: float = 240.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            call("GET", "/v1/health", ok=(200, 503))
            return True
        except Exception:
            time.sleep(2)
    return False


def _bind_provider(call: Callable[..., Any], model: str) -> None:
    call("PUT", "/v1/providers/lm", {"provider": "claude_code", "api_base": "", "model": model})
    call("GET", "/v1/providers/lm/wait", params={"timeout": 120}, ok=(200, 503))


def _allow_all(call: Callable[..., Any]) -> None:
    call(
        "PUT",
        "/v1/policies",
        {"policies": [{"scope": "workspace", "action": "allow", "tool_name_pattern": "*"}]},
    )


def _workspace(call: Callable[..., Any], name: str, root_path: str) -> str:
    ws = call("POST", "/v1/workspaces", {"name": name, "root_path": root_path})
    return ws.get("id") or ws.get("workspace_id")


def _session(call: Callable[..., Any], workspace_id: str, title: str, **extra) -> str:
    body = {"title": title, "workspace_id": workspace_id, **extra}
    return call("POST", "/v1/sessions", body)["id"]


# A plain leaf react agent that holds the native fs/shell tools directly — the
# default session agent is the earthscope ORCHESTRATOR (no tools of its own), so
# the permission/provenance pillars are validated against this leaf instead.
REACT_LEAF = (REPO / "scripts/live_gate_blueprints/react-leaf").resolve()
REACT_LEAF_XFORM = (REPO / "scripts/live_gate_blueprints/react-leaf-xform").resolve()


def _activate_leaf(call: Callable[..., Any], sid: str) -> dict:
    """Activate the native-tools leaf react blueprint on ``sid`` (by on-disk path)."""
    return call(
        "POST", f"/v1/sessions/{sid}/agent-blueprint", {"path": str(REACT_LEAF)}, ok=(200, 201)
    )


def _leaf_session(call: Callable[..., Any], workspace_id: str, title: str, **extra) -> str:
    """Create a session AND activate the leaf react agent on it."""
    sid = _session(call, workspace_id, title, **extra)
    _activate_leaf(call, sid)
    return sid


def _xform_session(call: Callable[..., Any], workspace_id: str, title: str, **extra) -> str:
    """Create a session AND activate the leaf+transform react agent (needs the
    live_gate_boot.py server which mounts xform_summarize_csv in-process)."""
    sid = _session(call, workspace_id, title, **extra)
    call(
        "POST", f"/v1/sessions/{sid}/agent-blueprint", {"path": str(REACT_LEAF_XFORM)}, ok=(200, 201)
    )
    return sid


# A tier-1 react orchestrator + one tier-2 worker child that holds the native
# tools — for P2 completion-injection (a fire-and-forget child completing mid-turn).
ORCH_WORKER = (REPO / "scripts/live_gate_blueprints/orchestrator-worker").resolve()


def _orch_session(call: Callable[..., Any], workspace_id: str, title: str, **extra) -> str:
    """Create a session AND activate the orchestrator+worker blueprint on it."""
    sid = _session(call, workspace_id, title, **extra)
    call(
        "POST", f"/v1/sessions/{sid}/agent-blueprint", {"path": str(ORCH_WORKER)}, ok=(200, 201)
    )
    return sid


def _post(call: Callable[..., Any], sid: str, text: str, ok=(200, 201, 202)) -> dict:
    return call("POST", f"/v1/sessions/{sid}/messages", {"text": text}, ok=ok)


def _wait_turn(call: Callable[..., Any], wsid: str, sid: str, timeout_s: float) -> str:
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


def _messages(call: Callable[..., Any], sid: str) -> list[dict]:
    return call("GET", f"/v1/sessions/{sid}/messages").get("messages", [])


def _session_error(call: Callable[..., Any], sid: str) -> dict:
    """Best-effort capture of a session's error detail + last assistant text (for
    diagnosing an ``error`` turn status)."""
    detail: dict = {}
    try:
        sess = call("GET", f"/v1/sessions/{sid}")
        detail["session_error"] = sess.get("error") or sess.get("last_error") or sess.get("status")
        detail["session_keys"] = sorted(sess.keys())
    except Exception as exc:  # noqa: BLE001
        detail["session_fetch_error"] = f"{type(exc).__name__}: {exc}"
    try:
        msgs = _messages(call, sid)
        tail = json.dumps(msgs[-2:], default=str)
        detail["last_messages_excerpt"] = tail[:1200]
    except Exception:  # noqa: BLE001
        pass
    return detail


def _dump(out_path: Path, name: str, blob: Any) -> None:
    (out_path.parent / name).write_text(json.dumps(blob, indent=2, default=str), encoding="utf-8")


def _set_policies(call: Callable[..., Any], policies: list[dict]) -> None:
    call("PUT", "/v1/policies", {"policies": policies})


def _pending(call: Callable[..., Any], sid: str) -> list[dict]:
    return call(
        "GET", "/v1/permissions", params={"session_id": sid, "status": "pending"}
    ).get("permissions", [])


def _wait_pending(call: Callable[..., Any], sid: str, timeout: float = 120.0) -> dict | None:
    """Poll for the first pending permission row on ``sid`` (the mid-turn gate block)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _pending(call, sid)
        if rows:
            return rows[0]
        time.sleep(2)
    return None


def _resolve(call: Callable[..., Any], pid: str, action: str) -> None:
    call("POST", f"/v1/permissions/{pid}", {"action": action}, ok=(200, 204))


def _audit(call: Callable[..., Any], sid: str) -> list[dict]:
    return call(
        "GET", "/v1/permissions", params={"session_id": sid, "status": "all"}
    ).get("permissions", [])


class _SSECollector:
    """Background collector of a session's SSE event types (for P2 liveness signals).

    Opens ``GET /v1/sessions/{sid}/events`` in a daemon thread and records every
    ``event:`` line's type. Used to prove a mid-turn ``loop_inbox.drained`` (the
    completion-injection signal) and ``agent.task.*`` transitions actually fire on
    the wire, not just in the transcript."""

    def __init__(self, base: str, sid: str) -> None:
        import threading

        self.base = base
        self.sid = sid
        self.events: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        import requests

        try:
            with requests.get(
                f"{self.base}/v1/sessions/{self.sid}/events", stream=True, timeout=900
            ) as r:
                for raw in r.iter_lines(decode_unicode=True):
                    if self._stop.is_set():
                        return
                    if raw and raw.startswith("event:"):
                        self.events.append(raw.split(":", 1)[1].strip())
        except Exception:
            return

    def start(self) -> "_SSECollector":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def count(self, event_type: str) -> int:
        return sum(1 for e in self.events if e == event_type)


# --------------------------------------------------------------------------- #
# P3 — provenance cross-JOB lineage bind + reproduce                          #
# --------------------------------------------------------------------------- #

def _artifact_id(a: dict) -> str:
    """The relay artifact_id from a list-artifacts record item (``_record_wire``).

    The list route returns logical records whose id is ``head_artifact_id``; a
    single-version resolve returns ``artifact_id``. Accept either."""
    return (
        a.get("head_artifact_id")
        or a.get("artifact_id")
        or (a.get("versions") or [{}])[-1].get("artifact_id")
        or ""
    )


_SALES_CSV = "region,units,revenue\nwest,100,5000\neast,150,7500\nnorth,80,4000\n"


def _p3_prompt_a(sales_path: Path) -> str:
    return (
        f"A CSV file already exists on disk at the absolute path {sales_path}. Register that "
        "EXISTING file as a tracked data artifact by calling the create_artifact tool with "
        f"name='sales', kind='dataset', and path='{sales_path}'. Do NOT pass any 'content' "
        "argument and do NOT write a new file — just register the file that is already there "
        f"at {sales_path}. Confirm the artifact was registered at that exact path."
    )


def _p3_prompt_b(sales_path: Path, summary_path: Path) -> str:
    return (
        f"An earlier job produced the sales CSV at {sales_path}. Transform it into a "
        "summary in ONE step using the summarize_csv tool: call summarize_csv with "
        f"input_path='{sales_path}' and output_path='{summary_path}'. That tool reads "
        "the input, sums the revenue, writes the summary CSV, AND tracks it as an "
        "artifact automatically — so you do NOT need to call create_artifact afterward. "
        "Just make that one summarize_csv call and then report the total revenue it "
        "returned and the output file path."
    )


def gate_p3(call: Callable[..., Any], out_path: Path, turn_timeout_s: float) -> dict:
    """Job A produces `sales` in ws1; job B (separate session, SAME root) consumes it
    and produces `summary`; then assert summary's upstream lineage binds to sales'
    real producing version across the job boundary, and the export crate compiles a
    transitive reproduce."""

    # Shared root on disk — the cross-job bind gates on Workspace.root_path equality.
    shared_root = (REPO / "out/live_gate_1031_p3_root").resolve()
    shared_root.mkdir(parents=True, exist_ok=True)
    sales_path = shared_root / "sales.csv"
    summary_path = shared_root / "summary.csv"
    # Clean any prior run's files, then PRE-WRITE sales.csv so job A registers an
    # EXISTING file by exact path (deterministic — avoids the model writing a
    # name-derived filename without the .csv suffix). Job B then transforms it.
    sales_path.unlink(missing_ok=True)
    summary_path.unlink(missing_ok=True)
    for stale in ("sales", "summary"):  # prior name-derived artifacts
        (shared_root / stale).unlink(missing_ok=True)
    sales_path.write_text(_SALES_CSV, encoding="utf-8")
    prompt_a = _p3_prompt_a(sales_path)
    prompt_b = _p3_prompt_b(sales_path, summary_path)
    verdict: dict = {"pillar": "P3", "prompt_a": prompt_a, "prompt_b": prompt_b}
    _allow_all(call)

    # --- Job A: workspace 1 ------------------------------------------------
    ws1 = _workspace(call, "p3-job-a", str(shared_root))
    sess_a = _xform_session(call, ws1, "p3-job-a")
    _post(call, sess_a, prompt_a)
    verdict["status_a"] = _wait_turn(call, ws1, sess_a, turn_timeout_s)
    if verdict["status_a"] == "error":
        verdict["job_a_error_detail"] = _session_error(call, sess_a)
    _dump(out_path, "p3_job_a_messages.json", _messages(call, sess_a))
    arts_a = call("GET", f"/v1/sessions/{sess_a}/artifacts").get("artifacts", [])
    verdict["artifacts_a"] = [
        {"id": _artifact_id(a), "name": a.get("name"), "kind": a.get("kind")} for a in arts_a
    ]
    sales = next((a for a in arts_a if "sales" in (a.get("name") or "").lower()), None)
    verdict["sales_minted"] = bool(sales)
    verdict["sales_on_disk"] = sales_path.exists()

    # --- Job B: workspace 2, SAME root -------------------------------------
    ws2 = _workspace(call, "p3-job-b", str(shared_root))
    verdict["distinct_workspaces"] = ws1 != ws2
    sess_b = _xform_session(call, ws2, "p3-job-b")
    _post(call, sess_b, prompt_b)
    verdict["status_b"] = _wait_turn(call, ws2, sess_b, turn_timeout_s)
    _dump(out_path, "p3_job_b_messages.json", _messages(call, sess_b))
    verdict["summary_on_disk"] = summary_path.exists()
    arts_b = call("GET", f"/v1/sessions/{sess_b}/artifacts").get("artifacts", [])
    verdict["artifacts_b"] = [
        {"id": _artifact_id(a), "name": a.get("name"), "kind": a.get("kind")} for a in arts_b
    ]
    summary = next((a for a in arts_b if "summary" in (a.get("name") or "").lower()), None)
    verdict["summary_minted"] = bool(summary)

    # --- Cross-job lineage assertion ---------------------------------------
    if summary is None or sales is None:
        verdict["error"] = "did not mint both artifacts; cannot assert cross-job lineage"
        verdict["pass"] = False
        return verdict

    sales_id = _artifact_id(sales)
    summary_id = _artifact_id(summary)
    verdict["sales_id"] = sales_id
    verdict["summary_id"] = summary_id

    # Capture the raw transform records for both jobs (diagnostic: which call got
    # which used/generated edges) — the ground truth behind the lineage walk.
    try:
        tr_a = call("GET", f"/v1/sessions/{sess_a}/transforms").get("transforms", [])
        tr_b = call("GET", f"/v1/sessions/{sess_b}/transforms").get("transforms", [])
        _dump(out_path, "p3_transforms.json", {"job_a": tr_a, "job_b": tr_b})
        verdict["transform_edge_summary"] = {
            "job_b": [
                {
                    "tool": t.get("instrument", {}).get("tool") or t.get("call_id", "")[:12],
                    "used": [e.get("artifact_id") or e.get("external_ref") for e in t.get("used", [])],
                    "generated": [e.get("artifact_id") for e in t.get("generated", [])],
                    "notes": t.get("notes", []),
                }
                for t in tr_b
            ]
        }
    except Exception as exc:  # noqa: BLE001
        verdict["transforms_error"] = f"{type(exc).__name__}: {exc}"

    lin = call(
        "GET",
        f"/v1/artifacts/{summary_id}/lineage",
        params={"direction": "upstream", "depth": 12},
    )
    _dump(out_path, "live_gate_1031_p3_lineage.json", lin)
    node_ids = {n.get("id") or n.get("artifact_id") for n in lin.get("nodes", [])}
    edges = lin.get("edges", [])
    verdict["lineage_node_count"] = len(lin.get("nodes", []))
    verdict["lineage_edge_roles"] = sorted({e.get("role") for e in edges if e.get("role")})
    # The headline: summary's upstream closure reaches sales' REAL producing version.
    reaches_sales = sales_id in node_ids
    cross_ws_edges = [e for e in edges if e.get("cross_workspace_bind") or "cross_workspace" in json.dumps(e, default=str)]
    verdict["reaches_sales_producer"] = reaches_sales
    verdict["cross_workspace_bind_edges"] = len(cross_ws_edges)

    # --- Transitive reproduce inside the export crate ----------------------
    try:
        crate = call("GET", f"/v1/artifacts/{summary_id}/export", ok=(200,), raw=True)
        crate_path = out_path.parent / "live_gate_1031_p3_summary.crate.zip"
        crate_path.write_bytes(crate.content)
        with zipfile.ZipFile(crate_path) as zf:
            names = zf.namelist()
            repro = next((n for n in names if n.endswith("reproduce.py")), None)
            repro_text = zf.read(repro).decode("utf-8", "replace") if repro else ""
        verdict["crate_entries"] = len(names)
        verdict["reproduce_present"] = bool(repro)
        # A transitive reproduce mentions BOTH stages (sales -> summary).
        verdict["reproduce_mentions_sales"] = "sales" in repro_text.lower()
        verdict["reproduce_mentions_summary"] = "summary" in repro_text.lower()
    except Exception as exc:  # noqa: BLE001
        verdict["export_error"] = f"{type(exc).__name__}: {exc}"

    verdict["pass"] = bool(
        verdict.get("sales_minted")
        and verdict.get("summary_minted")
        and verdict.get("distinct_workspaces")
        and reaches_sales
        and verdict.get("status_a") in ("idle", "completed")
        and verdict.get("status_b") in ("idle", "completed")
    )
    return verdict


# --------------------------------------------------------------------------- #
# Driver                                                                      #
# --------------------------------------------------------------------------- #

def _trace_text(msgs: list[dict]) -> str:
    """Flatten every part of every message into one searchable blob (tool obs + text)."""
    return json.dumps(msgs, default=str)


def gate_p1(call: Callable[..., Any], out_path: Path, turn_timeout_s: float) -> dict:
    """Unified permissions, Windows-focused. Sub-checks (each its own session on one
    server): reads-never-gated; live mid-turn grant; advisory write floor (in-root
    succeeds, out-of-root DENIED with outside_allowed_roots); shell fence in-root
    (the suspect Windows codex path); ai-review reviewer verdict recorded."""

    verdict: dict = {"pillar": "P1", "checks": {}}
    root = (REPO / "out/live_gate_1031_p1_root").resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "readme.txt").write_text("the-secret-is-42", encoding="utf-8")
    escape = (root.parent / "p1_escape_OUTSIDE.txt").resolve()  # under out/, NOT the ws root
    # ^ out/ is inside the repo (an allowed root), so use a target truly outside all
    #   allowed roots: a sibling of the user profile temp, under the drive root.
    escape = Path(os.path.expanduser("~")).resolve() / "clio_p1_escape_OUTSIDE.txt"
    escape.unlink(missing_ok=True)
    wsid = _workspace(call, "p1", str(root))

    # --- A. reads never gated (ask mode, no allow policy) -------------------
    _set_policies(call, [])
    sid_a = _leaf_session(call, wsid, "p1-reads", approval_mode="ask")
    _post(call, sid_a, "Read the file 'readme.txt' in the working directory and tell me its contents.")
    st_a = _wait_turn(call, wsid, sid_a, turn_timeout_s)
    msgs_a = _messages(call, sid_a)
    _dump(out_path, "p1_reads_messages.json", msgs_a)
    verdict["checks"]["reads_never_gated"] = {
        "status": st_a,
        "permission_rows": len(_audit(call, sid_a)),  # MUST be 0 — a read never gates
        "read_the_secret": "the-secret-is-42" in _trace_text(msgs_a),
        "pass": st_a in ("idle", "completed") and len(_audit(call, sid_a)) == 0,
    }

    # --- B. live mid-turn grant (ask mode; harness resolves the pending row) -
    _set_policies(call, [])
    sid_b = _leaf_session(call, wsid, "p1-grant", approval_mode="ask")
    _post(call, sid_b, "Create a file named 'granted.txt' in the working directory containing the word GRANTED.")
    row = _wait_pending(call, sid_b, timeout=180.0)
    granted_ok = False
    if row is not None:
        _resolve(call, row.get("id") or row.get("pid"), "allow")
        granted_ok = True
    st_b = _wait_turn(call, wsid, sid_b, turn_timeout_s)
    verdict["checks"]["live_mid_turn_grant"] = {
        "status": st_b,
        "pending_row_appeared": granted_ok,
        "file_written_after_grant": (root / "granted.txt").exists(),
        "pass": granted_ok and (root / "granted.txt").exists() and st_b in ("idle", "completed"),
    }

    # --- C. advisory write floor: in-root OK, out-of-root DENIED (bypass mode) -
    _set_policies(call, [])
    sid_c = _leaf_session(call, wsid, "p1-floor", approval_mode="bypass")
    _post(
        call,
        sid_c,
        "Do TWO things with the file-writing tool: (1) create 'inside.txt' in the "
        "working directory with the text OK; (2) then also try to create a file at the "
        f"absolute path {escape} with the text ESCAPE. Report what happened for each.",
    )
    st_c = _wait_turn(call, wsid, sid_c, turn_timeout_s)
    msgs_c = _messages(call, sid_c)
    _dump(out_path, "p1_floor_messages.json", msgs_c)
    trace_c = _trace_text(msgs_c)
    # The floor emits the human message "Path is outside allowed roots" (spaces) and
    # the typed code "outside_allowed_roots" (underscores) — accept either.
    denied_in_trace = ("outside allowed roots" in trace_c) or ("outside_allowed_roots" in trace_c)
    verdict["checks"]["write_floor"] = {
        "status": st_c,
        "in_root_written": (root / "inside.txt").exists(),
        "out_of_root_written": escape.exists(),  # MUST be False
        "outside_allowed_roots_in_trace": denied_in_trace,
        "pass": (root / "inside.txt").exists()
        and not escape.exists()
        and denied_in_trace
        and st_c in ("idle", "completed"),
    }
    escape.unlink(missing_ok=True)

    # --- D. shell fence in-root (the suspect Windows codex path) ------------
    _set_policies(call, [])
    sid_d = _leaf_session(call, wsid, "p1-shell", approval_mode="bypass")
    _post(
        call,
        sid_d,
        "Use a shell command to create a file 'shellout.txt' in the working directory "
        "containing the word SHELL, then run a shell command to print its contents.",
    )
    st_d = _wait_turn(call, wsid, sid_d, turn_timeout_s)
    msgs_d = _messages(call, sid_d)
    _dump(out_path, "p1_shell_messages.json", msgs_d)
    verdict["checks"]["shell_fence_in_root"] = {
        "status": st_d,
        "shell_wrote_in_root": (root / "shellout.txt").exists(),
        "note": "if False on Windows -> codex shell-fence breaks normal in-root writes (blocker)",
        "pass": (root / "shellout.txt").exists() and st_d in ("idle", "completed"),
    }

    # --- E. ai-review reviewer verdict recorded (grantor=reviewer) ----------
    _set_policies(call, [])
    sid_e = _leaf_session(call, wsid, "p1-ai-review", approval_mode="ai-review")
    _post(call, sid_e, "Create a file 'reviewed.txt' in the working directory with the word REVIEWED.")
    # The reviewer runs IN-PROCESS on the gate thread; the row is briefly pending
    # (reason=ai_review_reviewer_pending) during its LM call. We must NOT resolve
    # that pending row ourselves (that races the reviewer and steals the decision) —
    # only resolve if the reviewer EXPLICITLY escalates (reason=ai_review_escalate),
    # which is the sole case designed for a human. Otherwise wait for the reviewer's
    # own autonomous allow/deny (grantor=reviewer).
    human_resolved_escalation = False
    deadline = time.monotonic() + 200.0
    while time.monotonic() < deadline:
        audit = _audit(call, sid_e)
        settled = [
            r for r in audit
            if r.get("status") != "pending" and str(r.get("reason", "")).startswith("ai_review")
        ]
        if settled:
            break
        escalated = [
            r for r in _pending(call, sid_e)
            if str(r.get("reason", "")).startswith("ai_review_escalate")
        ]
        if escalated:  # the reviewer handed off to a human — resolve as human
            _resolve(call, escalated[0].get("id") or escalated[0].get("pid"), "allow")
            human_resolved_escalation = True
        time.sleep(2)
    st_e = _wait_turn(call, wsid, sid_e, turn_timeout_s)
    audit_e = _audit(call, sid_e)
    _dump(out_path, "p1_ai_review_audit.json", audit_e)
    reviewer_rows = [r for r in audit_e if r.get("grantor") == "reviewer"]
    ai_reasons = sorted(
        {str(r.get("reason")) for r in audit_e if str(r.get("reason", "")).startswith("ai_review")}
    )
    # PASS = the reviewer autonomously decided and it was RECORDED with grantor=reviewer
    # (owner's requirement), OR it escalated and a human resolved (also valid).
    verdict["checks"]["ai_review"] = {
        "status": st_e,
        "reviewer_grantor_rows": len(reviewer_rows),
        "ai_review_reasons": ai_reasons,
        "human_resolved_escalation": human_resolved_escalation,
        "pass": (len(reviewer_rows) > 0 or human_resolved_escalation)
        and len(ai_reasons) > 0
        and st_e in ("idle", "completed"),
    }

    verdict["pass"] = all(c.get("pass") for c in verdict["checks"].values())
    return verdict


def gate_bashprobe(call: Callable[..., Any], out_path: Path, turn_timeout_s: float) -> dict:
    """Isolate whether the shell tool works AT ALL on Windows in the live server (the
    codex-fence suspect). One turn: write a file via bash into an EXPLICIT absolute dir,
    then dump the full transcript so we see the tool call + any fence error."""

    verdict: dict = {"pillar": "bashprobe"}
    _allow_all(call)
    root = (REPO / "out/live_gate_1031_bashprobe_root").resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / "bashout.txt"
    target.unlink(missing_ok=True)
    wsid = _workspace(call, "bashprobe", str(root))
    sid = _leaf_session(call, wsid, "bashprobe", approval_mode="bypass")
    _post(
        call,
        sid,
        "Run a single shell command that writes the word HELLO into the file at the "
        f"absolute path {target}. Then run a shell command to print that file's contents. "
        "Report the exact command output.",
    )
    verdict["status"] = _wait_turn(call, wsid, sid, turn_timeout_s)
    msgs = _messages(call, sid)
    _dump(out_path, "bashprobe_messages.json", msgs)
    trace = _trace_text(msgs)
    verdict["file_written"] = target.exists()
    verdict["trace_has_shell_error"] = any(
        s in trace for s in ("syntax is incorrect", "rc=1", "EROFS", "WinError", "denied", "sandbox")
    )
    verdict["trace_excerpt"] = trace[:1500]
    verdict["pass"] = target.exists() and verdict["status"] in ("idle", "completed")
    return verdict


def _post_async(call: Callable[..., Any], base: str, sid: str, text: str) -> dict:
    """POST a message WITHOUT waiting — returns the ack (200 new-turn or 202 steer)."""
    import requests

    r = requests.post(
        f"{base}/v1/sessions/{sid}/messages", json={"text": text}, timeout=60
    )
    return {"status_code": r.status_code, "body": r.json() if r.content else {}}


def _wait_busy(call: Callable[..., Any], wsid: str, sid: str, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = call("GET", f"/v1/sessions?workspace_id={wsid}").get("sessions", [])
        row = next((r for r in rows if r.get("id") == sid), {})
        if row.get("status") == "running":
            return True
        if row.get("status") in ("idle", "completed", "error"):
            return False  # turn already finished — missed the window
        time.sleep(0.5)
    return False


def gate_p2(call: Callable[..., Any], out_path: Path, turn_timeout_s: float, base: str) -> dict:
    """Loop-inbox live gate. Two sub-checks on one server: (A) a mid-turn user POST
    while a turn runs returns 202 and the steer surfaces (not 409); (B) a
    fire-and-forget child that completes DURING the parent's turn injects its
    completion mid-turn (loop_inbox.drained SSE + agent.task.* + live handle)."""

    verdict: dict = {"pillar": "P2", "checks": {}}
    _allow_all(call)
    root = (REPO / "out/live_gate_1031_p2_root").resolve()
    root.mkdir(parents=True, exist_ok=True)

    # --- A. mid-turn steer = 202 (not 409) ---------------------------------
    sid_a = _leaf_session(call, _workspace(call, "p2-steer", str(root)), "p2-steer",
                          approval_mode="bypass")
    wsid_a = call("GET", f"/v1/sessions/{sid_a}").get("workspace_id", "")
    # A multi-step task keeps the turn busy long enough to land a mid-turn POST.
    _post_async(
        call, base, sid_a,
        "Run these as FOUR separate shell commands, one at a time: (1) print 'step 1' and "
        "sleep 4 seconds; (2) print 'step 2' and sleep 4 seconds; (3) print 'step 3' and "
        "sleep 4 seconds; (4) print 'done'. Report each step's output.",
    )
    busy = _wait_busy(call, wsid_a, sid_a, timeout=60.0)
    steer_resp = _post_async(call, base, sid_a, "Also, while you work, remember the codeword BANANA.")
    verdict["checks"]["mid_turn_steer_202"] = {
        "turn_went_busy": busy,
        "steer_status_code": steer_resp["status_code"],
        "steer_is_202": steer_resp["status_code"] == 202,
        "not_409": steer_resp["status_code"] != 409,
    }
    st_a = _wait_turn(call, wsid_a, sid_a, turn_timeout_s)
    msgs_a = _messages(call, sid_a)
    _dump(out_path, "p2_steer_messages.json", msgs_a)
    trace_a = _trace_text(msgs_a)
    steer_surfaced = ("BANANA" in trace_a) or ("Mid-turn user steer" in trace_a) or any(
        (m.get("metadata") or {}).get("mid_turn_steer") for m in msgs_a
    )
    verdict["checks"]["mid_turn_steer_202"].update(
        {"status": st_a, "steer_surfaced": steer_surfaced,
         "pass": busy and steer_resp["status_code"] == 202 and steer_surfaced}
    )

    # --- B. fire-and-forget completion injected mid-turn -------------------
    child_file = root / "worker_did_this.txt"
    child_file.unlink(missing_ok=True)
    sid_b = _orch_session(call, _workspace(call, "p2-inject", str(root)), "p2-inject",
                          approval_mode="bypass")
    wsid_b = call("GET", f"/v1/sessions/{sid_b}").get("workspace_id", "")
    sse = _SSECollector(base, sid_b).start()
    _post(
        call, sid_b,
        "Do these steps IN ORDER, and do NOT call wait_agent_tasks or observe_agent_tasks: "
        "(1) Dispatch ONE background worker task that writes the word DONE into the file "
        f"{child_file} (a quick shell command). (2) Then, WITHOUT waiting on the worker, keep "
        "your own turn active by running this pair of shell commands THREE times in sequence "
        "(six shell calls total, one at a time): 'Start-Sleep -Seconds 15' then 'Get-Content "
        f"{child_file}' (the file may not exist yet, that is fine, just continue). (3) After "
        "the third pair, report whether the worker's result came back and the file contents. "
        "Keep the turn alive with your own sleeps, never block via a wait/observe tool.",
    )
    st_b = _wait_turn(call, wsid_b, sid_b, turn_timeout_s)
    # The worker sleeps ~8s; it may still be running when the parent turn ends. Poll
    # for its file (and let the terminal SSE flush) before asserting.
    for _ in range(15):
        if child_file.exists():
            break
        time.sleep(2)
    time.sleep(3)
    sse.stop()
    msgs_b = _messages(call, sid_b)
    _dump(out_path, "p2_inject_messages.json", msgs_b)
    tasks = call("GET", f"/v1/sessions/{sid_b}/agent-tasks").get("tasks", [])
    live_ok = False
    if tasks:
        tid = tasks[0].get("task_id")
        try:
            live = call("GET", f"/v1/agent-tasks/{tid}/live")
            live_ok = bool(live.get("task"))
            _dump(out_path, "p2_live_handle.json", live)
        except Exception as exc:  # noqa: BLE001
            verdict["live_handle_error"] = f"{type(exc).__name__}: {exc}"
    trace_b = _trace_text(msgs_b)
    # The DEFINITIVE mid-turn-injection signal is loop_inbox.drained: it is emitted ONLY
    # by drain_active_session_inbox — the mid-turn tool-observation drain carrier (#1035).
    # Its presence + agent.task.consumed proves the child's completion was injected into
    # the RUNNING turn (not deferred to the next turn). child_file_written and the
    # persisted-trace marker are secondary (the former tests the worker's own task; the
    # latter can miss because the injected block rides the live tool-observation string,
    # not necessarily a persisted message part) — kept as info, not gating.
    drained = sse.count("loop_inbox.drained")
    verdict["checks"]["completion_injection"] = {
        "status": st_b,
        "child_task_count": len(tasks),
        "child_file_written": child_file.exists(),
        "loop_inbox_drained_sse": drained,
        "agent_task_completed_sse": sse.count("agent.task.completed"),
        "agent_task_consumed_sse": sse.count("agent.task.consumed"),
        "injection_marker_in_trace": "Background agent-task results" in trace_b,
        "live_handle_ok": live_ok,
        "sse_types_seen": sorted(set(sse.events)),
        "pass": len(tasks) >= 1
        and drained >= 1
        and sse.count("agent.task.completed") >= 1
        and live_ok
        and st_b in ("idle", "completed"),
    }

    verdict["pass"] = all(c.get("pass") for c in verdict["checks"].values())
    return verdict


def gate_composed(call: Callable[..., Any], out_path: Path, turn_timeout_s: float, base: str) -> dict:
    """The capstone: ONE synthetic multi-job/multi-workspace pipeline that threads all
    three pillars. Job A (ws1) produces `sales`. Job B (ws2, SAME root, approval_mode=
    ask) transforms it via the designated-output xform tool — whose write BLOCKS on a
    permission prompt; while the turn is blocked we (P2) post a mid-turn user steer
    (expect 202) and (P1) resolve the pending permission live; the turn then completes
    and (P3) `summary`'s lineage binds cross-job to `sales`. Requires the xform tool
    (repo .clio/mcp.yaml) + fence off, like p3."""

    verdict: dict = {"pillar": "composed"}
    shared_root = (REPO / "out/live_gate_1031_composed_root").resolve()
    shared_root.mkdir(parents=True, exist_ok=True)
    sales_path = shared_root / "sales.csv"
    summary_path = shared_root / "summary.csv"
    for f in ("sales.csv", "summary.csv", "sales", "summary"):
        (shared_root / f).unlink(missing_ok=True)
    sales_path.write_text(_SALES_CSV, encoding="utf-8")

    # --- Job A: produce `sales` in ws1 (bypass, deterministic) --------------
    _set_policies(call, [])
    ws1 = _workspace(call, "composed-job-a", str(shared_root))
    sess_a = _xform_session(call, ws1, "composed-job-a", approval_mode="bypass")
    _post(call, sess_a, _p3_prompt_a(sales_path))
    verdict["status_a"] = _wait_turn(call, ws1, sess_a, turn_timeout_s)
    arts_a = call("GET", f"/v1/sessions/{sess_a}/artifacts").get("artifacts", [])
    sales = next((a for a in arts_a if "sales" in (a.get("name") or "").lower()), None)
    verdict["sales_minted"] = bool(sales)

    # --- Job B: transform in ws2 (ask mode) with live grant + steer mid-turn -
    _set_policies(call, [])  # no allow policy → the write prompts (P1 live grant)
    ws2 = _workspace(call, "composed-job-b", str(shared_root))
    verdict["distinct_workspaces"] = ws1 != ws2
    sess_b = _xform_session(call, ws2, "composed-job-b", approval_mode="ask")
    sse = _SSECollector(base, sess_b).start()
    _post_async(
        call, base, sess_b,
        f"Transform the sales CSV at {sales_path} into a summary by calling the "
        f"summarize_csv tool with input_path='{sales_path}' and output_path='{summary_path}'. "
        "Make that single tool call, then report the total revenue it returns.",
    )
    # (P1) the xform write blocks on a permission prompt — resolve it live, mid-turn.
    row = _wait_pending(call, sess_b, timeout=180.0)
    verdict["p1_pending_row_appeared"] = bool(row)
    # (P2) while the turn is in flight, land a mid-turn user steer → expect 202.
    steer = _post_async(call, base, sess_b, "Note for context: this is the Q3 sales roll-up.")
    verdict["p2_steer_status"] = steer["status_code"]
    verdict["p2_steer_202"] = steer["status_code"] == 202
    if row is not None:
        _resolve(call, row.get("id") or row.get("pid"), "allow")
        verdict["p1_grant_resolved"] = True
    verdict["status_b"] = _wait_turn(call, ws2, sess_b, turn_timeout_s)
    time.sleep(2)
    sse.stop()
    _dump(out_path, "composed_job_b_messages.json", _messages(call, sess_b))

    # (P1) the grant was recorded as a resolved permission row.
    audit_b = _audit(call, sess_b)
    verdict["p1_resolved_rows"] = sum(1 for r in audit_b if r.get("status") != "pending")
    # (P2) the steer surfaced (mid_turn_steer message or the drain marker).
    trace_b = _trace_text(_messages(call, sess_b))
    verdict["p2_steer_surfaced"] = ("Q3 sales roll-up" in trace_b) or ("Mid-turn user steer" in trace_b)

    # (P3) cross-job lineage: summary binds to sales' producer across the job boundary.
    arts_b = call("GET", f"/v1/sessions/{sess_b}/artifacts").get("artifacts", [])
    summary = next((a for a in arts_b if "summary" in (a.get("name") or "").lower()), None)
    verdict["summary_minted"] = bool(summary)
    reaches = False
    if summary and sales:
        summary_id, sales_id = _artifact_id(summary), _artifact_id(sales)
        lin = call("GET", f"/v1/artifacts/{summary_id}/lineage",
                   params={"direction": "upstream", "depth": 12})
        _dump(out_path, "composed_lineage.json", lin)
        node_ids = {n.get("id") or n.get("artifact_id") for n in lin.get("nodes", [])}
        reaches = sales_id in node_ids
    verdict["p3_reaches_sales_producer"] = reaches

    verdict["pass"] = bool(
        verdict.get("sales_minted")
        and verdict.get("summary_minted")
        and verdict.get("distinct_workspaces")
        and verdict.get("p1_pending_row_appeared")
        and verdict.get("p1_grant_resolved")
        and verdict.get("p2_steer_202")
        and reaches
        and verdict.get("status_b") in ("idle", "completed")
    )
    return verdict


def gate_extras(call: Callable[..., Any], out_path: Path, turn_timeout_s: float) -> dict:
    """Targeted live coverage of P1 semantics not exercised by the p1 gate: an explicit
    DENY policy blocks a write even in a permissive mode (security invariant), and
    auto-edits mode auto-allows an fs write with no prompt. Uses the leaf react agent."""

    verdict: dict = {"pillar": "extras", "checks": {}}
    root = (REPO / "out/live_gate_1031_extras_root").resolve()
    root.mkdir(parents=True, exist_ok=True)
    wsid = _workspace(call, "extras", str(root))

    # --- A. explicit DENY policy blocks a write even in bypass mode ---------
    denied_file = root / "should_not_exist.txt"
    denied_file.unlink(missing_ok=True)
    _set_policies(call, [{"scope": "workspace", "action": "deny",
                          "tool_name_pattern": "fs_apply_edit_write"}])
    sid_d = _leaf_session(call, wsid, "extras-deny", approval_mode="bypass")
    _post(call, sid_d,
          f"Create a file at {denied_file} containing the word NOPE using the file-writing tool.")
    st_d = _wait_turn(call, wsid, sid_d, turn_timeout_s)
    trace_d = _trace_text(_messages(call, sid_d))
    verdict["checks"]["explicit_deny_beats_mode"] = {
        "status": st_d,
        "file_written": denied_file.exists(),  # MUST be False
        "deny_in_trace": ("deny" in trace_d.lower() or "denied" in trace_d.lower()
                          or "not permitted" in trace_d.lower()),
        "pass": (not denied_file.exists()) and st_d in ("idle", "completed"),
    }
    denied_file.unlink(missing_ok=True)

    # --- B. auto-edits mode auto-allows an fs write (no prompt) -------------
    auto_file = root / "auto_written.txt"
    auto_file.unlink(missing_ok=True)
    _set_policies(call, [])
    sid_a = _leaf_session(call, wsid, "extras-auto", approval_mode="auto-edits")
    _post(call, sid_a,
          f"Create a file at {auto_file} containing the word AUTO using the file-writing tool.")
    st_a = _wait_turn(call, wsid, sid_a, turn_timeout_s)
    rows_a = _audit(call, sid_a)
    verdict["checks"]["auto_edits_allows_fs_write"] = {
        "status": st_a,
        "file_written": auto_file.exists(),  # auto-allowed → written
        "permission_rows": len(rows_a),  # ideally 0 (no prompt) or auto-resolved
        "pass": auto_file.exists() and st_a in ("idle", "completed"),
    }

    verdict["pass"] = all(c.get("pass") for c in verdict["checks"].values())
    return verdict


def gate_smoke(call: Callable[..., Any], out_path: Path, turn_timeout_s: float) -> dict:
    """Cheap boot/turn smoke: one trivial turn confirms server+provider+CTE+haiku all
    work before the multi-minute pillar scenarios. NOT a pillar gate."""

    verdict: dict = {"pillar": "smoke"}
    _allow_all(call)
    root = (REPO / "out/live_gate_1031_smoke_root").resolve()
    root.mkdir(parents=True, exist_ok=True)
    wsid = _workspace(call, "smoke", str(root))
    sid = _session(call, wsid, "smoke")
    _post(call, sid, "Reply with exactly the word: ready. Nothing else.")
    verdict["status"] = _wait_turn(call, wsid, sid, turn_timeout_s)
    msgs = _messages(call, sid)
    _dump(out_path, "live_gate_1031_smoke_messages.json", msgs)
    assistant = [m for m in msgs if m.get("role") == "assistant"]
    text = " ".join(
        p.get("text", "") for m in assistant for p in m.get("parts", []) if p.get("type") == "text"
    )
    verdict["assistant_text"] = text[:400]
    verdict["assistant_replied"] = bool(text.strip())
    verdict["pass"] = verdict["status"] in ("idle", "completed") and bool(text.strip())
    return verdict


GATES: dict[str, Callable[..., dict]] = {
    "smoke": gate_smoke,
    "bashprobe": gate_bashprobe,
    "p1": gate_p1,
    "p2": gate_p2,
    "p3": gate_p3,
    "composed": gate_composed,
    "extras": gate_extras,
}

# Gates that also need the base URL (for the SSE collector).
_GATES_NEEDING_BASE = {"p2", "composed"}

# P3 needs the xform_summarize_csv designated-output tool. It is a DECLARED stdio MCP
# server discovered at repo (process-cwd) scope via <repo>/.clio/mcp.yaml, so the BASE
# agent's tool executor mounts it — the legitimate, executor-recognized way to add a
# tool (an in-process monkeypatch is rejected by the runtime custom-tool guard). On
# Windows the codex fence blocks fleet spawns (#974), so P3 runs with the OS fence OFF
# (provenance is minted at the clio boundary, fence-independent); CLIO_ARC_STORE=local
# avoids the CTE file-capacity preflight on this disk-tight box.
_GATE_BOOT_SCRIPTS: dict[str, str] = {}
_GATE_EXTRA_ENV: dict[str, dict[str, str]] = {
    "p3": {"CLIO_SANDBOX_ENABLED": "false", "CLIO_ARC_STORE": "local"},
    "composed": {"CLIO_SANDBOX_ENABLED": "false", "CLIO_ARC_STORE": "local"},
}
# Gates that require an <repo>/.clio/mcp.yaml declaring extra stdio MCP servers,
# written BEFORE boot (the base agent reads it at init) and removed after.
_GATE_MCP_YAML = {"p3", "composed"}


def _write_repo_mcp_yaml() -> Path | None:
    """Declare the xform stdio transform server at repo scope so the base executor
    mounts it. Returns the path to remove afterward (or None if pre-existing)."""
    mcp_yaml = REPO / ".clio" / "mcp.yaml"
    if mcp_yaml.exists():
        return None  # do not clobber a real config
    mcp_yaml.parent.mkdir(parents=True, exist_ok=True)
    script = (REPO / "scripts/live_gate_blueprints/transform_stdio.py").resolve()
    mcp_yaml.write_text(
        "mcp_servers:\n"
        f"  xform: {Path(sys.executable).as_posix()} {script.as_posix()}\n",
        encoding="utf-8",
    )
    return mcp_yaml


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("gate", choices=sorted(GATES))
    ap.add_argument("--port", type=int, default=17931)
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--transport", default="sdk")
    ap.add_argument("--turn-timeout-s", type=float, default=900.0)
    args = ap.parse_args()

    base = f"http://127.0.0.1:{args.port}"
    out_path = (REPO / f"out/live_gate_1031_{args.gate}.json").resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sse_log = f"out/live_gate_1031_{args.gate}_sse.log"

    mcp_yaml_to_remove = _write_repo_mcp_yaml() if args.gate in _GATE_MCP_YAML else None
    proc = _boot(
        args.port, args.model, args.transport, sse_log,
        boot_script=_GATE_BOOT_SCRIPTS.get(args.gate, ""),
        extra_env=_GATE_EXTRA_ENV.get(args.gate),
    )
    verdict: dict = {"gate": args.gate, "model": args.model}
    try:
        call = _client(base)
        if not _wait_health(call):
            verdict["error"] = "server never became healthy"
        else:
            _bind_provider(call, args.model)
            if args.gate in _GATES_NEEDING_BASE:
                verdict.update(GATES[args.gate](call, out_path, args.turn_timeout_s, base))
            else:
                verdict.update(GATES[args.gate](call, out_path, args.turn_timeout_s))
    except Exception as exc:  # noqa: BLE001
        verdict["error"] = f"{type(exc).__name__}: {exc}"
        import traceback

        verdict["traceback"] = traceback.format_exc()
    finally:
        out_path.write_text(json.dumps(verdict, indent=2, default=str), encoding="utf-8")
        print(json.dumps(verdict, indent=2, default=str), flush=True)
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except Exception:
            proc.kill()
        if mcp_yaml_to_remove is not None:
            mcp_yaml_to_remove.unlink(missing_ok=True)
    return 0 if verdict.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
