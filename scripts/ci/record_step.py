#!/usr/bin/env python3
"""Run one install-matrix step, time it, and append the result to a JSON-lines file.

The install matrix runs in report mode until its budgets are set: a failing step is
recorded with its exit code and the job carries on, so one broken path never hides the
timings of the others. ``--summary`` renders the collected rows as a Markdown table for
the job summary.

    python scripts/ci/record_step.py --file timings.jsonl --name "script install" -- bash install/install.sh
    python scripts/ci/record_step.py --file timings.jsonl --name "interrupted" --kill-after 40 -- bash install/install.sh
    python scripts/ci/record_step.py --file timings.jsonl --summary >> "$GITHUB_STEP_SUMMARY"
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from pathlib import Path


def run(args: argparse.Namespace, command: list[str]) -> int:
    started = time.monotonic()
    process = subprocess.Popen(command)  # noqa: S603 - the workflow's own command
    killed = False
    try:
        code = process.wait(timeout=args.kill_after or args.timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        code = process.wait()
        killed = True
    row = {
        "name": args.name,
        "os": f"{platform.system()} {platform.machine()}",
        "seconds": round(time.monotonic() - started, 1),
        "exit_code": code,
        "killed": killed,
        "outcome": "interrupted"
        if killed and args.kill_after
        else ("ok" if code == 0 else "failed"),
    }
    with Path(args.file).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    print(json.dumps(row))
    return 0


def summary(path: Path) -> int:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    print("| Step | OS | Seconds | Outcome |")
    print("|---|---|---|---|")
    for row in rows:
        outcome = (
            row["outcome"] if row["outcome"] != "failed" else f"failed (exit {row['exit_code']})"
        )
        print(f"| {row['name']} | {row['os']} | {row['seconds']} | {outcome} |")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--file", required=True)
    parser.add_argument("--name")
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--kill-after", type=float, default=0.0, help="interrupt after N seconds")
    parser.add_argument("--summary", action="store_true")
    args, rest = parser.parse_known_args()
    if args.summary:
        return summary(Path(args.file))
    command = rest[1:] if rest[:1] == ["--"] else rest
    if not args.name or not command:
        parser.error("--name and a command after -- are required")
    return run(args, command)


if __name__ == "__main__":
    sys.exit(main())
