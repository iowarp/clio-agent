"""The Claude Code reference: P1 to P4 with ``claude -p`` on the same fixtures and model.

``uv run --frozen --no-sync python -m scripts.eval.tool_calling.claude_code_ref run``
``uv run --frozen --no-sync python -m scripts.eval.tool_calling.claude_code_ref parse STREAM.jsonl``

Each run copies the task's fixture folder, runs ``claude -p PROMPT
--output-format stream-json --verbose --include-partial-messages --model M``
there, keeps the raw stream (``run-<n>.stream.jsonl``, each line
``{"recv": <epoch s>, "event": {...}}``) and writes a run JSON in the shape
``collect.py`` produces, so ``metrics``, ``grade`` and ``summarize`` read it
unchanged.

Stream events carry no clocks, so timing comes from when each line was
received (``timing_source: client_receive``). A model step is one assistant
message id; it starts when its thread's previous tool results arrived (or the
process started, or the parent's ``Task`` call was made) and ends at
``message_stop``. TTFT is the first ``content_block_delta`` of the message.
Subagent messages (``parent_tool_use_id`` set) form child sessions.

Claude Code loads ``CLAUDE.md`` from the working folder's ancestors. Work
folders under this checkout inherit its ``CLAUDE.md``; each result lists the
ancestor instruction files found, and ``--work-root`` outside the checkout
avoids them.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from scripts.eval.tool_calling import tasks
from scripts.eval.tool_calling.paths import DEFAULT_FIXTURES, DEFAULT_RESULTS, DEFAULT_WORK
from scripts.eval.tool_calling.session_run import RESULT_SCHEMA, prepare_workdir, write_json
from scripts.eval.tool_calling.usage_guard import DEFAULT_STOP_AT

CONFIG = "claude-code"
_INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md", "CLAUDE.local.md")


def _usage(raw: dict[str, Any] | None) -> dict[str, int]:
    raw = raw or {}
    return {
        "input_tokens": int(raw.get("input_tokens") or 0),
        "output_tokens": int(raw.get("output_tokens") or 0),
        "cache_read_tokens": int(raw.get("cache_read_input_tokens") or 0),
        "cache_write_tokens": int(raw.get("cache_creation_input_tokens") or 0),
        "reasoning_tokens": 0,
    }


def _merge_usage(old: dict[str, int], new: dict[str, int]) -> dict[str, int]:
    return {k: max(old.get(k, 0), v) for k, v in new.items()}


def rate_windows(info: dict[str, Any]) -> dict[str, float]:
    """``{window: utilization fraction}`` from one ``rate_limit_info``."""
    windows = info.get("unifiedWindows")
    if isinstance(windows, dict):
        return {
            str(k): float(v["utilization"])
            for k, v in windows.items()
            if isinstance(v, dict) and isinstance(v.get("utilization"), (int, float))
        }
    if isinstance(info.get("utilization"), (int, float)) and info.get("rateLimitType"):
        return {str(info["rateLimitType"]): float(info["utilization"])}
    return {}


class _Parse:
    """Fold received stream-json events into sessions."""

    def __init__(self, start: float) -> None:
        self.start = start
        self.boundary: dict[str, float] = {"": start}
        self.parent_of: dict[str, str] = {"": ""}
        self.current: dict[str, str] = {}
        self.messages: dict[str, dict[str, Any]] = {}
        self.tools: dict[str, dict[str, Any]] = {}
        self.result: dict[str, Any] | None = None
        self.rate_limits: list[dict[str, Any]] = []
        self.init: dict[str, Any] = {}
        self.partial = False

    def _message(self, mid: str, thread: str, at: float) -> dict[str, Any]:
        if mid not in self.messages:
            self.messages[mid] = {
                "id": mid,
                "thread": thread,
                "request_start": self.boundary.get(thread, self.start),
                "first_token": None,
                "last_seen": at,
                "usage": _usage(None),
                "model": "",
            }
        return self.messages[mid]

    def stream_event(self, at: float, thread: str, event: dict[str, Any]) -> None:
        self.partial = True
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            row = self._message(str(message.get("id") or f"anon-{len(self.messages)}"), thread, at)
            row["usage"] = _merge_usage(row["usage"], _usage(message.get("usage")))
            row["model"] = str(message.get("model") or row["model"])
            self.current[thread] = row["id"]
            return
        row = self.messages.get(self.current.get(thread, ""))
        if row is None:
            return
        if kind == "content_block_delta" and row["first_token"] is None:
            row["first_token"] = at
        elif kind == "message_delta":
            row["usage"] = _merge_usage(row["usage"], _usage(event.get("usage")))
        elif kind == "message_stop":
            row["last_seen"] = at

    def assistant(self, at: float, thread: str, message: dict[str, Any]) -> None:
        row = self._message(str(message.get("id") or f"anon-{len(self.messages)}"), thread, at)
        row["usage"] = _merge_usage(row["usage"], _usage(message.get("usage")))
        row["model"] = str(message.get("model") or row["model"])
        row["last_seen"] = max(row["last_seen"], at)
        for block in message.get("content") or []:
            if block.get("type") != "tool_use" or block.get("id") in self.tools:
                continue
            tid = str(block.get("id"))
            self.tools[tid] = {
                "id": tid,
                "step_id": row["id"],
                "thread": thread,
                "tool": str(block.get("name") or ""),
                "args": block.get("input") if isinstance(block.get("input"), dict) else {},
                "started_at": at,
                "ended_at": None,
                "duration_ms": None,
                "ok": None,
            }
            self.boundary[tid] = at
            self.parent_of[tid] = thread

    def user(self, at: float, thread: str, message: dict[str, Any]) -> None:
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool = self.tools.get(str(block.get("tool_use_id")))
            if tool is not None and tool["ended_at"] is None:
                tool["ended_at"] = at
                tool["duration_ms"] = round((at - tool["started_at"]) * 1000.0, 1)
                tool["ok"] = not bool(block.get("is_error"))
            self.boundary[thread] = at

    def feed(self, at: float, event: dict[str, Any]) -> None:
        kind = event.get("type")
        thread = str(event.get("parent_tool_use_id") or "")
        if kind == "stream_event":
            self.stream_event(at, thread, event.get("event") or {})
        elif kind == "assistant":
            self.assistant(at, thread, event.get("message") or {})
        elif kind == "user":
            self.user(at, thread, event.get("message") or {})
        elif kind == "rate_limit_event":
            self.rate_limits.append({"recv": at, **(event.get("rate_limit_info") or {})})
        elif kind == "result":
            self.result = event
        elif kind == "system" and event.get("subtype") == "init":
            self.init = {
                k: event.get(k) for k in ("model", "session_id", "cwd", "claude_code_version")
            }

    def _lm_row(self, row: dict[str, Any]) -> dict[str, Any]:
        start, end = row["request_start"], row["last_seen"]
        return {
            "id": row["id"],
            "step_id": row["id"],
            "started_at": start,
            "ended_at": end,
            "duration_ms": round((end - start) * 1000.0, 1),
            "ttft_ms": round((row["first_token"] - start) * 1000.0, 1)
            if row["first_token"] is not None
            else None,
            "usage": row["usage"],
            "finish_reason": "",
            "status": "completed",
            "model": row["model"],
            "error": "",
        }

    def sessions(self) -> list[dict[str, Any]]:
        threads = ["", *sorted({m["thread"] for m in self.messages.values()} - {""})]
        out = []
        for thread in threads:
            depth, cursor = 0, thread
            while cursor:
                depth, cursor = depth + 1, self.parent_of.get(cursor, "")
            out.append(
                {
                    "session_id": thread or "main",
                    "parent_session_id": (self.parent_of.get(thread, "") or "main")
                    if thread
                    else "",
                    "depth": depth,
                    "attribution": "subagent" if thread else "root",
                    "agent": "",
                    "lm_calls": sorted(
                        (self._lm_row(m) for m in self.messages.values() if m["thread"] == thread),
                        key=lambda c: c["started_at"],
                    ),
                    "tool_calls": sorted(
                        (
                            {k: v for k, v in t.items() if k != "thread"}
                            for t in self.tools.values()
                            if t["thread"] == thread
                        ),
                        key=lambda c: c["started_at"],
                    ),
                    "steps": [],
                    "events": {},
                }
            )
        return out

    def outcome(self) -> dict[str, Any]:
        result = self.result or {}
        text = str(result.get("result") or "").strip()
        if not result:
            reason = "no_result"
        elif result.get("is_error") or result.get("subtype") != "success":
            reason = str(result.get("subtype") or "error")
        elif not text:
            reason = "empty_reply"
        else:
            reason = "final_answer"
        return {
            "final": reason == "final_answer",
            "reason": reason,
            "stop_reason": "",
            "final_text": text,
        }


def parse_stream(received: Iterable[tuple[float, dict[str, Any]]], start: float) -> dict[str, Any]:
    """Received ``(time, event)`` pairs to the run fields metrics read."""
    state = _Parse(start)
    last = start
    for at, event in received:
        state.feed(at, event)
        last = max(last, at)
    result = state.result or {}
    return {
        "sessions": state.sessions(),
        "outcome": state.outcome(),
        "ended_at": last,
        "claude_result": {
            k: result.get(k)
            for k in (
                "duration_ms",
                "duration_api_ms",
                "num_turns",
                "total_cost_usd",
                "usage",
                "modelUsage",
            )
        },
        "rate_limits": state.rate_limits,
        "init": state.init,
        "timing_source": "client_receive",
        "partial_messages": state.partial,
    }


def ancestor_instructions(folder: Path) -> list[str]:
    """Instruction files Claude Code would load from ``folder``'s ancestors."""
    found = []
    for parent in [folder, *folder.parents]:
        found += [str(parent / n) for n in _INSTRUCTION_FILES if (parent / n).is_file()]
    return found


