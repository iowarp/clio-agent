"""Summarize HDF5 benchmark results: a Markdown table and a small-multiples graph.

Reads ``<results>/<arm>-<where>/<task>-run<i>.json`` written by ``run.py``. Each cell is
the median over runs, with the range in brackets; the graph shows medians with min-max
whiskers for wall time, cost and model steps, grouped by task and where the data lives.

    uv run --frozen --no-sync python -m scripts.eval.hdf5_bench.summarize \\
        --results .cloud-run/hdf5-bench/results --out .cloud-run/hdf5-bench/summary
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

ARMS = ("ptc", "direct")
ARM_LABEL = {"ptc": "Scripts (run_code)", "direct": "Tool calls only"}
WHERE_LABEL = {"local": "Local file", "s3": "S3 by range"}
# Categorical slots 1-2 of the reference palette (identity follows the arm, never rank).
ARM_COLOR = {"ptc": "#2a78d6", "direct": "#eb6834"}
METRICS = (
    ("wall_s", "Wall time (s)", "{:.0f}"),
    ("cost_usd", "Cost (USD)", "{:.2f}"),
    ("steps", "Model steps", "{:.0f}"),
)


def load(results: Path) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for path in sorted(results.glob("*-*/*-run*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        groups[(row["task"], row["where"], row["arm"])].append(row)
    return groups


def _cell(values: list[float], fmt: str) -> str:
    if not values:
        return "–"
    med = fmt.format(statistics.median(values))
    if len(values) == 1:
        return med
    return f"{med} [{fmt.format(min(values))}–{fmt.format(max(values))}]"


def table(groups: dict[tuple[str, str, str], list[dict[str, Any]]]) -> str:
    head = (
        "| Task | Data | Arm | Runs | Score | Wall s | Tool s | Model s | Steps | Calls "
        "| Output tokens | Cost USD | S3 MB read |"
    )
    lines = [head, "|" + "---|" * 13]
    for (task, where, arm), rows in sorted(groups.items()):

        def col(key: str, rows: list[dict[str, Any]] = rows) -> list[float]:
            return [float(r[key]) for r in rows if r.get(key) is not None]

        s3 = [r["s3"]["bytes"] / 2**20 for r in rows if r.get("s3")]
        scores = [r["grade"]["score"] for r in rows]
        lines.append(
            f"| {task} | {WHERE_LABEL[where]} | {ARM_LABEL[arm]} | {len(rows)} "
            f"| {_cell(scores, '{:.2f}')} | {_cell(col('wall_s'), '{:.0f}')} "
            f"| {_cell(col('tool_s'), '{:.0f}')} | {_cell(col('model_s'), '{:.0f}')} "
            f"| {_cell(col('steps'), '{:.0f}')} | {_cell(col('tool_calls'), '{:.0f}')} "
            f"| {_cell(col('output_tokens'), '{:.0f}')} | {_cell(col('cost_usd'), '{:.2f}')} "
            f"| {_cell(s3, '{:.0f}') if s3 else '–'} |"
        )
    return "\n".join(lines) + "\n"


def graph(groups: dict[tuple[str, str, str], list[dict[str, Any]]], out: Path) -> None:
    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415

    cases = sorted({(task, where) for task, where, _ in groups})
    fig, axes = plt.subplots(1, len(METRICS), figsize=(4.2 * len(METRICS), 3.8), dpi=200)
    width = 0.36
    for ax, (key, title, _fmt) in zip(axes, METRICS, strict=True):
        for offset, arm in zip((-width / 2 - 0.02, width / 2 + 0.02), ARMS, strict=True):
            xs, meds, lo, hi = [], [], [], []
            for i, (task, where) in enumerate(cases):
                values = [float(r[key]) for r in groups.get((task, where, arm), []) if key in r]
                if not values:
                    continue
                med = statistics.median(values)
                xs.append(i + offset)
                meds.append(med)
                lo.append(med - min(values))
                hi.append(max(values) - med)
            if not xs:
                continue
            ax.bar(xs, meds, width, color=ARM_COLOR[arm], label=ARM_LABEL[arm], zorder=2)
            ax.errorbar(xs, meds, yerr=[lo, hi], fmt="none", ecolor="#52514e", lw=1, capsize=3)
        ax.set_xticks(range(len(cases)))
        ax.set_xticklabels([f"{t}\n{WHERE_LABEL[w]}" for t, w in cases], fontsize=8)
        ax.set_title(title, fontsize=10, loc="left", color="#0b0b0b")
        ax.grid(axis="y", color="#e4e3df", lw=0.6, zorder=0)
        ax.set_axisbelow(True)
        ax.set_facecolor("#fcfcfb")
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.tick_params(axis="y", labelsize=7, colors="#52514e", length=0)
        ax.spines["bottom"].set_color("#c3c2b7")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(
        handles,
        labels,
        frameon=False,
        fontsize=8,
        ncol=2,
        loc="upper left",
        bbox_to_anchor=(0.01, 0.93),
    )
    fig.suptitle(
        "HDF5 exploration: scripts vs tool calls, local vs S3 (median, min–max)",
        fontsize=11,
        x=0.01,
        ha="left",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    fig.savefig(out, facecolor="#fcfcfb")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--results", default=".cloud-run/hdf5-bench/results")
    parser.add_argument("--out", default=".cloud-run/hdf5-bench/summary")
    args = parser.parse_args(argv)
    groups = load(Path(args.results))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.md").write_text(table(groups), encoding="utf-8")
    graph(groups, out / "summary.png")
    print(out / "summary.md", out / "summary.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
