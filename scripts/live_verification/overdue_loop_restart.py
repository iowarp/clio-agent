"""Verify an overdue wakeup across real backend downtime, without changing clocks.

Run prepare against conversation_control_server.py; it stops the owned service.
Wait at least 70 seconds, resume that service, then run verify with the same arguments.
Each phase preserves separate requests and refuses to overwrite an earlier phase.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import requests
from long_horizon_controls import LongHorizonAcceptance


def main() -> None:
    """Prepare a durable wakeup, or observe its real recovery after cold startup."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["prepare", "verify"])
    parser.add_argument("--endpoint", default="http://127.0.0.1:17963")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--service", type=Path, required=True)
    args = parser.parse_args()
    if (args.out / args.phase / "requests.json").exists():
        raise RuntimeError("Use a fresh evidence directory; completed phases must be retained")
    runner = LongHorizonAcceptance(args.endpoint, args.out / args.phase)
    try:
        runner.until(
            "api",
            lambda: requests.get(runner.endpoint + "/v1/health", timeout=10).status_code,
            lambda code: code == 200,
            seconds=60,
        )
        if args.phase == "prepare":
            root = args.service / "workspace/long-horizon" / args.out.name
            root.mkdir(parents=True, exist_ok=True)
            runner.workspace = runner.call(
                "POST",
                "/v1/workspaces",
                {"name": "Overdue recovery", "root_path": str(root)},
                status=201,
            )["id"]
            sid = runner.session("Loop: overdue after downtime")
            runner.command(
                sid,
                "goal",
                "A successful create_artifact call saved overdue.md",
                max_goal_iters=2,
                max_wallclock_s=600,
            )
            runner.command(
                sid,
                "loop",
                "60s Public fictional acceptance check: use create_artifact with name='overdue.md', "
                "content='Fictional overdue checkpoint complete.', kind='other', pdf_preview=false. "
                "Then use loop_wakeup with delay_seconds=60 and prompt='Continue the fictional checkpoint'. "
                "Report the actual saved file and end. No other tools.",
                max_iters=2,
                max_wallclock_s=600,
            )
            (args.out / "before-downtime.json").write_text(
                json.dumps(
                    {
                        "sid": sid,
                        "workspace": runner.workspace,
                        "loop": runner.metadata(sid)["loop"],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            return
        before = json.loads((args.out / "before-downtime.json").read_text(encoding="utf-8"))
        sid = before["sid"]
        runner.sessions = {"Loop: overdue after downtime": sid}
        runner.workspace = before["workspace"]
        try:
            meta = runner.until(
                "overdue-goal",
                lambda: runner.metadata(sid),
                lambda m: m.get("goal", {}).get("cleared"),
                seconds=180,
            )
            rows = runner.messages(sid)
            runner.save("messages", rows)
            runner.check(
                "overdue wakeup finishes after cold restart",
                meta["goal"].get("clear_reason") == "goal_met"
                and meta["loop"].get("stop_reason") == "loop_goal_met",
                meta,
            )
            delivered = [
                row
                for row in rows
                if row["role"] == "user"
                and row.get("metadata", {}).get("schedule_id")
                == before["loop"]["pending_schedule_id"]
            ]
            runner.check(
                "overdue wakeup delivered once",
                len(delivered) == 1,
                before["loop"]["pending_schedule_id"],
            )
            runner.check(
                "no startup turn failure",
                not any(row.get("error_info") for row in rows),
                [row["id"] for row in rows if row.get("error_info")],
            )
        finally:
            if runner.metadata(sid).get("loop", {}).get("active"):
                runner.command(sid, "loop", "stop")
    finally:
        (args.service / "stop.request").touch()


if __name__ == "__main__":
    main()
