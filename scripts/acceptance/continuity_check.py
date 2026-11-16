#!/usr/bin/env python3
"""Conversation continuity across a restart and an update, live (the CC grid).

A conversation must survive the service stopping and coming back: closed and reopened,
killed and reopened, updated and reopened. This script creates conversations in known
states with plain requests, applies a transition to the server, reopens them and checks
each one (``continuity_states.verify_case``):

* it lists and opens with its title, messages and tool results intact;
* a turn the transition cut reads as interrupted with a typed reason, never ``running``;
* a new plain message gets a real answer that uses the earlier context.

States: ``1`` idle after an answer, ``2`` idle after several tool calls with a large
output, ``3a`` mid model stream, ``3b`` mid shell call; and (``continuity_more.py``) ``4``
waiting on a permission, ``5`` waiting on the agent's question, ``7`` a child task
running, ``8`` compacted, ``9r`` rewound, ``9f`` forked; ``8n`` (``continuity_compaction.py``)
just below auto-compaction, run with ``CLIO_AUTOCOMPACT_PCT=0.04``.

Transitions (the server runs through ``scripts/cloud/stack.sh`` with this checkout's
``.cloud-run`` state; export the stack's ports and ``CLIO_CLOUD_ISOLATE=1`` first):

* ``A`` graceful restart: stop the server (it stops its clio-core daemon), start it;
* ``B`` hard stop: SIGKILL the server's process group, start it;
* ``B-core`` hard stop of the server and its clio-core daemon;
* ``C`` update: states made by ``--old-agent-bin`` (a released ``clio-agent``), stopped
  gracefully, reopened by this checkout on the same folders and ports;
* ``D`` update after a hard stop: the old version killed mid-turn, this checkout starts.

Usage::

    python3 scripts/acceptance/continuity_check.py grid --cells A,B --states 1,2,3a,3b \\
        --out .cloud-run/continuity
    python3 scripts/acceptance/continuity_check.py grid --cells C,D \\
        --old-agent-bin /path/to/old-venv/bin/clio-agent --out .cloud-run/continuity

``setup`` and ``verify`` run the two halves by hand around a transition done elsewhere.
Exit status is 0 when every cell passes. The model calls are real: a run spends them.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import continuity_compaction  # noqa: E402,F401 - registers state 8n
import continuity_more  # noqa: E402,F401 - registers states 4, 5, 7, 8, 9r and 9f
import continuity_states as cs  # noqa: E402

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
STACK = ROOT / "scripts" / "cloud" / "stack.sh"
CELLS = {"A", "B", "B-core", "C", "D"}


def agent_url() -> str:
    return f"http://127.0.0.1:{os.environ.get('AGENT_PORT', '8787')}"


def stack(command: str, agent_bin: str = "") -> str:
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(ROOT))
    if agent_bin:
        env["CLIO_AGENT_BIN"] = agent_bin
    else:
        env.pop("CLIO_AGENT_BIN", None)
    # Output goes to a file: the started server inherits no pipe of ours to hold open.
    log = ROOT / ".cloud-run" / "continuity-stack.out"
    with log.open("w") as sink:
        done = subprocess.run(
            ["bash", str(STACK), command],
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
            timeout=400,
            check=False,
        )
    out = log.read_text().strip()
    print(f"  stack {command}: {out}", flush=True)
    if done.returncode != 0:
        raise RuntimeError(f"stack.sh {command} failed: {out}")
    return out


def wait_ready(api: cs.Api, timeout: float = 240.0) -> dict[str, Any]:
    """Wait until the server reports ``readiness: ready``; return the last health body."""
    deadline = time.monotonic() + timeout
    health: Any = {}
    while time.monotonic() < deadline:
        status, health = api.call("GET", "/v1/health")
        if status == 200 and isinstance(health, dict) and health.get("readiness") == "ready":
            return health
        time.sleep(1.0)
    raise RuntimeError(f"server not ready within {timeout:.0f}s: {str(health)[:400]}")


def setup_states(api: cs.Api, states: list[str], label: str) -> list[cs.Case]:
    """Create the idle states one by one, then start the running ones together."""
    cases: list[cs.Case] = []
    for state in [s for s in states if not s.startswith(("3", "4", "5"))]:
        cases.append(cs.STATES[state](api, label))
        print(f"  state {state}: {cases[-1].sid} {cases[-1].setup_notes}", flush=True)
    running = [s for s in states if s.startswith(("3", "4", "5"))]
    results: dict[str, cs.Case | BaseException] = {}

    def run(state: str) -> None:
        try:
            results[state] = cs.STATES[state](api, label)
        except BaseException as error:  # noqa: BLE001 - reported with its state below
            results[state] = error

    threads = [threading.Thread(target=run, args=(s,)) for s in running]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for state in running:
        result = results[state]
        if isinstance(result, BaseException):
            raise RuntimeError(f"state {state} setup failed: {result}") from result
        cases.append(result)
        print(f"  state {state}: {result.sid} {result.setup_notes}", flush=True)
    for case in cases:
        case.before = cs.snapshot(api, case.sid)
    return cases


def kill_agent() -> None:
    pid_file = ROOT / ".cloud-run" / "agent.pid"
    pgid = int(pid_file.read_text().strip())
    os.killpg(pgid, signal.SIGKILL)
    for _ in range(40):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.25)
    pid_file.unlink(missing_ok=True)
    print(f"  SIGKILL agent process group {pgid}", flush=True)


def kill_core() -> None:
    """SIGKILL this home's clio-core daemon, named by its pidfile."""
    state = Path(os.environ.get("XDG_STATE_HOME", ROOT / ".cloud-run/xdg/state"))
    for pid_file in state.glob("clio-agent/core-hosts/*/clio-runtime.pid"):
        try:
            pid = int(pid_file.read_text().split()[0])
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except (OSError, ValueError):
            continue
        if b"clio_run" in cmdline:
            os.kill(pid, signal.SIGKILL)
            print(f"  SIGKILL clio-core daemon {pid}", flush=True)


