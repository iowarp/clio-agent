#!/usr/bin/env python3
"""Enforce the install matrix's budgets on the timings and results a job recorded.

The matrix records every step (``record_step.py``), the startup measurement
(``measure_startup.py``) and the packaged desktop's launch receipts
(``smoke_desktop_package.py``). The budgets in ``install_budgets.json`` are the user's
upper limits (agent start to ready, desktop launch to ready, install) plus the shutdown
budget; this script checks what a job recorded against them, prints a Markdown table
for the job summary, and exits 1 with one message per budget exceeded that names the
budget and the measured value. Steps without a budget stay report-only.

    python scripts/ci/check_budgets.py --timings timings.jsonl >> "$GITHUB_STEP_SUMMARY"
    python scripts/ci/check_budgets.py --startup results/ >> "$GITHUB_STEP_SUMMARY"
    python scripts/ci/check_budgets.py --desktop "$RUNNER_TEMP" >> "$GITHUB_STEP_SUMMARY"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BUDGETS = Path(__file__).resolve().parent / "install_budgets.json"
HEADER = ("Budget", "What", "Where", "Measured", "Limit", "Verdict")


def _exceeded(budget: str, measured: Any, limit: str) -> str:
    return f"FAIL: budget '{budget}' exceeded: measured {measured}, limit {limit}"


def check_timings(
    rows: list[dict[str, Any]], budgets: dict[str, Any], job: str = ""
) -> list[tuple[str, ...]]:
    """One table row per budgeted step that ran, and per step ``job`` must have run."""
    checked = []
    for name, budget in budgets.items():
        label, limit = budget.get("budget", name), f"{budget['seconds']} s"
        matching = [row for row in rows if row.get("name") == name]
        if not matching and job in budget.get("required_in", []):
            checked.append(
                (label, name, "-", "not recorded", limit, f"FAIL: budget '{label}': not measured")
            )
        for row in matching:
            measured = f"{row['seconds']} s"
            verdict = "ok"
            if budget.get("ok") and row.get("outcome") != "ok":
                verdict = (
                    f"FAIL: budget '{label}': the step {row.get('outcome')} "
                    f"(exit {row.get('exit_code')}) after {measured}"
                )
            elif row["seconds"] > budget["seconds"]:
                verdict = _exceeded(label, measured, limit)
            checked.append((label, name, row.get("os", ""), measured, limit, verdict))
    return checked


def _limit(rule: dict[str, Any]) -> str:
    return f"<= {rule['max']}" if "max" in rule else f"== {json.dumps(rule['equals'])}"


def _within(rule: dict[str, Any], value: Any) -> bool:
    if "max" in rule:
        return isinstance(value, (int, float)) and value <= rule["max"]
    return value == rule["equals"]


def check_startup(results: Path, budgets: dict[str, Any]) -> list[tuple[str, ...]]:
    """One table row per budgeted value of each budgeted startup results file."""
    checked = []
    for filename, limits in budgets.items():
        path = results / filename
        if not path.is_file():
            for key, rule in limits.items():
                label = rule.get("budget", key)
                verdict = f"FAIL: budget '{label}': not measured ({filename} missing)"
                checked.append((label, key, filename, "not recorded", _limit(rule), verdict))
            continue
        result = json.loads(path.read_text(encoding="utf-8"))
        values = {**result, **result.get("marks", {})}
        values["task_runtime_ready_s"] = result.get("marks", {}).get("task_runtime_ready")
        for key, rule in limits.items():
            label, value = rule.get("budget", key), values.get(key)
            measured = json.dumps(value) + (" s" if "max" in rule else "")
            verdict = (
                "ok" if _within(rule, value) else _exceeded(label, measured, _limit(rule) + " s")
            )
            checked.append((label, key, filename, measured, _limit(rule), verdict))
    return checked


def check_desktop(evidence_root: Path, budgets: dict[str, Any]) -> list[tuple[str, ...]]:
    """Budget the launch receipts ``smoke_desktop_package.py`` wrote under ``evidence-*``."""
    checked = []
    receipts = {
        path.parent.name.removeprefix("evidence-"): path
        for path in sorted(evidence_root.glob("evidence-*/receipt.json"))
    }
    for key, rule in budgets.items():
        label = rule.get("budget", key)
        for launch in rule.get("required_launches", []):
            if launch not in receipts:
                verdict = f"FAIL: budget '{label}': not measured (no {launch} launch receipt)"
                checked.append(
                    (label, key, f"{launch} launch", "not recorded", _limit(rule) + " s", verdict)
                )
        for launch, path in receipts.items():
            receipt = json.loads(path.read_text(encoding="utf-8"))
            value = receipt.get(key)
            measured = f"{value} s"
            verdict = (
                "ok" if _within(rule, value) else _exceeded(label, measured, _limit(rule) + " s")
            )
            checked.append((label, key, f"{launch} launch", measured, _limit(rule) + " s", verdict))
    return checked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--timings", type=Path, help="a record_step.py JSON-lines file")
    parser.add_argument("--startup", type=Path, help="the directory of startup-*.json results")
    parser.add_argument(
        "--desktop", type=Path, help="the directory holding evidence-*/receipt.json"
    )
    parser.add_argument(
        "--job", default="", help="the matrix job: its required steps fail when not recorded"
    )
    parser.add_argument("--budgets", type=Path, default=BUDGETS)
    args = parser.parse_args()
    if not (args.timings or args.startup or args.desktop):
        parser.error("pass --timings, --startup or --desktop")
    budgets = json.loads(args.budgets.read_text(encoding="utf-8"))
    rows: list[tuple[str, ...]] = []
    if args.timings:
        lines = (
            args.timings.read_text(encoding="utf-8").splitlines() if args.timings.is_file() else []
        )
        recorded = [json.loads(line) for line in lines if line.strip()]
        rows += check_timings(recorded, budgets["timings"], args.job)
    if args.startup:
        rows += check_startup(args.startup, budgets["startup"])
    if args.desktop:
        rows += check_desktop(args.desktop, budgets["desktop"])
    print("\n### Budgets\n")
    print("| " + " | ".join(HEADER) + " |")
    print("|" + "---|" * len(HEADER))
    for row in rows:
        print("| " + " | ".join(row) + " |")
    failed = [row for row in rows if row[-1].startswith("FAIL")]
    for row in failed:
        print(f"::error title=Budget exceeded::{row[1]} ({row[2]}): {row[-1]}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
