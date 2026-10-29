"""MCP fleet memory attribution + release-gating budget check (#930 S1/#931).

Samples a running gact server's WHOLE process tree per-process (role-
classified), drives the standard acceptance load (N concurrent sessions on
one agent, pinned per session), and reports IDLE / PEAK / FINAL attribution. The whole
run repeats ``--runs`` times (default 3, each on a freshly booted server) and
the verdict uses the MEDIAN peak and MEDIAN final, printing every run and the
spread. With ``--assert-budget`` the gate FAILS if the medians exceed the
recorded budget in ``scripts/mcp_mem_budget.json`` -- the budget only ratchets
DOWN (record new, lower numbers after an optimization lands; never raise them
to make a regression pass).

Usage (the #921/#929 acceptance shape — 3 concurrent claude-haiku sessions):

    uv run python scripts/mcp_mem_attribution.py \
        --agent clio/clio-data \
        --workspace <dir with sensor_readings.csv> \
        --sessions 3 --settle-s 180 --runs 3 --assert-budget

The agent comes from the default marketplace, which the fresh gate config
installs on first use; ``--marketplace PATH`` also installs (and trusts) a local
marketplace before the load, for an agent that is not shipped.

The server is booted as a child of THIS process (claude_code/haiku + the real
CTE substrate per the accepted gate config) and
torn down on exit. Run it as ONE task: server and load share this process
tree so external task eviction cannot split them.

Background-children scenario (#955): pass ``--children-agent <agent ref with
defined alpha and beta subagents>`` (e.g. a canary agent installed with
``--marketplace``). Session 0 then drives that agent and fans out 2 BACKGROUND
children (alpha+beta) via the async spawn
tools instead of the clio-data load; the other sessions keep the standard
load. The children are REAL sessions — ``resident_ledgers`` caps their
transcripts and the #933 workspace-fleet reaper cleans their fleets — so the
gate exercises the bound with real child sessions resident.

HONEST CAVEAT (the load is a SWAP, not an add): session 0 runs the canary agent
INSTEAD of the clio-data agent, so this scenario has 2 MCP-fleet data
sessions (1-2) vs the baseline's 3, and the canary children carry NO MCP
servers (they add transcript + provider-CLI cost, not a fleet). The
children peak is therefore a LIGHTER fleet load than the baseline and is NOT a
peak-vs-baseline delta measurement — its value is (a) the dedicated ``children``
budget block ratchet (its own recorded cold-max numbers) and (b) the
non-vacuity guard below (``_assert_children_spawned``), which fails the gate if
session 0 did not actually spawn its 2 children (so a silent no-spawn cannot be
recorded as a passing children run). Under ``--assert-budget`` this scenario is
checked against the ``children`` budget block in ``mcp_mem_budget.json``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import psutil
import requests

REPO = Path(__file__).resolve().parents[1]
BUDGET_PATH = Path(__file__).resolve().parent / "mcp_mem_budget.json"
# Tolerance for machine noise on a live box; the recorded budget itself only
# ratchets down. PEAK is a 5s-interval lower bound (sub-interval spikes are
# invisible) — fine for a ratchet, do not read it as a true max. Role
# classification is report-cosmetic: totals drive the verdict, so a
# misclassified row can never change the gate outcome. The budget INCLUDES
# provider-CLI processes (claude SDK CLI) — they are fleet-resident memory.
BUDGET_TOLERANCE = 1.05
# The verdict compares the MEDIAN of N complete runs, never one run: run-to-run
# noise on one commit (final 0.97 vs 0.95 GB) spans the whole tolerance band.
DEFAULT_RUNS = 3
MIN_ASSERT_RUNS = 3

PROMPT_SHAPES = [
    (
        "A colleague handed me the dataset at {data} and wants to train a model "
        "on it next week. Can you take a look and tell me whether it's ready, "
        "and what problems you see?"
    ),
    (
        "Before we archive {data}, can you review it like a collaborator would "
        "and flag anything that would bite us in a downstream analysis?"
    ),
    ("Is {data} trustworthy enough to build a report on? Walk me through its condition."),
]

# The children scenario's fan-out prompt: drive the canary orchestrator's
# "parallel consult" exercise, which spawns alpha AND beta as 2 background
# children in one spawn_agents_parallel call and waits on both. This is the
# minimal correct vehicle for "one session fanning out 2 background children".
CHILDREN_PROMPT = (
    "Run the parallel-consult exercise: consult alpha and beta in parallel. "
    "Spawn BOTH as background children in a SINGLE spawn_agents_parallel call, "
    "wait on both task_ids with timeout_s=180, then write a final answer that "
    "quotes alpha's canary token and beta's canary token verbatim."
)


def classify(name: str, cmdline: str) -> str:
    """Role-classify one process of the server tree."""

    lowered = cmdline.lower()
    if "clio-kit" in lowered or "mcp-environments" in lowered or "mcp-server" in lowered:
        for server in ("pandas", "parquet", "hdf5", "plot", "geo", "ndp", "seismic"):
            if server in lowered:
                return f"mcp:{server}"
        return "mcp:other"
    if "claude" in lowered and ("node" in name.lower() or "claude" in name.lower()):
        return "claude-sdk-cli"
    if name.lower().startswith("uv"):
        return "launcher"  # the resident launcher parent, even when its cmdline names the server
    if "clio-agent-gact" in lowered or "uvicorn" in lowered:
        return "server-main"
    if "clio_run" in lowered or "cte" in lowered:
        return "cte-daemon"
    if name.lower().startswith("conhost"):
        return "conhost"
    return f"other:{name}"


class TreeSampler:
    """Per-process RSS snapshots of a process tree, every ``interval`` seconds.

    ``extra_pids`` covers fleet members OUTSIDE the tree — the clio-core
    daemon is connect-or-spawn, so a pre-existing ``clio_run`` has a foreign
    parent and would otherwise be silently excluded from the budget.
    """

    def __init__(
        self, root_pid: int, interval: float = 5.0, extra_pids: tuple[int, ...] = ()
    ) -> None:
        self.root_pid = root_pid
        self.interval = interval
        self.extra_pids = extra_pids
        self.snapshots: list[tuple[float, list[dict], int]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive() or self._thread.ident is not None:
            self._thread.join(timeout=self.interval + 5)

    def _run(self) -> None:
        try:
            root = psutil.Process(self.root_pid)
        except psutil.Error:
            return
        while not self._stop.is_set():
            rows: list[dict] = []
            skipped = 0
            procs: list[psutil.Process] = [root]
            try:
                procs += root.children(recursive=True)
            except psutil.Error:
                pass
            for pid in self.extra_pids:
                try:
                    procs.append(psutil.Process(pid))
                except psutil.Error:
                    continue  # a vanished external daemon just drops out of totals
            for proc in procs:
                try:
                    with proc.oneshot():
                        role = classify(proc.name(), " ".join(proc.cmdline()[:6]))
                        if proc.pid in self.extra_pids:
                            role = "cte-daemon(external)"
                        rows.append(
                            {
                                "pid": proc.pid,
                                "name": proc.name(),
                                "role": role,
                                "rss_mb": proc.memory_info().rss / 1e6,
                            }
                        )
                except psutil.Error:
                    skipped += 1  # counted, never silent (report prints it)
            self.snapshots.append((time.time(), rows, skipped))
            self._stop.wait(self.interval)

    def total_gb(self, rows: list[dict]) -> float:
        return sum(r["rss_mb"] for r in rows) / 1000

    def peak(self) -> tuple[float, list[dict], int] | None:
        return max(self.snapshots, key=lambda s: self.total_gb(s[1])) if self.snapshots else None

    def final(self) -> tuple[float, list[dict], int] | None:
        return self.snapshots[-1] if self.snapshots else None


def report(tag: str, rows: list[dict], skipped: int = 0) -> None:
    total = sum(r["rss_mb"] for r in rows)
    by_role: dict[str, float] = {}
    for r in rows:
        by_role[r["role"]] = by_role.get(r["role"], 0.0) + r["rss_mb"]
    note = f" (skipped {skipped} unreadable)" if skipped else ""
    print(f"\n===== {tag}: total {total / 1000:.2f} GB across {len(rows)} processes{note} =====")
    for role, mb in sorted(by_role.items(), key=lambda kv: -kv[1]):
        count = sum(1 for r in rows if r["role"] == role)
        print(f"  {role:<22} {mb / 1000:5.2f} GB  ({count} proc)")


def check_budget(peak_gb: float, final_gb: float, budget: dict) -> tuple[bool, str]:
    """Pure budget verdict — unit-tested; the ratchet contract in one place.

    A malformed budget is a typed FAIL (never a vacuous pass), and empty /
    non-positive measurements are rejected (a dead-server 0.0 must not pass).
    """

    try:
        peak_budget = float(budget["peak_gb"])
        final_budget = float(budget["final_gb"])
    except (KeyError, TypeError, ValueError) as exc:
        return False, f"malformed budget file: {exc!r}"
    if peak_gb <= 0 or final_gb <= 0:
        return False, f"non-positive measurement (peak={peak_gb}, final={final_gb})"
    peak_cap = peak_budget * BUDGET_TOLERANCE
    final_cap = final_budget * BUDGET_TOLERANCE
    detail = (
        f"peak {peak_gb:.2f} <= {peak_cap:.2f}? {peak_gb <= peak_cap}   "
        f"final {final_gb:.2f} <= {final_cap:.2f}? {final_gb <= final_cap}"
    )
    return peak_gb <= peak_cap and final_gb <= final_cap, detail


def prepare_session(base: str, agent: str, idx: int, workspace_id: str | None = None) -> str:
    """Create one session and pin ``agent`` to it before any acceptance turn starts."""

    with requests.Session() as session:
        session_input = {"title": f"membudget {idx}"}
        if workspace_id:
            session_input["workspace_id"] = workspace_id
        response = session.post(f"{base}/v1/sessions", json=session_input, timeout=30)
        response.raise_for_status()
        created = response.json()
        sid = created["id"]
        if not isinstance(sid, str) or not sid:
            raise ValueError(f"session {idx} creation returned an invalid id")
        if workspace_id and created.get("workspace_id") != workspace_id:
            raise ValueError(f"session {idx} was not created in its measurement workspace")
        session.patch(
            f"{base}/v1/sessions/{sid}/agent", json={"ref": agent}, timeout=120
        ).raise_for_status()
    return sid


def drive_load(
    base: str,
    plan: list[tuple[str, str]],
    out: dict[int, str],
    sids: dict[int, str],
    workspace_id: str | None = None,
    model: dict[str, str] | None = None,
) -> bool:
    """Prepare every session's agent, then drive the complete concurrent acceptance load."""

    for idx, (agent, _prompt) in enumerate(plan):
        try:
            sids[idx] = (
                prepare_session(base, agent, idx, workspace_id=workspace_id)
                if workspace_id
                else prepare_session(base, agent, idx)
            )
        except (requests.RequestException, KeyError, ValueError) as exc:
            out[idx] = f"setup error: {exc!r}"
            print(f"GATE: FAIL (session {idx} setup failed: {exc!r})")
            return False
    threads = [
        threading.Thread(
            target=drive_session,
            args=(base, sids[idx], prompt, out, idx, model)
            if model
            else (base, sids[idx], prompt, out, idx),
            daemon=True,
        )
        for idx, (_agent, prompt) in enumerate(plan)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1500)
    return True


