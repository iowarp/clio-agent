"""MCP fleet memory attribution + release-gating budget check (#930 S1/#931).

Samples a running gact server's WHOLE process tree per-process (role-
classified), drives the standard acceptance load (N concurrent sessions on
the active blueprint), and reports IDLE / PEAK / FINAL attribution. With
``--assert-budget`` the run FAILS if peak/final exceed the recorded budget in
``scripts/mcp_mem_budget.json`` — the budget only ratchets DOWN (record new,
lower numbers after an optimization lands; never raise them to make a
regression pass).

Usage (the #921/#929 acceptance shape — 3 concurrent claude-haiku sessions):

    uv run python scripts/mcp_mem_attribution.py \
        --pack external/clio-agent-marketplace/data-semantics \
        --workspace <dir with sensor_readings.csv> \
        --sessions 3 --assert-budget

The server is booted as a child of THIS process (claude_code/haiku + the real
CTE substrate per the accepted gate config — never CLIO_ARC_STORE=local) and
torn down on exit. Run it as ONE task: server and load share this process
tree so external task eviction cannot split them.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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
    (
        "Is {data} trustworthy enough to build a report on? Walk me through "
        "its condition."
    ),
]


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


def drive_session(base: str, pack: Path, prompt: str, out: dict, idx: int) -> None:
    s = requests.Session()
    try:
        sid = s.post(f"{base}/v1/sessions", json={"title": f"membudget {idx}"}, timeout=30).json()[
            "id"
        ]
        s.post(
            f"{base}/v1/sessions/{sid}/agent-blueprint", json={"path": str(pack)}, timeout=120
        ).raise_for_status()
        deadline = time.time() + 300
        while True:
            r = s.post(f"{base}/v1/sessions/{sid}/messages", json={"text": prompt}, timeout=60)
            if r.status_code != 503 or time.time() > deadline:
                break
            time.sleep(10)
        r.raise_for_status()
        deadline = time.time() + 1200
        while time.time() < deadline:
            try:
                status = s.get(f"{base}/v1/sessions/{sid}", timeout=30).json().get("status", "")
            except Exception:  # noqa: BLE001 - server sheds keepalives under load; retry
                time.sleep(10)
                continue
            if status in {"idle", "error"}:
                out[idx] = status
                return
            time.sleep(10)
        out[idx] = "timeout"
    except Exception as exc:  # noqa: BLE001 - a failed session is a failed run, reported below
        out[idx] = f"error: {exc!r}"


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", required=True, type=Path)
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
            "XDG_CONFIG_HOME for the server. Defaults to a FRESH stamped temp dir: "
            "the config-FILE layer outranks env pins (conf precedence file>env), so "
            "an inherited real config could silently swap substrate/provider."
        ),
    )
    parser.add_argument("--settle-s", type=int, default=60, help="post-load settle before FINAL")
    parser.add_argument("--assert-budget", action="store_true")
    args = parser.parse_args()

    if args.sessions < 1:
        print("FAIL: --sessions must be >= 1 (a zero-session run proves nothing)")
        return 2
    if args.assert_budget and args.sessions != 3:
        print("FAIL: --assert-budget is defined for the recorded load (--sessions 3)")
        return 2

    xdg = args.xdg or Path(tempfile.mkdtemp(prefix="clio-mem-gate-xdg-"))
    # Pack mcp_servers mount at AGENT CONSTRUCTION from INSTALLED blueprints
    # (path-activation alone mounts nothing), so the pack must be installed
    # into the gate XDG or the run measures a fleet-less server.
    install_root = xdg / "clio-agent" / "agent-blueprints" / args.pack.name
    if not install_root.exists():
        shutil.copytree(args.pack, install_root)
        print(f"installed pack into gate XDG: {install_root}")
    base = f"http://127.0.0.1:{args.port}"
    env = {
        **os.environ,
        "CLIO_LM_PROVIDER": args.provider,
        "CLIO_LM_MODEL": args.model,
        # The accepted gate substrate — never 'local' (underperforming fallback).
        "CLIO_ARC_STORE": "cte",
        "CLIO_ALLOWED_ROOTS": str(args.workspace),
        "CLIO_SEMANTIC_TRACE_BACKEND": "none",
        "XDG_CONFIG_HOME": str(xdg),
    }

    # A pre-existing clio-core daemon (connect-or-spawn) is OUTSIDE the server
    # tree — sample it explicitly or the substrate cost silently drops out.
    external_cte = tuple(
        proc.pid
        for proc in psutil.process_iter(["name"])
        if str(proc.info.get("name") or "").lower().startswith("clio_run")
    )
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
        # report the ARC row READY on the cte backend — a LOUD degrade to the
        # local store (or an inherited config selecting it) fails the gate here.
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
            if storage_mode == "local" or arc.get("status") in {"ready", "degraded"}:
                break  # settled (ready-cte, degraded-anything, or local) — judge it
            time.sleep(10)  # still constructing/spawning; keep polling
        if arc.get("status") != "ready" or storage_mode == "local":
            print(
                "FAIL: gate substrate is not healthy cte — arc row: "
                f"status={arc.get('status')} storage_mode={storage_mode!r} "
                f"summary={arc.get('summary')!r}"
            )
            return 2
        print(f"substrate verified: arc ready, storage_mode={storage_mode!r}")
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
        out: dict = {}
        threads = [
            threading.Thread(
                target=drive_session,
                args=(base, args.pack, PROMPT_SHAPES[i % len(PROMPT_SHAPES)].format(data=data_path), out, i),
                daemon=True,
            )
            for i in range(args.sessions)
        ]
        started = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=1500)
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
        print(f"\npeak: {peak_gb:.2f} GB   final: {final_gb:.2f} GB")

        failed_sessions = [k for k, v in out.items() if v != "idle"]
        if failed_sessions or len(out) != args.sessions:
            print(f"GATE: FAIL (sessions not all idle: {out})")
            return 1

        if args.assert_budget:
            budget = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
            ok, detail = check_budget(peak_gb, final_gb, budget)
            print(f"BUDGET ({BUDGET_PATH.name}): {detail}")
            if not ok:
                print(
                    "GATE: FAIL — fleet memory regressed past the recorded budget. "
                    "Fix the regression; never raise the budget."
                )
                return 1
            if peak_gb < budget["peak_gb"] * 0.9 or final_gb < budget["final_gb"] * 0.9:
                print(
                    "NOTE (ratchet down): measured well under budget — record the "
                    f"MEDIAN of >=3 runs in {BUDGET_PATH.name} (never a single noise "
                    "trough: the 5% tolerance must still cover honest run-to-run "
                    "variance under the new number)."
                )
        print("GATE: PASS")
        return 0
    finally:
        if sampler is not None:
            sampler.stop()
        server.terminate()
        try:
            server.wait(timeout=20)
        except Exception:  # noqa: BLE001 - hard-kill fallback on teardown
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


if __name__ == "__main__":
    sys.exit(main())
