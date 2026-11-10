"""Per-run metrics from a run JSON (CLIO runner or the Claude Code reference).

``uv run --frozen --no-sync python -m scripts.eval.tool_calling.metrics RUN.json [...]``

Definitions (the plan's "Evaluation" section):

- **model steps**: ``lm.call`` records, for the root session and per child.
- **calls per step**: tool calls attributed to a step (same step span as the
  model call) divided by model steps; also the largest batch.
- **prompt tokens per step**: ``usage.input_tokens`` as *uncached* and
  ``usage.cache_read_tokens`` as *cached*, averaged over model steps. On the
  ``claude_code`` engine (Anthropic usage) ``input_tokens`` already excludes
  cache reads; for a provider whose ``input_tokens`` includes them, pass
  ``input_includes_cache=True`` and uncached becomes ``input - cache_read``.
- **wall time**: the run's ``wall_s``.
- **TTFT / decode**: ``ttft_ms`` and ``duration_ms - ttft_ms`` where recorded.
- **batch wall vs slowest call**: for each step with two or more timed tool
  calls, the span from the first start to the last end, against the slowest
  single call; the ratio is 1.0 when the batch ran fully in parallel and
  approaches the batch size when it ran one call at a time.
- **step overhead**: per step, the time from this model call's start to the
  next one's, minus model time and minus the union of the step's tool-call
  intervals: time spent outside model and tools.
- **prefix resets**: ``provider.stateful`` stream-audit rows by ``reason``
  (``first_call``, ``prefix_mismatch``, ...).
- **tool counts**: ``search_tools``, ``run_code``, ``plan_update``, ``write_todos``.
- **completion guard**: the run ended with a final answer, not an error, a
  cancellation, a timeout or an empty reply.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

TRACKED_TOOLS = ("search_tools", "run_code", "plan_update", "write_todos")


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 3) if values else None


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def tool_matches(name: str, wanted: str) -> bool:
    """A tool name equals ``wanted`` or is a namespaced form of it (``core_write_todos``)."""
    return name == wanted or name.endswith(f"_{wanted}") or name.endswith(f"__{wanted}")


def _union_ms(intervals: list[tuple[float, float]]) -> float:
    total, end = 0.0, None
    for start, stop in sorted(intervals):
        if end is None or start > end:
            total += stop - start
            end = stop
        elif stop > end:
            total += stop - end
            end = stop
    return total * 1000.0


def _steps(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Each model call with the tool calls of its step."""
    tools_by_step: dict[str, list[dict[str, Any]]] = {}
    for call in session.get("tool_calls") or []:
        if call.get("step_id"):
            tools_by_step.setdefault(call["step_id"], []).append(call)
    calls = sorted(
        session.get("lm_calls") or [],
        key=lambda c: (c.get("started_at") or c.get("ended_at") or 0.0, c.get("id") or ""),
    )
    seen: set[str] = set()
    out = []
    for call in calls:
        step_id = call.get("step_id") or ""
        tools = tools_by_step.get(step_id, []) if step_id and step_id not in seen else []
        seen.add(step_id)
        out.append({"lm": call, "tools": tools})
    return out


def _batch(tools: list[dict[str, Any]]) -> tuple[float, float] | None:
    timed = [t for t in tools if t.get("started_at") and t.get("ended_at")]
    if len(timed) < 2:
        return None
    wall = (max(t["ended_at"] for t in timed) - min(t["started_at"] for t in timed)) * 1000.0
    slowest = max(t.get("duration_ms") or (t["ended_at"] - t["started_at"]) * 1000.0 for t in timed)
    return wall, slowest