def drive_session(
    base: str,
    sid: str,
    prompt: str,
    out: dict[int, str],
    idx: int,
    model: dict[str, str] | None = None,
) -> None:
    """Submit a turn for an already configured session and wait for its outcome."""

    s = requests.Session()
    try:
        submission: dict[str, object] = {
            "text": prompt,
            "behavior": {"confirmation_policy": "bypass"},
        }
        if model:
            submission["model"] = model
        deadline = time.time() + 300
        while True:
            r = s.post(f"{base}/v1/sessions/{sid}/messages", json=submission, timeout=60)
            if r.status_code != 503 or time.time() > deadline:
                break
            time.sleep(10)
        r.raise_for_status()
        acceptance = r.json()
        message_id = acceptance.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("Acceptance did not identify the submitted turn")
        if model and acceptance.get("effective_model") != {**model, "variant": ""}:
            effective = acceptance.get("effective_model") or {}
            if any(effective.get(key) != value for key, value in model.items()):
                raise ValueError(f"Accepted model {effective!r} differs from requested {model!r}")
        deadline = time.time() + 1200
        while time.time() < deadline:
            try:
                status = s.get(f"{base}/v1/sessions/{sid}", timeout=30).json().get("status", "")
            except Exception:  # noqa: BLE001 - server sheds keepalives under load; retry
                time.sleep(10)
                continue
            if status == "idle":
                tasks_response = s.get(f"{base}/v1/sessions/{sid}/agent-tasks", timeout=30)
                tasks_response.raise_for_status()
                tasks = tasks_response.json().get("tasks", [])
                failed = [
                    row
                    for row in tasks
                    if row.get("status") in {"failed", "cancelled", "interrupted"}
                ]
                if failed:
                    out[idx] = "failed_tasks: " + ", ".join(
                        str(row.get("error_reason") or row.get("status")) for row in failed
                    )
                    return
                if any(row.get("status") != "completed" for row in tasks):
                    time.sleep(10)
                    continue
                messages_response = s.get(f"{base}/v1/sessions/{sid}/messages", timeout=30)
                messages_response.raise_for_status()
                messages = messages_response.json().get("messages", [])
                replies = [
                    row
                    for row in messages
                    if row.get("role") == "assistant" and row.get("turn_id") == message_id
                ]
                if any(
                    row.get("error_info") or row.get("stop_reason") in {"error", "cancelled"}
                    for row in replies
                ):
                    out[idx] = "failed_turn: the accepted turn ended with an error"
                    return
                if not any(
                    part.get("type") == "text" and str(part.get("text") or "").strip()
                    for row in replies
                    for part in row.get("parts", [])
                ):
                    time.sleep(10)
                    continue
            if status in {"idle", "error"}:
                out[idx] = status
                return
            time.sleep(10)
        out[idx] = "timeout"
    except Exception as exc:  # noqa: BLE001 - a failed session is a failed run, reported below
        out[idx] = f"error: {exc!r}"
    finally:
        s.close()