def transition(cell: str) -> None:
    if cell in ("A", "C"):
        stack("agent-stop")
    else:
        kill_agent()
        if cell == "B-core":
            kill_core()


def run_cell(cell: str, states: list[str], old_bin: str, out: Path) -> list[dict[str, Any]]:
    """One column of the grid: create the states, transition, reopen and check."""
    print(f"cell {cell}: states {states}", flush=True)
    api = cs.Api(agent_url())
    # Start the version this cell creates its states with, never the one left running.
    stack("agent-stop")
    stack("agent-start", old_bin if cell in ("C", "D") else "")
    wait_ready(api)
    label = f"{cell} {time.strftime('%H%M')}"
    cases = setup_states(api, states, label)
    (out / f"{cell}-cases.json").write_text(
        json.dumps([cs.case_record(c) for c in cases], indent=2)
    )
    transition(cell)
    started = time.monotonic()
    stack("agent-start")
    health = wait_ready(api)
    print(f"  ready {time.monotonic() - started:.0f}s after start; failure={health.get('failure')}")
    verdicts = [cs.verdict_record(cs.verify_case(api, case)) for case in cases]
    for verdict in verdicts:
        verdict["cell"] = cell
        mark = "PASS" if verdict["passed"] else "FAIL"
        print(f"  {cell} {verdict['state']} {verdict['sid']}: {mark} {verdict['problems']}")
    (out / f"{cell}-verdicts.json").write_text(json.dumps(verdicts, indent=2))
    return verdicts


def cmd_grid(args: argparse.Namespace) -> int:
    cells = [c.strip() for c in args.cells.split(",") if c.strip()]
    unknown = set(cells) - CELLS
    if unknown:
        raise SystemExit(f"unknown cells {sorted(unknown)}; choose from {sorted(CELLS)}")
    if {"C", "D"} & set(cells) and not args.old_agent_bin:
        raise SystemExit("cells C and D need --old-agent-bin (a released clio-agent)")
    states = [s.strip() for s in args.states.split(",") if s.strip()]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stack("agent-stop")
    verdicts: list[dict[str, Any]] = []
    for cell in cells:
        try:
            verdicts.extend(run_cell(cell, states, args.old_agent_bin, out))
        except Exception as error:  # noqa: BLE001 - a broken cell is a failed cell
            print(f"  {cell}: ERROR {error}", flush=True)
            verdicts.append({"cell": cell, "state": "*", "passed": False, "problems": [str(error)]})
    (out / "grid.json").write_text(json.dumps(verdicts, indent=2))
    failed = [v for v in verdicts if not v["passed"]]
    print(f"\n{len(verdicts) - len(failed)}/{len(verdicts)} cells passed; report {out}/grid.json")
    return 1 if failed else 0


def cmd_setup(args: argparse.Namespace) -> int:
    api = cs.Api(args.agent or agent_url())
    states = [s.strip() for s in args.states.split(",") if s.strip()]
    cases = setup_states(api, states, args.label)
    Path(args.out).write_text(json.dumps([cs.case_record(c) for c in cases], indent=2))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    api = cs.Api(args.agent or agent_url())
    cases = [cs.case_from_record(row) for row in json.loads(Path(args.cases).read_text())]
    verdicts = [cs.verdict_record(cs.verify_case(api, case)) for case in cases]
    for verdict in verdicts:
        mark = "PASS" if verdict["passed"] else "FAIL"
        print(f"{verdict['state']} {verdict['sid']}: {mark} {verdict['problems']}")
    if args.out:
        Path(args.out).write_text(json.dumps(verdicts, indent=2))
    return 0 if all(v["passed"] for v in verdicts) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    grid = sub.add_parser("grid", help="run transitions end to end through stack.sh")
    grid.add_argument("--cells", default="A,B")
    grid.add_argument("--states", default="1,2,3a,3b")
    grid.add_argument("--old-agent-bin", default="")
    grid.add_argument("--out", default=str(ROOT / ".cloud-run" / "continuity"))
    setup = sub.add_parser("setup", help="create states on a running server")
    setup.add_argument("--agent", default="")
    setup.add_argument("--states", default="1,2,3a,3b")
    setup.add_argument("--label", default="manual")
    setup.add_argument("--out", required=True)
    verify = sub.add_parser("verify", help="reopen and check states after a transition")
    verify.add_argument("--agent", default="")
    verify.add_argument("--cases", required=True)
    verify.add_argument("--out", default="")
    args = parser.parse_args()
    return {"grid": cmd_grid, "setup": cmd_setup, "verify": cmd_verify}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