def seven_day_utilization(rate_limits: list[dict[str, Any]]) -> float | None:
    values = [
        u
        for info in rate_limits
        for k, u in rate_windows(info).items()
        if k.startswith("seven_day")
    ]
    return max(values) if values else None


def run_once(
    args: argparse.Namespace, manifest: dict[str, Any], task_id: str, index: int
) -> dict[str, Any]:
    task = tasks.task(task_id, manifest)
    workdir = prepare_workdir(
        args.fixtures,
        tasks.fixture_dir(task_id, manifest),
        args.work_root / CONFIG / task_id / f"run-{index}",
    )
    command = [
        args.claude, "-p", task.prompt, "--output-format", "stream-json", "--verbose",
        "--include-partial-messages", "--model", args.model,
        "--permission-mode", args.permission_mode, "--no-session-persistence",
    ]  # fmt: skip
    if args.allowed_tools:
        # Pre-approved tools, for where skipping permissions is refused (as root).
        command += ["--allowedTools", args.allowed_tools]
    target = args.results / CONFIG / task_id / f"run-{index}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    received: list[tuple[float, dict[str, Any]]] = []
    start = time.time()
    timed_out = False
    with (
        target.with_suffix(".stream.jsonl").open("w", encoding="utf-8") as raw,
        subprocess.Popen(
            command, cwd=workdir, stdout=subprocess.PIPE, text=True, bufsize=1
        ) as proc,
    ):
        assert proc.stdout is not None
        for line in proc.stdout:
            at = time.time()
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            received.append((at, event))
            raw.write(json.dumps({"recv": at, "event": event}) + "\n")
            if at - start > args.timeout:
                timed_out = True
                proc.kill()
                break
    parsed = parse_stream(received, start)
    if timed_out:
        parsed["outcome"] = {**parsed["outcome"], "final": False, "reason": "timed_out"}
    result = {
        "schema": RESULT_SCHEMA,
        "source": "claude_code",
        "config": CONFIG,
        "task": task_id,
        "run_index": index,
        "prompt": task.prompt,
        "command": command,
        "model": {"provider_id": "claude-code-cli", "model_id": args.model},
        "workdir": str(workdir),
        "ancestor_instructions": ancestor_instructions(workdir),
        "started_at": start,
        "wall_s": round(parsed["ended_at"] - start, 3),
        "handles": [parsed["init"].get("session_id") or ""],
        "exit_code": proc.returncode,
        "stream_audit_enabled": False,
        **parsed,
    }
    write_json(target, result)
    return result