def _assert_children_spawned(base: str, sid: str | None) -> tuple[bool, str]:
    """Non-vacuity guard for the children scenario (adversarial-review finding [8]).

    Session 0 MUST have actually spawned its 2 background children (alpha + beta). A run
    where the model answered WITHOUT spawning them (or the spawns failed) is a strictly
    LIGHTER no-children load; recording it as a passing ``children`` budget would let a
    silent no-spawn masquerade as proof. We read the authoritative task projection
    (``GET /v1/sessions/{sid}/agent-tasks``) and require >= 2 child records.
    """
    if not sid:
        return False, "children guard: session 0 sid was never recorded (no spawn possible)"
    try:
        r = requests.get(f"{base}/v1/sessions/{sid}/agent-tasks", timeout=30)
        r.raise_for_status()
        tasks = r.json().get("tasks", [])
    except Exception as exc:  # noqa: BLE001 - a guard that cannot read is a guard that fails
        return False, f"children guard: could not read agent-tasks for session 0 ({sid}): {exc!r}"
    statuses = [t.get("status") for t in tasks]
    ok = len(tasks) >= 2
    return ok, (
        f"children guard: session 0 spawned {len(tasks)} child task(s) "
        f"(need >= 2: alpha+beta); statuses={statuses}"
    )


