#!/usr/bin/env python3
"""Thinking-level acceptance experiment harness (#895).

Runs ONE thinking level end to end against a live gact server + the real
claude_code transport, then records the numbers the owner's acceptance
experiment compares across levels (SDK default vs medium vs low):

  * turn wall-clock,
  * output tokens,
  * thinking-vs-content token/char split (from the stream audit + the
    ``analyze_turn_waterfall.py`` drill-down),

and appends one row to a JSONL results file. The orchestrator invokes this once
per ``--level`` (off/low/medium/high, plus the unset SDK default via
``--level default``); the human then fills the ``passed`` verdict per level from
the honest-answer / correct-delegation / no-fabrication check.

Sequencing (matches the owner spec):
  1. boot a server with the level set + stream audit on (unless ``--backend-url``),
  2. **pre-allow permissions via PUT /v1/policies FIRST**,
  3. create workspace + a blueprint session,
  4. POST the LA one-shot question and wait for the turn to finish,
  5. run ``analyze_turn_waterfall.py`` on the capture,
  6. append the results row.

This is experiment PREP — it is not run in CI and does not run itself. Use
``--dry-run`` to validate wiring (env, endpoints, results path) without booting a
server or spending an LM turn.

Usage::

    uv run python scripts/run_thinking_experiment.py --level low \
        --provider claude_code --model haiku \
        --blueprint earthscope-gnss-region \
        --results out/thinking_experiment.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# The LA one-shot: the canonical full-study Los Angeles acceptance prompt (the
# demo benchmark's LA mutation case) — full pipeline, staged CSV, PNG artifact.
# Override with --question for other cases. NOTE: an earlier default here asked a
# US-wide metadata-aggregation question that was never part of the blueprint's
# verified capability surface (no routing row computes tabular metadata stats) —
# it was mislabeled as the LA one-shot and never passed; see #893 campaign log.
DEFAULT_QUESTION = (
    "Explore recent seismic or geodetic activity around the Los Angeles basin. "
    "Resolve the geography without using any San Diego-specific hints, find public "
    "EarthScope/NDP GNSS station or station time-series evidence for that region, "
    "stage a concrete CSV resource if available, analyze the station time series "
    "and uncertainty columns, produce a PNG artifact, and explain data freshness, "
    "coverage, and provenance limitations. Do not use SAC waveform files unless "
    "live catalog evidence makes waveform data necessary; do not force "
    "earthquake/event-catalog analysis unless the user explicitly asks for "
    "events, magnitudes, depths, or epicenters."
)

TERMINAL_STATUSES = {"idle", "finished", "error", "cancelled"}


@dataclass
class ExperimentPlan:
    """Fully-resolved run configuration (assembled from CLI args)."""

    level: str
    provider: str
    model: str
    transport: str
    blueprint: str
    questions: tuple[str, ...]
    workspace_root: Path
    results_path: Path
    audit_path: Path
    waterfall_path: Path
    host: str
    port: int
    backend_url: str
    turn_timeout_s: float
    boot_timeout_s: float

    @property
    def base_url(self) -> str:
        return self.backend_url or f"http://{self.host}:{self.port}"


def server_env(plan: ExperimentPlan) -> dict[str, str]:
    """Environment for the booted server: provider + level + stream audit on.

    The level rides ``CLIO_LM_THINKING_LEVEL`` (env-settable knob, #895) so no
    PUT round-trip is needed; ``default`` means "send nothing — the provider/CLI
    default governs" and is expressed by simply not setting the level.
    """
    env = dict(os.environ)
    env["CLIO_LM_PROVIDER"] = plan.provider
    env["CLIO_LM_MODEL"] = plan.model
    if plan.provider == "claude_code":
        env["CLIO_CLAUDE_CODE_TRANSPORT"] = plan.transport
    if plan.level and plan.level != "default":
        env["CLIO_LM_THINKING_LEVEL"] = plan.level
    else:
        env.pop("CLIO_LM_THINKING_LEVEL", None)
    env["CLIO_STREAM_AUDIT_LOG"] = str(plan.audit_path)
    return env


def _allow_all_policies() -> list[dict[str, Any]]:
    """A blanket workspace-scoped allow so the turn never blocks on HITL."""
    return [{"scope": "workspace", "action": "allow", "tool_name_pattern": "*"}]


class _Client:
    """Tiny JSON HTTP client (requests) with a shared base URL + timeout."""

    def __init__(self, base_url: str, timeout: float = 180.0) -> None:
        # 180s, not 30s: the timeout must cover the longest legitimately-held
        # call — the synchronous PUT provider bind cold-starts the MCP tool
        # servers (>30s on a loaded machine), and /v1/providers/lm/wait is
        # server-held up to its own 120s param. Turn SSE reads use the
        # per-turn timeout, not this one.
        import requests  # noqa: PLC0415 - optional dep, only needed for a live run

        self._requests = requests
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        params: dict | None = None,
        ok_statuses: tuple[int, ...] = (),
    ) -> dict:
        resp = self._requests.request(
            method, f"{self._base}{path}", json=body, params=params, timeout=self._timeout
        )
        if resp.status_code not in ok_statuses:
            resp.raise_for_status()
        return resp.json() if resp.content else {}


def _wait_for_port(client: _Client, timeout_s: float) -> None:
    """Wait until the server responds AT ALL (any status) — provider binding
    happens before full health is achievable on the SDK transport."""
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            client.call("GET", "/v1/health", ok_statuses=(200, 503))
            return
        except Exception as exc:  # noqa: BLE001 - retry until deadline
            last_err = exc
            time.sleep(2.0)
    raise TimeoutError(f"server never responded in {timeout_s}s: {last_err}")


def _wait_for_health(client: _Client, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            client.call("GET", "/v1/health")
            return
        except Exception as exc:  # noqa: BLE001 - retry until the port is up
            last_err = exc
            time.sleep(1.0)
    raise TimeoutError(f"server did not become healthy in {timeout_s}s: {last_err}")


def _ensure_workspace(client: _Client, name: str, root: Path) -> str:
    existing = client.call("GET", "/v1/workspaces").get("workspaces", [])
    for ws in existing:
        if ws.get("name") == name:
            return str(ws["id"])
    created = client.call("POST", "/v1/workspaces", {"name": name, "root_path": str(root)})
    return str(created["id"])


def _create_session(client: _Client, workspace_id: str, blueprint: str) -> str:
    created = client.call(
        "POST", "/v1/sessions", {"title": "thinking-experiment", "workspace_id": workspace_id}
    )
    sid = str(created["id"])
    if blueprint:
        client.call("POST", f"/v1/sessions/{sid}/agent-blueprint", {"blueprint_id": blueprint})
    return sid


def _session_row(client: _Client, workspace_id: str, sid: str) -> dict:
    sessions = client.call("GET", f"/v1/sessions?workspace_id={workspace_id}").get("sessions", [])
    for s in sessions:
        if s.get("id") == sid:
            return s
    return {}


def _run_turn(client: _Client, plan: ExperimentPlan, workspace_id: str, sid: str) -> float:
    """POST each question in sequence, polling every turn to terminal.

    Owner direction (2026-07-12): the acceptance probe is a SHORT real
    multi-turn case, not the full LA showcase — each level's data point
    should cost ~10 minutes, not 35-40.
    """
    started = time.monotonic()
    expected_messages = 0
    for question in plan.questions:
        client.call("POST", f"/v1/sessions/{sid}/messages", {"text": question})
        expected_messages += 2
        turn_started = time.monotonic()
        deadline = turn_started + plan.turn_timeout_s
        while time.monotonic() < deadline:
            row = _session_row(client, workspace_id, sid)
            if (
                row.get("status") in TERMINAL_STATUSES
                and int(row.get("message_count") or 0) >= expected_messages
            ):
                break
            time.sleep(2.0)
        else:
            raise TimeoutError(f"turn did not finish within {plan.turn_timeout_s}s")
    return time.monotonic() - started


def _run_waterfall(plan: ExperimentPlan, session_id: str) -> dict:
    """Invoke analyze_turn_waterfall.py and return its parsed JSON summary."""
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "analyze_turn_waterfall.py"),
        "--audit",
        str(plan.audit_path),
        "--session-id",
        session_id,
        "--json-out",
        str(plan.waterfall_path),
    ]
    subprocess.run(cmd, check=True, cwd=str(REPO_ROOT))
    if plan.waterfall_path.exists():
        return json.loads(plan.waterfall_path.read_text(encoding="utf-8"))
    return {}


def _thinking_share(plan: ExperimentPlan) -> dict[str, int]:
    """Sum claude_code_sdk thinking vs content chars from the stream audit."""
    thinking = content = 0
    if not plan.audit_path.exists():
        return {"thinking_chars": 0, "content_chars": 0}
    for line in plan.audit_path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("stage") == "provider.raw_event" and row.get("provider") == "claude_code_sdk":
            thinking += int(row.get("thinking_len") or 0)
            content += int(row.get("text_len") or 0)
    return {"thinking_chars": thinking, "content_chars": content}


def _sum_output_tokens(waterfall: dict) -> int | None:
    """Sum per-call ``output_tokens`` from the waterfall report (None if absent)."""
    calls = waterfall.get("calls") or []
    totals = [int(c["output_tokens"]) for c in calls if c.get("output_tokens") is not None]
    return sum(totals) if totals else None


def append_result(plan: ExperimentPlan, row: dict[str, Any]) -> None:
    """Append one JSONL results row (the orchestrator aggregates across levels)."""
    plan.results_path.parent.mkdir(parents=True, exist_ok=True)
    with plan.results_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True))
        f.write("\n")


def build_plan(args: argparse.Namespace) -> ExperimentPlan:
    out_dir = Path(args.out_dir).resolve()
    # Fresh per-run workspace by default: a shared root lets one run's staged
    # artifacts poison the next (observed 2026-07-12: stale/BOM-damaged catalog
    # CSVs from a failed run drove a later run to a false zero-station result).
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    workspace_root = (
        Path(args.workspace_root).resolve()
        if args.workspace_root
        else out_dir / "workspace" / stamp
    )
    return ExperimentPlan(
        level=args.level,
        provider=args.provider,
        model=args.model,
        transport=args.transport,
        blueprint=args.blueprint,
        questions=tuple(args.question) if args.question else (DEFAULT_QUESTION,),
        workspace_root=workspace_root,
        results_path=Path(args.results).resolve(),
        audit_path=(out_dir / f"stream_audit_{args.level}.jsonl").resolve(),
        waterfall_path=(out_dir / f"waterfall_{args.level}.json").resolve(),
        host=args.host,
        port=args.port,
        backend_url=args.backend_url,
        turn_timeout_s=args.turn_timeout_s,
        boot_timeout_s=args.boot_timeout_s,
    )


def run(plan: ExperimentPlan) -> dict[str, Any]:
    """Execute the full experiment for one level; return the results row."""
    plan.workspace_root.mkdir(parents=True, exist_ok=True)
    plan.audit_path.parent.mkdir(parents=True, exist_ok=True)
    plan.audit_path.write_text("", encoding="utf-8")  # fresh capture

    proc: subprocess.Popen | None = None
    if not plan.backend_url:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from clio_agent.gact.app import run_server; "
                f"run_server(host='{plan.host}', port={plan.port})",
            ],
            env=server_env(plan),
            cwd=str(REPO_ROOT),
        )
    try:
        client = _Client(plan.base_url)
        # Bind the provider over the API instead of relying on the env-config
        # path: the boot-time health probe treats ``claude-code://sdk`` as an
        # HTTP endpoint and reports lm_provider unavailable forever (filed as a
        # health-probe bug), whereas PUT /v1/providers/lm handles the SDK
        # transport correctly. The thinking level still comes from the server
        # process env (the LM factory resolves CLIO_LM_THINKING_LEVEL at build).
        _wait_for_port(client, plan.boot_timeout_s)
        client.call(
            "PUT",
            "/v1/providers/lm",
            {"provider": plan.provider, "api_base": "", "model": plan.model},
        )
        client.call("GET", "/v1/providers/lm/wait", params={"timeout": 120})
        # Health is ADVISORY after the provider bind: /v1/health's lm probe
        # HTTP-probes claude-code://sdk and can report unavailable while turns
        # run fine (filed as a health-probe bug). lm/wait state=ready is the
        # real gate; log health's verdict without gating on it.
        verdict = client.call("GET", "/v1/health", ok_statuses=(200, 503))
        print(f"health (advisory): healthy={verdict.get('healthy')}", flush=True)
        # (2) Pre-allow permissions FIRST — before any session or turn.
        client.call("PUT", "/v1/policies", {"policies": _allow_all_policies()})
        # Name derived from the (per-run) root: _ensure_workspace matches by
        # name, so a fixed name would silently reuse the FIRST run's persisted
        # workspace root and defeat the fresh-workspace isolation above.
        workspace_id = _ensure_workspace(
            client, f"thinking-experiment-{plan.workspace_root.name}", plan.workspace_root
        )
        sid = _create_session(client, workspace_id, plan.blueprint)
        wall_s = _run_turn(client, plan, workspace_id, sid)
        waterfall = _run_waterfall(plan, sid)
        share = _thinking_share(plan)
        row = {
            "level": plan.level,
            "provider": plan.provider,
            "model": plan.model,
            "transport": plan.transport,
            "session_id": sid,
            "wall_clock_s": round(wall_s, 2),
            "thinking_chars": share["thinking_chars"],
            "content_chars": share["content_chars"],
            "output_tokens": _sum_output_tokens(waterfall),
            "waterfall": plan.waterfall_path.name,
            "passed": None,  # human fills the honest-answer/delegation/no-fab verdict
        }
        append_result(plan, row)
        return row
    finally:
        if proc is not None:
            _terminate_server_tree(proc)


def _terminate_server_tree(proc: subprocess.Popen) -> None:
    """Tree-kill the spawned gact server and every descendant (#900 harness discipline).

    The booted server (claude_code SDK transport) fans out into MCP stdio children +
    pooled ``claude`` CLI process(es); a plain ``proc.terminate()`` on the parent orphans
    them (Windows never reaps the tree by terminating the parent). Reuse the audited
    :func:`clio_agent.serve._terminate_tree` (psutil-recursive + POSIX process-group).
    """
    from clio_agent.serve import _terminate_tree  # noqa: PLC0415

    _terminate_tree(proc.pid, record_create_time=None, trusted=True)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--level",
        required=True,
        choices=["default", "off", "low", "medium", "high"],
        help="thinking level for this run ('default' sends nothing → SDK/CLI default)",
    )
    p.add_argument("--provider", default="claude_code")
    p.add_argument("--model", default="haiku")
    p.add_argument("--transport", default="sdk")
    p.add_argument("--blueprint", default="earthscope-gnss-region")
    p.add_argument(
        "--question",
        action="append",
        default=None,
        help="Repeatable; each is one TURN in sequence. Default: the LA one-shot.",
    )
    p.add_argument("--workspace-root", default="")
    p.add_argument("--out-dir", default=str(REPO_ROOT / "out" / "thinking_experiment"))
    p.add_argument(
        "--results", default=str(REPO_ROOT / "out" / "thinking_experiment" / "results.jsonl")
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8109)
    p.add_argument(
        "--backend-url",
        default="",
        help="use an already-running server instead of booting one (skips level env)",
    )
    p.add_argument("--turn-timeout-s", type=float, default=1800.0)
    p.add_argument("--boot-timeout-s", type=float, default=120.0)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved plan + server env and exit (no server, no LM turn)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    plan = build_plan(args)
    if args.dry_run:
        print("PLAN:")
        for key, value in plan.__dict__.items():
            print(f"  {key} = {value}")
        env = server_env(plan)
        print("SERVER ENV (thinking-relevant):")
        for key in (
            "CLIO_LM_PROVIDER",
            "CLIO_LM_MODEL",
            "CLIO_CLAUDE_CODE_TRANSPORT",
            "CLIO_LM_THINKING_LEVEL",
            "CLIO_STREAM_AUDIT_LOG",
        ):
            print(f"  {key} = {env.get(key, '<unset>')}")
        print(f"base_url = {plan.base_url}")
        return 0
    row = run(plan)
    print(json.dumps(row, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