def cmd_run(args: argparse.Namespace) -> int:
    manifest = json.loads((args.fixtures / "manifest.json").read_text(encoding="utf-8"))
    if shutil.which(args.claude) is None:
        raise SystemExit(f"{args.claude!r} is not on PATH")
    utilization: float | None = None
    for task_id in tasks.select(args.tasks):
        for index in range(1, args.n + 1):
            target = args.results / CONFIG / task_id / f"run-{index}.json"
            if target.is_file() and not args.force:
                continue
            if utilization is not None and utilization >= args.stop_at:
                print(f"STOPPED: 7-day window at {utilization:.1%} >= {args.stop_at:.1%}")
                return 3
            result = run_once(args, manifest, task_id, index)
            if result["ancestor_instructions"]:
                print(f"  note: inherited instructions {result['ancestor_instructions']}")
            utilization = seven_day_utilization(result["rate_limits"]) or utilization
            print(f"done {task_id} run {index}: {result['outcome']['reason']}, {result['wall_s']}s")
    return 0


def cmd_parse(args: argparse.Namespace) -> int:
    rows = [
        json.loads(line) for line in args.stream.read_text(encoding="utf-8").splitlines() if line
    ]
    received = [(float(r["recv"]), r["event"]) for r in rows]
    start = args.start if args.start is not None else (received[0][0] if received else 0.0)
    print(json.dumps(parse_stream(received, start), indent=1, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude_code_ref", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--tasks", default=",".join(tasks.REFERENCE_TASKS))
    run.add_argument("--n", type=int, default=3)
    run.add_argument("--model", default="claude-sonnet-5-5")
    run.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    run.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    run.add_argument("--work-root", type=Path, default=DEFAULT_WORK)
    run.add_argument("--claude", default="claude")
    run.add_argument("--permission-mode", default="bypassPermissions")
    run.add_argument(
        "--allowed-tools",
        default="",
        help="tools pre-approved with --allowedTools (comma-separated), e.g. Bash,Read,Grep,Glob",
    )
    run.add_argument("--timeout", type=float, default=3600.0)
    run.add_argument("--stop-at", type=float, default=DEFAULT_STOP_AT)
    run.add_argument("--force", action="store_true")
    parse = sub.add_parser("parse", help="re-parse a saved run-<n>.stream.jsonl")
    parse.add_argument("stream", type=Path)
    parse.add_argument("--start", type=float, default=None)
    args = parser.parse_args(argv)
    if args.command == "run":
        args.fixtures = args.fixtures.resolve()
        args.work_root = args.work_root.resolve()
        return cmd_run(args)
    return cmd_parse(args)


if __name__ == "__main__":
    sys.exit(main())