def _service_alive(base: str) -> bool:
    try:
        requests.get(f"{base}/v1/capabilities", timeout=5).raise_for_status()
        return True
    except Exception:  # noqa: BLE001 - not answering = not alive for gate purposes
        return False


def _port_listener_pid(port: int) -> int | None:
    """The pid listening on the port — the REAL server root (the uv wrapper
    can die independently of the python process it spawned)."""

    for conn in psutil.net_connections(kind="tcp"):
        if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
            return conn.pid
    return None


def _external_cte_pids(environ: Mapping[str, str]) -> tuple[int, ...]:
    """Count the configured private daemon without charging unrelated CLIO apps."""
    if configured_port := environ.get("CLIO_CORE_PORT"):
        port = int(configured_port)
        pid = _port_listener_pid(port)
        if pid is None:
            raise RuntimeError(f"Configured private CTE has no listener on port {port}")
        if not psutil.Process(pid).name().lower().startswith("clio_run"):
            raise RuntimeError(f"Configured private CTE port {port} belongs to another process")
        return (pid,)
    return tuple(
        proc.pid
        for proc in psutil.process_iter(["name"])
        if str(proc.info.get("name") or "").lower().startswith("clio_run")
    )


@dataclass(frozen=True)
class RunMeasurement:
    """One complete gate run: boot -> load -> settle -> teardown."""

    peak_gb: float
    final_gb: float


@dataclass(frozen=True)
class RunSummary:
    """The median the budget is judged on, plus the spread it hides."""

    runs: tuple[RunMeasurement, ...]
    peak_median_gb: float
    final_median_gb: float
    peak_spread_gb: tuple[float, float]
    final_spread_gb: tuple[float, float]

    def describe(self) -> str:
        """Per-run values, medians and spreads, for the gate log."""

        per_run = "  ".join(
            f"run{i + 1}: peak {r.peak_gb:.2f} final {r.final_gb:.2f}"
            for i, r in enumerate(self.runs)
        )
        return (
            f"{per_run}\n"
            f"median of {len(self.runs)}: peak {self.peak_median_gb:.2f} GB "
            f"(spread {self.peak_spread_gb[0]:.2f}-{self.peak_spread_gb[1]:.2f}, "
            f"{self.peak_spread_gb[1] - self.peak_spread_gb[0]:.2f})   "
            f"final {self.final_median_gb:.2f} GB "
            f"(spread {self.final_spread_gb[0]:.2f}-{self.final_spread_gb[1]:.2f}, "
            f"{self.final_spread_gb[1] - self.final_spread_gb[0]:.2f})"
        )


