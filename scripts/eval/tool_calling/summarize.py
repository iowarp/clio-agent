"""Aggregate run results into the ledger's Evaluation tables (Markdown).

``uv run --frozen --no-sync python -m scripts.eval.tool_calling.summarize [--results DIR] [--out FILE]``

Reads ``<results>/<config>/<task>/run-<n>.json``. Each cell is the median of
the valid runs with the range in brackets, ``median [min-max]``; only runs
that pass the completion guard count, and the Steps cell says how many did
(``k/n valid``). Cells the harness cannot fill mechanically (whether the plan
was recited after compaction, how threads ended) say ``see trace``.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts.eval.tool_calling.chains import is_chain_task
from scripts.eval.tool_calling.claude_code_ref import rate_windows
from scripts.eval.tool_calling.grade import grade
from scripts.eval.tool_calling.metrics import run_metrics
from scripts.eval.tool_calling.paths import DEFAULT_FIXTURES, DEFAULT_RESULTS, manifest_path
from scripts.eval.tool_calling.tasks import ALL_TASKS

DASH = "—"


def load_runs(results: Path) -> list[dict[str, Any]]:
    """Every run JSON under ``results`` (raw exports and streams excluded)."""
    runs = []
    for path in sorted(results.glob("*/*/run-*.json")):
        if path.name.endswith(".export.json"):
            continue
        run = json.loads(path.read_text(encoding="utf-8"))
        run["_path"] = str(path)
        runs.append(run)
    return runs


def cell(values: list[float | None], fmt: Callable[[float], str] = lambda v: f"{v:g}") -> str:
    """``median [min-max]`` of the present values."""
    present = [float(v) for v in values if v is not None]
    if not present:
        return DASH
    mid = fmt(statistics.median(present))
    if len(present) == 1 or min(present) == max(present):
        return mid
    return f"{mid} [{fmt(min(present))}-{fmt(max(present))}]"


def _int(v: float) -> str:
    return f"{v:,.0f}"


def _one(v: float) -> str:
    return f"{v:.1f}"


def _two(v: float) -> str:
    return f"{v:.2f}"


def window_share(run: dict[str, Any]) -> tuple[float | None, float | None]:
    """Percentage points of the 5-hour and 7-day windows one run used."""
    windows = run.get("usage_windows") or {}
    five = [w.get("delta_points") for w in windows.values() if w.get("window_minutes") == 300]
    week = [w.get("delta_points") for w in windows.values() if w.get("window_minutes") == 10080]
    if windows:
        weekly = [v for v in week if v is not None]
        return (five[0] if five else None), (max(weekly) if weekly else None)
    limits = run.get("rate_limits") or []
    if len(limits) < 2:
        return None, None
    first, last = rate_windows(limits[0]), rate_windows(limits[-1])
    deltas = {k: (last[k] - first[k]) * 100 for k in last if k in first}
    weekly = [v for k, v in deltas.items() if k.startswith("seven_day")]
    return deltas.get("five_hour"), (max(weekly) if weekly else None)


class Row:
    """The runs of one (task, configuration)."""

    def __init__(self, task: str, config: str) -> None:
        self.task, self.config = task, config
        self.runs: list[dict[str, Any]] = []
        self.metrics: list[dict[str, Any]] = []
        self.grades: list[dict[str, Any] | None] = []

    def valid(self) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]]:
        return [
            (r, m, g)
            for r, m, g in zip(self.runs, self.metrics, self.grades, strict=True)
            if m["completion"]["valid"]
        ]

    def handles(self) -> str:
        return ", ".join(
            f"`{r.get('session_id') or (r.get('handles') or [''])[0]}`" for r in self.runs
        )

    def correct(self) -> str:
        graded = [g for g in self.grades if g and g.get("graded")]
        if not graded:
            return DASH
        return f"{sum(1 for g in graded if g.get('correct'))}/{len(graded)}"


def build_rows(runs: list[dict[str, Any]], manifest: dict[str, Any] | None) -> list[Row]:
    rows: dict[tuple[str, str], Row] = {}
    for run in runs:
        key = (str(run.get("task")), str(run.get("config")))
        row = rows.setdefault(key, Row(*key))
        row.runs.append(run)
        row.metrics.append(run_metrics(run))
        answer = ((manifest or {}).get("tasks") or {}).get(key[0], {}).get("answer")
        if is_chain_task(key[0]):
            answer = {}
        row.grades.append(grade(key[0], answer, run) if answer is not None else None)
    order = {t: i for i, t in enumerate(ALL_TASKS)}
    return sorted(rows.values(), key=lambda r: (order.get(r.task, 99), r.config))


def _table(header: list[str], body: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in body]
    return "\n".join(lines)


def usage_table(rows: list[Row]) -> str:
    body = []
    for row in rows:
        shares = [window_share(r) for r, _, _ in row.valid()]
        body.append(
            [
                f"{row.task} ({row.config})",
                cell([s[0] for s in shares], _two),
                cell([s[1] for s in shares], _two),
                cell([m["wall_s"] for _, m, _ in row.valid()], _one) + " s",
            ]
        )
    header = [
        "Task",
        "5-hour window share per run",
        "7-day window share per run",
        "Median wall time",
    ]
    return _table(header, body)


def _resets(m: dict[str, Any]) -> float | None:
    resets = m.get("prefix_resets")
    return None if resets is None else float(resets.get("prefix_mismatch", 0))


def performance_table(rows: list[Row]) -> str:
    body = []
    for row in (r for r in rows if r.task.startswith("P")):
        valid = row.valid()
        ms = [m for _, m, _ in valid]
        roots = [m["root"] for m in ms]
        body.append(
            [
                row.task,
                row.config,
                f"{cell([m['totals']['steps'] for m in ms], _int)} ({len(valid)}/{len(row.runs)} valid)",
                cell([m["totals"]["calls_per_step"] for m in ms], _two),
                f"{cell([m['totals']['uncached_per_step'] for m in ms], _int)} / "
                f"{cell([m['totals']['cached_per_step'] for m in ms], _int)}",
                cell([m["totals"]["output_tokens"] for m in ms], _int),
                cell([m["wall_s"] for m in ms], _one) + " s",
                f"{cell([r['ttft_ms']['median'] for r in roots], _int)} / "
                f"{cell([r['decode_ms']['median'] for r in roots], _int)} ms",
                cell([r["batch"]["ratio"] for r in roots], _two),
                cell([r["step_overhead_ms"]["mean"] for r in roots], _int) + " ms",
                cell([_resets(m) for m in ms], _int),
                row.correct(),
                row.handles(),
            ]
        )
    header = [
        "Task", "Configuration", "Steps", "Calls per step",
        "Prompt tokens per step (uncached / cached)", "Output tokens", "Wall time",
        "TTFT / decode", "Batch wall vs slowest call", "Step overhead", "Prefix resets",
        "Correct (where graded)", "Handles",
    ]  # fmt: skip
    return _table(header, body)


def long_horizon_table(rows: list[Row]) -> str:
    body = []
    for row in (r for r in rows if r.task.startswith("H")):
        valid = row.valid()
        ms = [m for _, m, _ in valid]
        roots = [m["root"] for m in ms]
        trend = " ; ".join(
            f"latency {_fmt(r['trend']['latency_ms_first_quarter'])}→"
            f"{_fmt(r['trend']['latency_ms_last_quarter'])} ms, prompt "
            f"{_fmt(r['trend']['prompt_tokens_first_quarter'])}→"
            f"{_fmt(r['trend']['prompt_tokens_last_quarter'])}"
            for r in roots
        )
        compactions = cell([m["totals"]["events"].get("compactions", 0) for m in ms], _int)
        plan = cell([m["totals"]["tool_counts"].get("plan_update", 0) for m in ms], _int)
        todos = cell([m["totals"]["tool_counts"].get("write_todos", 0) for m in ms], _int)
        body.append(
            [
                row.task,
                row.config,
                f"{cell([r['steps'] for r in roots], _int)} ({len(valid)}/{len(row.runs)} valid)",
                cell([m["wall_s"] for m in ms], _one) + " s",
                trend or DASH,
                f"{compactions} compactions; plan_update {plan}, write_todos {todos}; "
                "recital: see trace",
                "see trace",
                cell([m["totals"]["events"].get("goal_redrives", 0) for m in ms], _int),
                row.correct(),
                row.handles(),
            ]
        )
    header = [
        "Task", "Configuration", "Steps to self-stop", "Wall time",
        "Per-step latency and prompt tokens over the run",
        "Compactions; plan recited; findings carried",
        "Threads open at stop and how each ended", "Re-drives", "Correct root cause", "Handles",
    ]  # fmt: skip
    return _table(header, body)


def _fmt(value: float | None) -> str:
    return DASH if value is None else f"{value:,.0f}"


def render(runs: list[dict[str, Any]], manifest: dict[str, Any] | None) -> str:
    rows = build_rows(runs, manifest)
    return (
        "\n\n".join(
            [
                "### Usage per task",
                usage_table(rows),
                "### Performance",
                performance_table(rows),
                "### Long-horizon",
                long_horizon_table(rows),
            ]
        )
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="summarize", description=__doc__.split("\n")[0])
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    path = manifest_path(args.fixtures)
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    text = render(load_runs(args.results), manifest)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
