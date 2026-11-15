"""Summarize HDF5 benchmark results: a Markdown table and a small-multiples graph.

Reads ``<results>/<arm>-<where>/<task>-run<i>.json`` written by ``run.py``. Each cell is
the median over runs, with the range in brackets; the graph shows every run as a dot with
the median as a tick, and the direct/scripts ratios of steps, input tokens and cost.

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


PANELS = (
    ("steps", "Model steps"),
    ("input_total", "Input tokens (incl. cached)"),
    ("cost_usd", "Cost (USD)"),
    ("wall_s", "Wall time (s)"),
    ("s3_mb", "S3 bytes read (MB)"),
)
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"


def _metric(row: dict[str, Any], key: str) -> float | None:
    if key == "input_total":
        return float(row.get("input_tokens") or 0) + float(row.get("cached_input_tokens") or 0)
    if key == "s3_mb":
        return row["s3"]["bytes"] / 2**20 if row.get("s3") else None
    value = row.get(key)
    return None if value is None else float(value)


def ratios(groups: dict[tuple[str, str, str], list[dict[str, Any]]]) -> list[str]:
    """Median direct / median PTC for steps, input tokens and cost, per task and location."""
    lines = []
    for task, where in sorted({(t, w) for t, w, _ in groups}):
        ptc, direct = groups.get((task, where, "ptc"), []), groups.get((task, where, "direct"), [])
        if not ptc or not direct:
            continue
        parts = []
        for key, label in (("steps", "steps"), ("input_total", "input"), ("cost_usd", "cost")):
            a = [v for r in direct if (v := _metric(r, key)) is not None]
            b = [v for r in ptc if (v := _metric(r, key)) is not None]
            if a and b and statistics.median(b) > 0:
                parts.append(f"{label} x{statistics.median(a) / statistics.median(b):.1f}")
        lines.append(f"{task} {WHERE_LABEL[where]}: " + ", ".join(parts))
    return lines


def graph(groups: dict[tuple[str, str, str], list[dict[str, Any]]], out: Path) -> None:
    """Every run as a dot with each cell's median as a tick; no bars, so spread stays visible."""
    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415
    from matplotlib.lines import Line2D  # noqa: PLC0415

    cases = sorted({(task, where) for task, where, _ in groups})
    fig, axes = plt.subplots(2, 3, figsize=(13, 7.2), dpi=200)
    offsets = {"ptc": -0.17, "direct": 0.17}
    for ax, (key, title) in zip(axes.flat, PANELS, strict=False):
        for arm in ARMS:
            for i, (task, where) in enumerate(cases):
                values = [
                    v
                    for r in groups.get((task, where, arm), [])
                    if (v := _metric(r, key)) is not None
                ]
                if not values:
                    continue
                x = i + offsets[arm]
                jitter = [x + (j - (len(values) - 1) / 2) * 0.04 for j in range(len(values))]
                ax.scatter(
                    jitter,
                    values,
                    s=22,
                    color=ARM_COLOR[arm],
                    edgecolor="#fcfcfb",
                    linewidth=1.5,
                    zorder=3,
                )
                med = statistics.median(values)
                ax.plot([x - 0.12, x + 0.12], [med, med], color=ARM_COLOR[arm], lw=2, zorder=2)
        ax.set_xticks(range(len(cases)))
        ax.set_xticklabels([f"{t}\n{WHERE_LABEL[w]}" for t, w in cases], fontsize=8, color=MUTED)
        ax.set_title(title, fontsize=10, loc="left", color=INK)
        ax.set_ylim(bottom=0)
        ax.set_xlim(-0.5, len(cases) - 0.5)
        ax.grid(axis="y", color=GRID, lw=0.6, zorder=0)
        ax.set_axisbelow(True)
        ax.set_facecolor("#fcfcfb")
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color("#c3c2b7")
        ax.tick_params(axis="y", labelsize=7, colors=MUTED, length=0)
    note = axes.flat[-1]
    note.axis("off")
    note.set_title("Direct ÷ scripts (medians)", fontsize=10, loc="left", color=INK)
    note.text(
        0,
        0.95,
        "\n".join(ratios(groups)) or "needs both arms",
        fontsize=8,
        color=INK,
        va="top",
        family="monospace",
    )
    handles = [
        Line2D([], [], marker="o", ls="", color=ARM_COLOR[a], label=ARM_LABEL[a]) for a in ARMS
    ]
    handles.append(Line2D([], [], color=MUTED, lw=2, label="median"))
    fig.legend(
        handles=handles,
        frameon=False,
        fontsize=8,
        ncol=3,
        loc="upper left",
        bbox_to_anchor=(0.01, 0.95),
    )
    fig.suptitle(
        "HDF5 exploration: scripts vs tool calls, local vs S3 (each dot is a run)",
        fontsize=11,
        x=0.01,
        ha="left",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.91))
    fig.savefig(out, facecolor="#fcfcfb")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--results", default=".cloud-run/hdf5-bench/results")
    parser.add_argument("--out", default=".cloud-run/hdf5-bench/summary")
    args = parser.parse_args(argv)
    groups = load(Path(args.results))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.md").write_text(
        table(groups) + "\n" + "\n".join(f"- {line}" for line in ratios(groups)) + "\n",
        encoding="utf-8",
    )
    graph(groups, out / "summary.png")
    print(out / "summary.md", out / "summary.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