def summarize_runs(runs: list[RunMeasurement]) -> RunSummary:
    """Median + spread of N complete runs (pure; unit-tested).

    The budget is compared against the MEDIAN, never a single run: two clean runs of
    one commit measured final 0.97 and 0.95 GB, a spread larger than the 5% tolerance
    band itself, so one run can land either side of the cap by noise alone. The median
    of an odd N is an actual measured run; the spread is reported so noise stays
    visible instead of being averaged away.

    Raises:
        ValueError: With no runs (an empty gate proves nothing).
    """

    if not runs:
        raise ValueError("no runs to summarize")
    peaks = [r.peak_gb for r in runs]
    finals = [r.final_gb for r in runs]
    return RunSummary(
        runs=tuple(runs),
        peak_median_gb=statistics.median(peaks),
        final_median_gb=statistics.median(finals),
        peak_spread_gb=(min(peaks), max(peaks)),
        final_spread_gb=(min(finals), max(finals)),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent", default="clio/clio-data", help="agent ref each session is pinned to"
    )
    parser.add_argument(
        "--marketplace",
        type=Path,
        default=None,
        help="local marketplace to install and trust before the load (for a non-shipped agent)",
    )
    parser.add_argument(
        "--children-agent",
        default=None,
        help=(
            "Agent ref with defined alpha and beta subagents. When set, session 0 fans "
            "out 2 BACKGROUND children instead of the data load; --assert-budget then "
            "checks the 'children' block of mcp_mem_budget.json."
        ),
    )
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--data", default="sensor_readings.csv")
    parser.add_argument("--sessions", type=int, default=3)
    parser.add_argument("--port", type=int, default=8195)
    parser.add_argument("--model", default="haiku")
    parser.add_argument("--provider", default="claude_code")
    parser.add_argument(
        "--xdg",
        type=Path,
        default=None,
        help=(
            "XDG_CONFIG_HOME for the server. Defaults to a FRESH stamped temp dir per run: "
            "the config-FILE layer outranks env pins (conf precedence file>env), so "
            "an inherited real config could silently swap substrate/provider."
        ),
    )
    parser.add_argument("--settle-s", type=int, default=60, help="post-load settle before FINAL")
    parser.add_argument(
        "--runs",
        type=int,
        default=DEFAULT_RUNS,
        help=(
            "Complete runs (fresh server boot each) whose MEDIAN peak/final is compared "
            f"to the budget (default {DEFAULT_RUNS}; --assert-budget needs >= {MIN_ASSERT_RUNS})."
        ),
    )
    parser.add_argument("--assert-budget", action="store_true")
    parser.add_argument(
        "--private-core-root",
        type=Path,
        help="start and retire a fresh private Core daemon and store for every run",
    )
    parser.add_argument("--measurement-out", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.sessions < 1:
        print("FAIL: --sessions must be >= 1 (a zero-session run proves nothing)")
        return 2
    if args.runs < 1:
        print("FAIL: --runs must be >= 1")
        return 2
    if args.assert_budget and args.sessions != 3:
        print("FAIL: --assert-budget is defined for the recorded load (--sessions 3)")
        return 2
    if args.assert_budget and args.settle_s < 180:
        # The recorded final_gb is post-reap: the workspace-fleet reaper's
        # default 120s idle TTL must elapse before FINAL or fleets are still
        # resident and an honest run fails spuriously (#935).
        print("FAIL: --assert-budget requires --settle-s >= 180 (the recorded load's settle)")
        return 2
    if args.assert_budget and args.runs < MIN_ASSERT_RUNS:
        # One run's noise spans the tolerance band (final 0.97 vs 0.95 on one commit).
        print(f"FAIL: --assert-budget compares a median; it needs --runs >= {MIN_ASSERT_RUNS}")
        return 2

    if args.measurement_out is not None:
        if args.assert_budget or args.runs != 1 or args.private_core_root is not None:
            print("FAIL: an isolated sample cannot assert or recursively aggregate a budget")
            return 2
        sample = _run_once(args)
        if isinstance(sample, int):
            return sample
        args.measurement_out.write_text(
            json.dumps({"peak_gb": sample.peak_gb, "final_gb": sample.final_gb}),
            encoding="utf-8",
        )
        return 0

    measurements: list[RunMeasurement] = []
    for run_index in range(args.runs):
        print(f"\n########## run {run_index + 1}/{args.runs} ##########")
        outcome = _run_once(args)
        if isinstance(outcome, int):
            print(f"GATE: FAIL (run {run_index + 1}/{args.runs} did not produce a measurement)")
            return outcome
        print(
            f"run {run_index + 1}: peak {outcome.peak_gb:.2f} GB   final {outcome.final_gb:.2f} GB"
        )
        measurements.append(outcome)

    summary = summarize_runs(measurements)
    print(f"\n{summary.describe()}")

    if args.assert_budget:
        budget_root = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
        if args.children_agent is not None:
            budget = budget_root.get("children")
            budget_label = "children"
            if not isinstance(budget, dict):
                print(
                    "FAIL: --children-agent asserted but no 'children' budget block "
                    f"is recorded in {BUDGET_PATH.name}"
                )
                return 2
        else:
            budget = budget_root
            budget_label = "baseline"
        ok, detail = check_budget(summary.peak_median_gb, summary.final_median_gb, budget)
        print(f"BUDGET ({BUDGET_PATH.name}:{budget_label}, median of {args.runs}): {detail}")
        if not ok:
            print(
                "GATE: FAIL — fleet memory regressed past the recorded budget. "
                "Fix the regression; never raise the budget."
            )
            return 1
        if (
            summary.peak_median_gb < budget["peak_gb"] * 0.9
            or summary.final_median_gb < budget["final_gb"] * 0.9
        ):
            print(
                "NOTE (ratchet down): the median is well under budget — record the "
                f"median of >=3 runs in {BUDGET_PATH.name} (never a single noise "
                "trough: the 5% tolerance must still cover honest run-to-run "
                "variance under the new number)."
            )
    print("GATE: PASS")
    return 0


def _run_with_fresh_core(args: argparse.Namespace) -> RunMeasurement | int:
    """Measure in a fresh process/Core store; the parent alone judges the median."""
    root = args.private_core_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="memory-sample-", dir=root) as temporary:
        sample_root = Path(temporary).resolve()
        if not sample_root.is_relative_to(root):
            raise RuntimeError("Private memory sample escaped its owned root")
        output = sample_root / "measurement.json"
        command = [
            sys.executable,
            "-B",
            str(REPO / "scripts/live_verification/run_with_private_cte.py"),
            "--runtime-root",
            str(sample_root),
            str(Path(__file__).resolve()),
            "--runs",
            "1",
            "--measurement-out",
            str(output),
        ]
        for name in (
            "agent",
            "workspace",
            "data",
            "sessions",
            "port",
            "model",
            "provider",
            "settle_s",
        ):
            command.extend(["--" + name.replace("_", "-"), str(getattr(args, name))])
        for name in ("marketplace", "children_agent", "xdg"):
            value = getattr(args, name, None)
            if value is not None:
                command.extend(["--" + name.replace("_", "-"), str(value)])
        result = subprocess.run(command, cwd=REPO, check=False)
        if result.returncode:
            return result.returncode
        if not output.is_file():
            print("FAIL: isolated run returned no measurement")
            return 2
        recorded = json.loads(output.read_text(encoding="utf-8"))
        values = [recorded.get("peak_gb"), recorded.get("final_gb")]
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in values
        ):
            print("FAIL: isolated run returned an invalid measurement")
            return 2
        return RunMeasurement(peak_gb=values[0], final_gb=values[1])