def session_metrics(
    session: dict[str, Any], *, input_includes_cache: bool = False
) -> dict[str, Any]:
    """Metrics of one normalized session."""
    steps = _steps(session)
    uncached, cached, output, ttft, decode, latency = [], [], [], [], [], []
    calls_per_step = []
    batch_wall = batch_slowest = 0.0
    batches = 0
    overheads: list[float] = []
    for i, step in enumerate(steps):
        lm = step["lm"]
        usage = lm.get("usage") or {}
        inp, read = int(usage.get("input_tokens") or 0), int(usage.get("cache_read_tokens") or 0)
        uncached.append(max(0, inp - read) if input_includes_cache else inp)
        cached.append(read)
        output.append(int(usage.get("output_tokens") or 0))
        calls_per_step.append(len(step["tools"]))
        if lm.get("duration_ms") is not None:
            latency.append(float(lm["duration_ms"]))
        if lm.get("ttft_ms") is not None:
            ttft.append(float(lm["ttft_ms"]))
            if lm.get("duration_ms") is not None:
                decode.append(float(lm["duration_ms"]) - float(lm["ttft_ms"]))
        batch = _batch(step["tools"])
        if batch is not None:
            batches += 1
            batch_wall += batch[0]
            batch_slowest += batch[1]
        start = lm.get("started_at")
        if start is None or lm.get("duration_ms") is None:
            continue
        nxt = steps[i + 1]["lm"].get("started_at") if i + 1 < len(steps) else None
        tool_iv = [
            (t["started_at"], t["ended_at"])
            for t in step["tools"]
            if t.get("started_at") and t.get("ended_at")
        ]
        end = (
            nxt if nxt is not None else max([lm.get("ended_at") or start, *(e for _, e in tool_iv)])
        )
        window_ms = (end - start) * 1000.0
        overheads.append(max(0.0, window_ms - float(lm["duration_ms"]) - _union_ms(tool_iv)))
    tool_names = [str(c.get("tool") or "") for c in session.get("tool_calls") or []]
    quarter = max(1, len(steps) // 4)
    prompt = [u + c for u, c in zip(uncached, cached, strict=True)]
    return {
        "session_id": session.get("session_id"),
        "steps": len(steps),
        "tool_calls": len(tool_names),
        "calls_per_step": {"mean": _mean(calls_per_step), "max": max(calls_per_step, default=0)},
        "prompt_tokens_per_step": {
            "uncached_mean": _mean(uncached),
            "cached_mean": _mean(cached),
            "uncached_total": sum(uncached),
            "cached_total": sum(cached),
        },
        "output_tokens": sum(output),
        "ttft_ms": {"median": _median(ttft), "n": len(ttft)},
        "decode_ms": {"median": _median(decode), "n": len(decode)},
        "model_latency_ms": {"median": _median(latency), "total": round(sum(latency), 1)},
        "batch": {
            "steps": batches,
            "wall_ms": round(batch_wall, 1),
            "slowest_ms": round(batch_slowest, 1),
            "ratio": round(batch_wall / batch_slowest, 3) if batch_slowest else None,
        },
        "step_overhead_ms": {
            "total": round(sum(overheads), 1) if overheads else None,
            "mean": _mean(overheads),
            "n": len(overheads),
        },
        "trend": {
            "latency_ms_first_quarter": _mean(latency[:quarter]),
            "latency_ms_last_quarter": _mean(latency[-quarter:]),
            "prompt_tokens_first_quarter": _mean([float(x) for x in prompt[:quarter]]),
            "prompt_tokens_last_quarter": _mean([float(x) for x in prompt[-quarter:]]),
        },
        "tool_counts": {
            name: sum(1 for t in tool_names if tool_matches(t, name)) for name in TRACKED_TOOLS
        },
        "events": session.get("events") or {},
    }


def prefix_resets(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Stateful resets by reason (``provider.stateful`` rows that carry one)."""
    return dict(
        sorted(
            Counter(
                str(r.get("reason"))
                for r in rows
                if r.get("stage") == "provider.stateful" and r.get("reason")
            ).items()
        )
    )


def _sum_dicts(rows: list[dict[str, Any]]) -> dict[str, int]:
    out: Counter[str] = Counter()
    for row in rows:
        out.update({k: int(v or 0) for k, v in row.items()})
    return dict(out)


def run_metrics(run: dict[str, Any], *, input_includes_cache: bool = False) -> dict[str, Any]:
    """Metrics of one run: root, per child, and totals."""
    sessions = run.get("sessions") or []
    per = [session_metrics(s, input_includes_cache=input_includes_cache) for s in sessions]
    root = per[0] if per else session_metrics({})
    all_steps = sum(m["steps"] for m in per)
    all_calls = sum(m["tool_calls"] for m in per)
    uncached = sum(m["prompt_tokens_per_step"]["uncached_total"] for m in per)
    cached = sum(m["prompt_tokens_per_step"]["cached_total"] for m in per)
    outcome = run.get("outcome") or {}
    return {
        "config": run.get("config"),
        "task": run.get("task"),
        "run_index": run.get("run_index"),
        "source": run.get("source", "clio"),
        "handles": run.get("handles") or [s.get("session_id") for s in sessions],
        "completion": {"valid": bool(outcome.get("final")), "reason": outcome.get("reason")},
        "wall_s": run.get("wall_s"),
        "root": root,
        "children": per[1:],
        "totals": {
            "steps": all_steps,
            "child_steps": all_steps - root["steps"],
            "children": len(per) - 1,
            "tool_calls": all_calls,
            "calls_per_step": round(all_calls / all_steps, 3) if all_steps else None,
            "uncached_per_step": round(uncached / all_steps, 1) if all_steps else None,
            "cached_per_step": round(cached / all_steps, 1) if all_steps else None,
            "output_tokens": sum(m["output_tokens"] for m in per),
            "tool_counts": _sum_dicts([m["tool_counts"] for m in per]),
            "events": _sum_dicts([m["events"] for m in per]),
        },
        "prefix_resets": prefix_resets(run.get("stream_audit") or [])
        if run.get("stream_audit_enabled", True)
        else None,
        "usage_windows": run.get("usage_windows") or {},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="metrics", description="Print per-run metrics.")
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--input-includes-cache", action="store_true")
    args = parser.parse_args(argv)
    for path in args.runs:
        run = json.loads(path.read_text(encoding="utf-8"))
        print(
            json.dumps(run_metrics(run, input_includes_cache=args.input_includes_cache), indent=1)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
