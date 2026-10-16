"""Exercise real goal work, independent LLM judgments and automatic continuation."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    allow_all,
    bind_provider,
    boot_server,
    client,
    create_session,
    create_workspace,
    dump_json,
    session_messages,
    terminate_server,
    wait_health,
)


def run(provider: str, model: str, out: Path) -> dict[str, Any]:
    """Run a real four-phase task and retain transitions, transcripts and file evidence."""
    out.mkdir(parents=True, exist_ok=True)
    workspace = out / "workspace"
    state = out / "state"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # Keep model sign-in in its normal provider store; every CLIO mutable root
    # belongs to this probe. Never use the installed application's workspace.
    env = {
        f"CLIO_AGENT_{role}_DIR": str(state / role.lower())
        for role in ("CONFIG", "DATA", "STATE", "CACHE")
    }
    env.update(CLIO_ALLOWED_ROOTS=str(workspace), CLIO_SESSIONS_PATH=str(state / "sessions.json"))
    proc = boot_server(port, cwd=workspace, sse_log=out / "stream.jsonl", extra_env=env)
    call = client(f"http://127.0.0.1:{port}")
    result: dict[str, Any] = {"provider": provider, "model": model, "port": port}
    samples: list[dict[str, Any]] = []
    started = time.monotonic()
    try:
        if not wait_health(call):
            raise RuntimeError("Private server did not start")
        bind_provider(call, provider=provider, model=model)
        allow_all(call)
        wsid = create_workspace(call, "Goal iteration acceptance", workspace)
        sid = create_session(call, wsid, "Four real goal iterations")
        result.update(session_id=sid, workspace_id=wsid)
        objective = (
            f"Build a verified Collatz project in {workspace}. Complete exactly ONE phase per "
            "work turn, then finish that turn so the goal controller can judge and continue. "
            "Never complete a later phase in the same turn. Append one numbered iteration "
            "entry to PROGRESS.md for each phase with command evidence. "
            "Phase 1: implement collatz_lab.py with memoized collatz_steps(n) and "
            "longest_chain(limit), rejecting nonpositive integers, and run a smoke check. "
            "Phase 2: write test_collatz_lab.py and run pytest: steps(1)=0, steps(27)=111, "
            "longest_chain(1000000)=(837799,524), and invalid-input errors. "
            "Phase 3: compute lengths for 1..1000000; save collatz_stats.csv with summary "
            "statistics and collatz_steps.png histogram using matplotlib, and inspect the image. "
            "Phase 4: write REPORT.md referencing the chart and numerical findings, register "
            "report and chart as artifacts. Complete only when all four phases passed, tests "
            "passed, every file exists and PROGRESS.md has four separate iteration entries. "
            "Use filesystem and shell tools. Do not delegate, start monitoring loops, or "
            "ask for instructions. You have approval for operations inside this workspace."
        )
        call(
            "POST",
            f"/v1/sessions/{sid}/commands/goal",
            {"input": objective, "args": {"max_goal_iters": 8}},
        )
        deadline = time.monotonic() + 1800
        last: tuple[Any, ...] | None = None
        while time.monotonic() < deadline:
            before = time.monotonic()
            snapshot = call("GET", f"/v1/sessions/{sid}")
            session = snapshot.get("session", snapshot)
            goal = (session.get("metadata") or {}).get("goal", {})
            sample = {
                "elapsed_s": round(time.monotonic() - started, 2),
                "read_latency_s": round(time.monotonic() - before, 3),
                "status": session.get("status"),
                "goal": goal,
            }
            samples.append(sample)
            identity = (session.get("status"), goal.get("phase"), goal.get("iters_elapsed"))
            if identity != last:
                print(
                    json.dumps(
                        {k: v for k, v in sample.items() if k != "goal"}
                        | {"goal_phase": goal.get("phase"), "iterations": goal.get("iters_elapsed")}
                    ),
                    flush=True,
                )
                last = identity
                dump_json(out / "transitions.json", samples)
                dump_json(out / "messages.json", session_messages(call, sid))
            if goal.get("cleared") or goal.get("paused"):
                result["goal"] = goal
                break
            time.sleep(1)
        result["messages"] = len(session_messages(call, sid))
        dump_json(out / "messages.json", session_messages(call, sid))
        result["files"] = {
            name: (workspace / name).stat().st_size if (workspace / name).is_file() else 0
            for name in (
                "collatz_lab.py",
                "test_collatz_lab.py",
                "PROGRESS.md",
                "collatz_stats.csv",
                "collatz_steps.png",
                "REPORT.md",
            )
        }
        goal = result.get("goal", {})
        result["checks"] = {
            "goal_met": goal.get("met") is True,
            "four_separate_iterations": int(goal.get("iters_elapsed") or 0) == 4,
            "all_files": all(result["files"].values()),
            "checking_visible": any(s["goal"].get("phase") == "judging" for s in samples),
            "no_judge_failure": goal.get("clear_reason") != "goal_judge_unavailable",
        }
        result["pass"] = all(result["checks"].values())
    except Exception as exc:
        result.update({"pass": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        dump_json(out / "transitions.json", samples)
        result["elapsed_s"] = round(time.monotonic() - started, 1)
        dump_json(out / "verdict.json", result)
        terminate_server(proc)
    return result


def main() -> int:
    """Choose an authenticated real provider and a private output directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="claude_code")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    result = run(args.provider, args.model, args.out.resolve())
    print(json.dumps(result, indent=2), flush=True)
    return 0 if result.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