def _gate_environment(args: argparse.Namespace, xdg: Path) -> dict[str, str]:
    """Keep every CLIO role private on Windows as well as XDG-based hosts."""
    instance = xdg / "clio-home"
    return {
        **os.environ,
        "CLIO_AGENT_HOME": str(instance),
        **{
            f"CLIO_AGENT_{role.upper()}_DIR": str(instance / role)
            for role in ("config", "data", "cache")
        },
        "CLIO_LM_PROVIDER": args.provider,
        "CLIO_LM_MODEL": args.model,
        "CLIO_ARC_STORE": "cte",
        "CLIO_ALLOWED_ROOTS": str(args.workspace),
        "CLIO_SEMANTIC_TRACE_BACKEND": "none",
        "XDG_CONFIG_HOME": str(xdg),
    }


def _stop_private_services(env: Mapping[str, str]) -> None:
    """Retire controllers belonging to this run's freshly generated private home."""
    from clio_agent.gact.infrastructure import node_service

    root = (Path(env["CLIO_AGENT_DATA_DIR"]) / "services").resolve()
    for marker in root.glob("*/*/owner.json"):
        service = marker.parent.resolve()
        if not service.is_relative_to(root):
            raise RuntimeError("Private service escaped its owned data directory")
        owner = json.loads(marker.read_text(encoding="utf-8"))["owner"]
        receipt = json.loads((service / "receipt.json").read_text(encoding="utf-8"))
        outcome = node_service.control(
            {
                "action": "stop",
                "root": str(service),
                "owner": owner,
                "require_operation_id": receipt.get("operation_id"),
            }
        )
        if outcome.get("running") or outcome.get("phase") != "stopped":
            raise RuntimeError(f"Private service did not settle: {service}")


