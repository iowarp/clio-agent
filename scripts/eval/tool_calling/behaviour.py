"""How an agent spent a run: the shape of its work, not only how long it took.

``uv run --frozen --no-sync python -m scripts.eval.tool_calling.behaviour [results]``

Wall time says whether a run was fast; this says why. For every run it reads the
recorded steps, calls and model calls and reports:

- **steps**: model calls, each one a turn the agent waited for;
- **width**: tool calls the agent made at once in one step (mean, and the widest);
- **shell composition**: shell calls, and how many commands each one ran, joined by
  pipes (``|``) or sequenced (``&&``, ``;``, newlines), so one call doing the work of
  several shows as such;
- **scripts**: ``run_code`` scripts and the tool calls they made between them, which
  never cost a model turn;
- **time**: model time (all model calls), tool time (the union of the agent's own
  calls, nested script calls included in their script) and the rest, which is the
  harness: recording, hooks, scheduling between steps;
- **clio-core and cache**: puts and megabytes sent to clio-core, and the provider's
  prompt-cache tokens created and read (from the stream audit, when it was on);
- **clio-core time**: total and longest store call, the history writer's queue wait
  (longest too) and run time, and waits at the highway barrier
  (``runtime.io_audit``), so a run shows where it waited on storage;
- **context**: the root agent's prompt at its first and largest model call;
- **compaction**: how many ran, the context before and after each (median shrink),
  and the share of the checkpoint call's input read from the cache;
- **delegation**: child sessions and the models they ran on;
- **tool-return semantics**: injections the agent read (by source, in
  :func:`run_semantics`) and reads of spilled tool output.

A table per task puts the configurations side by side with the median over runs.
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from scripts.eval.tool_calling.collect import SPILL_MARKER
from scripts.eval.tool_calling.metrics import _union_ms
from scripts.eval.tool_calling.paths import DEFAULT_RESULTS
from scripts.eval.tool_calling.summarize import load_runs

_SHELL = "shell_bash"
_SCRIPT = "run_code"
#: Separators that join commands within one shell call (quoted text is removed first).
_PIPE = re.compile(r"(?<![|])\|(?![|])")
_SEQUENCE = re.compile(r"&&|\|\||;|\n")
_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")


def shell_shape(command: str) -> tuple[int, int, int]:
    """``(commands, pipes, sequences)`` in one shell command line."""
    bare = _QUOTED.sub("''", command.strip())
    pipes = len(_PIPE.findall(bare))
    sequences = len([s for s in _SEQUENCE.split(bare) if s.strip()]) - 1
    return 1 + pipes + max(sequences, 0), pipes, max(sequences, 0)


def _nested(calls: list[dict[str, Any]]) -> set[str]:
    """Ids of calls a ``run_code`` script made (inside a script's interval, same step)."""
    scripts = [c for c in calls if c.get("tool") == _SCRIPT and c.get("started_at")]
    inner: set[str] = set()
    for call in calls:
        if call.get("tool") == _SCRIPT or not call.get("started_at"):
            continue
        for script in scripts:
            same_step = script.get("step_id") == call.get("step_id")
            within = script["started_at"] <= call["started_at"] <= (script.get("ended_at") or 0)
            if same_step and within:
                inner.add(str(call.get("id")))
                break
    return inner


def run_behaviour(run: dict[str, Any]) -> dict[str, Any]:
    """The shape of one run, over every session it used."""
    steps = width_max = shells = commands = pipes = sequences = scripts = nested = 0
    widths: list[int] = []
    model_ms = 0.0
    intervals: list[tuple[float, float]] = []
    for session in run.get("sessions") or []:
        calls = list(session.get("tool_calls") or [])
        inner = _nested(calls)
        own = [c for c in calls if str(c.get("id")) not in inner]
        nested += len(inner)
        by_step: dict[str, int] = defaultdict(int)
        for call in own:
            by_step[str(call.get("step_id") or "")] += 1
            if call.get("started_at") and call.get("ended_at"):
                intervals.append((call["started_at"], call["ended_at"]))
            if call.get("tool") == _SHELL:
                shells += 1
                n, p, s = shell_shape(str((call.get("args") or {}).get("command") or ""))
                commands, pipes, sequences = commands + n, pipes + p, sequences + s
            elif call.get("tool") == _SCRIPT:
                scripts += 1
        widths += [n for step, n in by_step.items() if step]
        lm_calls = session.get("lm_calls") or []
        steps += len(lm_calls)
        model_ms += sum(float(c.get("duration_ms") or 0.0) for c in lm_calls)
    width_max = max(widths, default=0)
    tool_ms = _union_ms(intervals)
    semantics = run_semantics(run)
    wall_ms = float(run.get("wall_s") or 0.0) * 1000.0
    return {
        "steps": steps,
        "calls": sum(widths),
        "width": statistics.mean(widths) if widths else 0.0,
        "width_max": width_max,
        "shells": shells,
        "commands_per_shell": commands / shells if shells else 0.0,
        "pipes": pipes,
        "sequences": sequences,
        "scripts": scripts,
        "nested": nested,
        "model_s": model_ms / 1000.0,
        "tool_s": tool_ms / 1000.0,
        "other_s": max(wall_ms - model_ms - tool_ms, 0.0) / 1000.0,
        "wall_s": wall_ms / 1000.0,
        "store_puts": float((run.get("io") or {}).get("store_puts") or 0),
        "store_mb": float((run.get("io") or {}).get("store_put_bytes") or 0) / 1e6,
        "cache_create_k": float((run.get("io") or {}).get("cache_creation_tokens") or 0) / 1e3,
        "cache_read_k": float((run.get("io") or {}).get("cache_read_tokens") or 0) / 1e3,
        **_core_time(run.get("io") or {}),
        **{k: v for k, v in semantics.items() if not isinstance(v, (dict, list))},
    }


def _core_time(io: dict[str, Any]) -> dict[str, float]:
    """clio-core and I/O time of a run: store calls, the history writer, the barrier."""
    return {
        "rpc_s": float(io.get("rpc_ms") or 0.0) / 1e3,
        "rpc_max_ms": float(io.get("rpc_max_ms") or 0.0),
        "writer_wait_s": float(io.get("writer_wait_ms") or 0.0) / 1e3,
        "writer_wait_max_ms": float(io.get("writer_wait_max_ms") or 0.0),
        "writer_run_s": float(io.get("writer_run_ms") or 0.0) / 1e3,
        "barrier_s": float(io.get("barrier_ms") or 0.0) / 1e3,
        "barrier_max_ms": float(io.get("barrier_max_ms") or 0.0),
    }


def run_semantics(run: dict[str, Any]) -> dict[str, Any]:
    """Context, compaction, delegation and tool-return use of one run."""
    sessions = list(run.get("sessions") or [])
    root = next((s for s in sessions if not s.get("depth")), sessions[0] if sessions else {})
    prompts = [p for p in root.get("prompt_tokens") or [] if p]
    compactions = [c for s in sessions for c in s.get("compactions") or []]
    shrink = [c["after"] / c["before"] for c in compactions if c.get("before") and c.get("after")]
    cached = [
        c["checkpoint_cache_read"] / c["checkpoint_input"]
        for c in compactions
        if c.get("checkpoint_input")
    ]
    children = [s for s in sessions if s.get("depth")]
    injections: dict[str, int] = defaultdict(int)
    for session in sessions:
        for source, n in (session.get("injections") or {}).items():
            injections[source] += n
    spill_reads = sum(
        1
        for s in sessions
        for c in s.get("tool_calls") or []
        if SPILL_MARKER in str(c.get("args") or "")
    )
    return {
        "prompt_first_k": (prompts[0] / 1e3) if prompts else 0.0,
        "prompt_max_k": (max(prompts) / 1e3) if prompts else 0.0,
        "compactions": len(compactions),
        "compaction_kept": statistics.median(shrink) if shrink else 0.0,
        "checkpoint_cached": statistics.median(cached) if cached else 0.0,
        "children": len(children),
        "child_models": sorted(
            {str(c.get("model") or "") for s in children for c in s.get("lm_calls") or []}
        ),
        "injections": sum(injections.values()),
        "injections_by_source": dict(injections),
        "spill_reads": spill_reads,
    }


_COLUMNS = (
    ("wall s", "wall_s", "{:.0f}"),
    ("steps", "steps", "{:.0f}"),
    ("calls", "calls", "{:.0f}"),
    ("width", "width", "{:.1f}"),
    ("widest", "width_max", "{:.0f}"),
    ("shell", "shells", "{:.0f}"),
    ("cmds/shell", "commands_per_shell", "{:.1f}"),
    ("pipes", "pipes", "{:.0f}"),
    ("seq", "sequences", "{:.0f}"),
    ("scripts", "scripts", "{:.0f}"),
    ("in scripts", "nested", "{:.0f}"),
    ("model s", "model_s", "{:.0f}"),
    ("tools s", "tool_s", "{:.0f}"),
    ("other s", "other_s", "{:.0f}"),
    ("core puts", "store_puts", "{:.0f}"),
    ("core MB", "store_mb", "{:.1f}"),
    ("cache new k", "cache_create_k", "{:.1f}"),
    ("cache read k", "cache_read_k", "{:.0f}"),
    ("core rpc s", "rpc_s", "{:.1f}"),
    ("core rpc max ms", "rpc_max_ms", "{:.0f}"),
    ("writer wait s", "writer_wait_s", "{:.1f}"),
    ("writer wait max ms", "writer_wait_max_ms", "{:.0f}"),
    ("writer run s", "writer_run_s", "{:.1f}"),
    ("barrier s", "barrier_s", "{:.1f}"),
    ("barrier max ms", "barrier_max_ms", "{:.0f}"),
    ("prompt first k", "prompt_first_k", "{:.0f}"),
    ("prompt max k", "prompt_max_k", "{:.0f}"),
    ("compactions", "compactions", "{:.0f}"),
    ("kept after fold", "compaction_kept", "{:.2f}"),
    ("checkpoint cached", "checkpoint_cached", "{:.2f}"),
    ("children", "children", "{:.0f}"),
    ("injections", "injections", "{:.0f}"),
    ("spill reads", "spill_reads", "{:.0f}"),
)


def render(runs: list[dict[str, Any]]) -> str:
    """A table per task: one row per configuration, medians over its runs."""
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for run in runs:
        grouped[str(run.get("task"))][str(run.get("config"))].append(run_behaviour(run))
    lines = []
    head = "| config | n | " + " | ".join(name for name, _, _ in _COLUMNS) + " |"
    rule = "|" + "---|" * (len(_COLUMNS) + 2)
    for task in sorted(grouped, key=lambda t: (t[0], int(re.sub(r"\D", "", t) or 0))):
        lines += [f"### {task}", "", head, rule]
        for config, rows in sorted(grouped[task].items()):
            cells = [
                fmt.format(statistics.median(r[key] for r in rows)) for _, key, fmt in _COLUMNS
            ]
            lines.append(f"| {config} | {len(rows)} | " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="behaviour", description=__doc__.split("\n")[0])
    parser.add_argument("results", nargs="?", type=Path, default=DEFAULT_RESULTS)
    args = parser.parse_args(argv)
    print(render(load_runs(args.results)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
