#!/usr/bin/env python3
"""Enforce the install matrix's budgets on the timings and startup results a job recorded.

The matrix records every step (``record_step.py``) and the startup measurement
(``measure_startup.py``). The budgets in ``install_budgets.json`` are set from the
report-mode numbers with generous margins; this script checks the rows a job recorded
against them, prints a Markdown table for the job summary, and exits 1 with a clear
message for each budget exceeded. Steps without a budget stay report-only.

    python scripts/ci/check_budgets.py --timings timings.jsonl >> "$GITHUB_STEP_SUMMARY"
    python scripts/ci/check_budgets.py --startup results/ >> "$GITHUB_STEP_SUMMARY"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BUDGETS = Path(__file__).resolve().parent / "install_budgets.json"


def check_timings(rows: list[dict[str, Any]], budgets: dict[str, Any]) -> list[tuple[str, ...]]:
    """One table row per budgeted step: (step, OS, measured, budget, verdict)."""
    checked = []
    for name, budget in budgets.items():
        matching = [row for row in rows if row.get("name") == name]
        if not matching and budget.get("required"):
            checked.append((name, "-", "not recorded", f"{budget['seconds']} s", "FAIL: missing"))
        for row in matching:
            verdict = "ok"
            if budget.get("ok") and row.get("outcome") != "ok":
                verdict = f"FAIL: {row.get('outcome')} (exit {row.get('exit_code')})"
            elif row["seconds"] > budget["seconds"]:
                verdict = f"FAIL: {row['seconds']} s > {budget['seconds']} s"
            checked.append(
                (name, row.get("os", ""), f"{row['seconds']} s", f"{budget['seconds']} s", verdict)
            )
    return checked


def check_startup(results: Path, budgets: dict[str, Any]) -> list[tuple[str, ...]]:
    """One table row per budgeted startup value of each budgeted results file."""
    checked = []
    for filename, limits in budgets.items():
        path = results / filename
        if not path.is_file():
            checked.append((filename, "-", "not recorded", "-", "FAIL: missing"))
            continue
        result = json.loads(path.read_text(encoding="utf-8"))
        values = {**result, **result.get("marks", {})}
        values["task_runtime_ready_s"] = result.get("marks", {}).get("task_runtime_ready")
        for key, limit in limits.items():
            value = values.get(key)
            if "max" in limit:
                ok = isinstance(value, (int, float)) and value <= limit["max"]
                shown = f"<= {limit['max']}"
            else:
                ok = value == limit["equals"]
                shown = f"== {json.dumps(limit['equals'])}"
            verdict = "ok" if ok else f"FAIL: {json.dumps(value)} not {shown}"
            checked.append((filename, key, json.dumps(value), shown, verdict))
    return checked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--timings", type=Path, help="a record_step.py JSON-lines file")
    parser.add_argument("--startup", type=Path, help="the directory of startup-*.json results")
    parser.add_argument("--budgets", type=Path, default=BUDGETS)
    args = parser.parse_args()
    budgets = json.loads(args.budgets.read_text(encoding="utf-8"))
    rows: list[tuple[str, ...]] = []
    if args.timings:
        lines = (
            args.timings.read_text(encoding="utf-8").splitlines() if args.timings.is_file() else []
        )
        recorded = [json.loads(line) for line in lines if line.strip()]
        rows += check_timings(recorded, budgets["timings"])
        header = ("Step", "OS", "Measured", "Budget", "Verdict")
    if args.startup:
        rows += check_startup(args.startup, budgets["startup"])
        header = ("Result", "Value", "Measured", "Budget", "Verdict")
    if not args.timings and not args.startup:
        parser.error("pass --timings or --startup")
    print("\n### Budgets\n")
    print("| " + " | ".join(header) + " |")
    print("|" + "---|" * len(header))
    for row in rows:
        print("| " + " | ".join(row) + " |")
    failed = [row for row in rows if row[-1].startswith("FAIL")]
    for row in failed:
        print(f"::error title=Budget exceeded::{row[0]} {row[1]}: {row[-1]}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