def _run_once(args: argparse.Namespace) -> RunMeasurement | int:
    """One complete gate run: boot, verify substrate, drive the load, settle, measure.

    Returns:
        The measurement, or a non-zero exit code when this run cannot produce an
        honest one (boot failure, degraded substrate, dead server, sessions not idle,
        or a children run that never spawned its children).
    """

    if getattr(args, "private_core_root", None) is not None:
        return _run_with_fresh_core(args)
    xdg = args.xdg or Path(tempfile.mkdtemp(prefix="clio-mem-gate-xdg-"))
    # The gate config is fresh: the default marketplace installs into it on first
    # use, and a pinned session's plugin servers form its fleet. --marketplace
    # installs a local one too (after boot, below).
    env = _gate_environment(args, xdg)
    base = f"http://127.0.0.1:{args.port}"
    # A pre-existing clio-core daemon (connect-or-spawn) is OUTSIDE the server
    # tree — sample it explicitly or the substrate cost silently drops out.
    external_cte = _external_cte_pids(env)
    if external_cte:
        print(f"note: pre-existing clio-core daemon(s) {external_cte} sampled as external")

    log = open(args.workspace / "mem-gate-server.log", "w", encoding="utf-8")
    server = subprocess.Popen(
        ["uv", "run", "--no-sync", "clio-agent-gact", "--port", str(args.port)],
        cwd=str(REPO),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    listener_pid: int | None = None
    sampler: TreeSampler | None = None
    try:
        deadline = time.time() + 480
        while time.time() < deadline:
            if server.poll() is not None and not _service_alive(base):
                print(f"FAIL: server exited during boot (rc={server.returncode}); see log")
                return 2
            try:
                requests.get(f"{base}/v1/capabilities", timeout=5).raise_for_status()
                break
            except Exception:  # noqa: BLE001 - booting
                time.sleep(5)
        else:
            print("FAIL: server never became reachable")
            return 2
        listener_pid = _port_listener_pid(args.port)
        if listener_pid is None:
            print("FAIL: could not resolve the server pid from the port listener")
            return 2
        extra = external_cte + ((server.pid,) if server.poll() is None else ())
        sampler = TreeSampler(listener_pid, extra_pids=extra)

        # Substrate verification (never trust the pins alone): the doctor must
        # report the ARC row READY on the cte backend — anything else (clio-core
        # unavailable or degraded) fails the gate here.
        # The CTE daemon spawns during AGENT construction (a background task
        # after the port binds), so poll until the arc row SETTLES rather than
        # judging the first snapshot; the doctor itself is also slow cold.
        arc: dict = {}
        storage_mode = ""
        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                health = requests.get(f"{base}/v1/health", timeout=60).json()
            except Exception:  # noqa: BLE001 - cold-boot doctor latency; retry
                time.sleep(10)
                continue
            rows = [r for r in health.get("integrations", []) if r.get("name") == "arc"]
            arc = rows[0] if rows else {}
            storage_mode = str((arc.get("details") or {}).get("storage_mode") or "")
            if arc.get("status") in {"ready", "degraded", "unavailable"}:
                break  # settled — judge it
            time.sleep(10)  # still constructing/spawning; keep polling
        if arc.get("status") != "ready":
            print(
                "FAIL: gate substrate is not healthy cte — arc row: "
                f"status={arc.get('status')} storage_mode={storage_mode!r} "
                f"summary={arc.get('summary')!r}"
            )
            return 2
        print(f"substrate verified: arc ready, storage_mode={storage_mode!r}")
        initial_sessions = requests.get(
            f"{base}/v1/sessions", params={"include_all_workspaces": "true"}, timeout=30
        )
        initial_sessions.raise_for_status()
        if initial_sessions.json().get("sessions"):
            raise ValueError("Memory qualification requires a fresh private CLIO data directory")
        workspace_response = requests.post(
            f"{base}/v1/workspaces",
            json={"name": "Memory qualification", "root_path": str(args.workspace.resolve())},
            timeout=30,
        )
        workspace_response.raise_for_status()
        workspace_id = workspace_response.json()["id"]
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError("Memory qualification workspace has no usable id")
        if args.marketplace is not None:
            name = "mem-gate-" + args.marketplace.resolve().name
            requests.post(
                f"{base}/v1/plugins/marketplaces",
                json={"source": str(args.marketplace.resolve()), "name": name},
                timeout=300,
            ).raise_for_status()
            requests.post(
                f"{base}/v1/plugins/marketplaces/{name}/trust", json={}, timeout=300
            ).raise_for_status()
            print(f"installed and trusted marketplace {name} from {args.marketplace}")
        requests.put(
            f"{base}/v1/policies",
            json={
                "policies": [
                    {
                        "scope": "workspace",
                        "action": "allow",
                        "tool_name_pattern": "*",
                        "path_pattern": "*",
                    }
                ]
            },
            timeout=30,
        ).raise_for_status()

        sampler.start()
        time.sleep(15)
        if sampler.snapshots:
            idle = sampler.snapshots[-1]
            report("IDLE (post-boot)", idle[1], idle[2])

        data_path = (args.workspace / args.data).as_posix()
        out: dict[int, str] = {}
        sids: dict[int, str] = {}

        def _assign(i: int) -> tuple[str, str]:
            # Children scenario: session 0 drives the agent with alpha and beta
            # subagents and fans out 2 background children; the rest run the
            # standard clio-data load so the fleet cost is measured WITH real
            # child sessions resident alongside the normal acceptance load.
            if args.children_agent is not None and i == 0:
                return args.children_agent, CHILDREN_PROMPT
            return args.agent, PROMPT_SHAPES[i % len(PROMPT_SHAPES)].format(data=data_path)

        plan = [_assign(i) for i in range(args.sessions)]
        if args.children_agent is not None:
            print(
                f"scenario: children — session 0 -> {args.children_agent} "
                "(fan-out of 2 background children: alpha+beta)"
            )
        started = time.time()
        # Sampling already covers setup. No session's agent may change workspace
        # access after another session starts its turn.
        if not drive_load(
            base,
            plan,
            out,
            sids,
            workspace_id=workspace_id,
            model={"provider_id": args.provider, "model_id": args.model},
        ):
            return 2
        print(f"\nsessions: {json.dumps(out)}  wall: {time.time() - started:.0f}s")
        time.sleep(args.settle_s)  # let reclamation (once it exists, #933) act
        sampler.stop()

        # The measured object must still be alive: a server crash during the
        # settle window would otherwise yield empty snapshots and a vacuous
        # 0.00 GB "pass". Liveness = the SERVICE answers; the `uv run` wrapper
        # exiting on its own is noted but not fatal (the python server it
        # spawned keeps serving and stays the sampler root).
        if not _service_alive(base):
            print("FAIL: server died during the run; see log")
            return 2
        if server.poll() is not None:
            print(f"note: uv wrapper exited rc={server.returncode}; python server survived")
        peak = sampler.peak()
        final = sampler.final()
        if not peak or not final or not peak[1] or not final[1]:
            print("FAIL: empty samples (dead or unreadable server tree)")
            return 2
        report("PEAK", peak[1], peak[2])
        report(f"FINAL (after {args.settle_s}s settle)", final[1], final[2])
        peak_gb = sampler.total_gb(peak[1])
        final_gb = sampler.total_gb(final[1])

        failed_sessions = [k for k, v in out.items() if v != "idle"]
        if failed_sessions or len(out) != args.sessions:
            print(f"GATE: FAIL (sessions not all idle: {out})")
            return 1

        # Children scenario is only meaningful if the children ACTUALLY spawned: a
        # no-spawn run measures a lighter load and must never be recorded as a pass.
        if args.children_agent is not None:
            child_ok, child_detail = _assert_children_spawned(base, sids.get(0))
            print(child_detail)
            if not child_ok:
                print(
                    "GATE: FAIL — the children scenario recorded fewer than 2 child tasks. "
                    "A silent no-spawn cannot pass as a children run; fix the spawn, do not "
                    "record this measurement."
                )
                return 1
        if peak_gb <= 0 or final_gb <= 0:
            print(f"FAIL: non-positive measurement (peak={peak_gb}, final={final_gb})")
            return 2
        return RunMeasurement(peak_gb=peak_gb, final_gb=final_gb)
    finally:
        if sampler is not None:
            sampler.stop()
        try:
            if args.xdg is None:
                _stop_private_services(env)
        finally:
            server.terminate()
            try:
                server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                server.kill()
            if listener_pid is not None:
                try:
                    root = psutil.Process(listener_pid)
                    for child in root.children(recursive=True):
                        try:
                            child.kill()
                        except psutil.Error:
                            pass
                    root.kill()
                except psutil.Error:
                    pass  # already gone with the wrapper
            log.close()
            retained = args.workspace / f"mem-gate-server-{os.getpid()}-{time.time_ns()}.log"
            shutil.copyfile(log.name, retained)
            print(f"retained complete server log: {retained}")


if __name__ == "__main__":
    sys.exit(main())
