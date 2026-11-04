"""The semantic record from sources other than a live export, and its ``rebuild`` command.

* The Claude Code reference: its saved stream (``run-N.stream.jsonl``, each line
  ``{"recv", "event"}``) gives the same record shape as a CLIO run
  (:func:`~scripts.eval.tool_calling.semantic.semantic_record`): per model call its
  usage and the tool results it received, per step the visible text and each call
  with its output. A Task subagent's events (``parent_tool_use_id``) form a child.
* A stored CLIO run whose export was not kept: its sessions' semantic traces
  (``<state>/traces/semantic_traces/<sid>.semantic.jsonl``, by the result's
  ``handles``) stand in for the export's events. Messages, skills and the work state
  are not in a trace, so those parts stay empty and ``unread`` says so.

``uv run --frozen --no-sync python -m scripts.eval.tool_calling.semantic_sources rebuild
<results>/<config>/<task>`` writes ``run-N.semantic.json`` and ``run-N.read.md`` for every
run there that has none.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from scripts.eval.tool_calling import semantic
from scripts.eval.tool_calling.paths import is_run_file

TRACES = Path("/root/.local/state/clio-agent/traces/semantic_traces")
__all__ = ["claude_code_record", "trace_export"]


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        str(block.get("text") or "") for block in content or [] if isinstance(block, dict)
    )


class _Thread:
    """One Claude Code agent thread (the root, or one Task subagent)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.steps: list[dict[str, Any]] = []
        self.incoming: list[dict[str, Any]] = []
        self.by_id: dict[str, dict[str, Any]] = {}

    def start(self, at: float, usage: dict[str, Any]) -> None:
        self.calls.append({
            "started_at": at, "duration_ms": None, "usage": _usage(usage),
            "system_chars": 0, "tool_chars": 0, "tools": [], "incoming": self.incoming,
            "reply_chars": 0, "reasoning_chars": 0,
        })  # fmt: skip
        self.calls[-1]["prompt_tokens"] = sum(
            self.calls[-1]["usage"][k]
            for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens")
        )
        self.incoming = []
        self.steps.append({"index": len(self.steps), "text": "", "is_finish": False,
                           "calls": [], "at": at + 1e-6})  # fmt: skip

    def block(self, block: dict[str, Any]) -> None:
        if not self.calls:
            self.start(0.0, {})
        kind = block.get("type")
        if kind == "text":
            self.steps[-1]["text"] += str(block.get("text") or "")
            self.calls[-1]["reply_chars"] += len(str(block.get("text") or ""))
        elif kind == "thinking":
            self.calls[-1]["reasoning_chars"] += len(str(block.get("thinking") or ""))
        elif kind == "tool_use":
            call = {"name": str(block.get("name") or ""), "args": block.get("input") or {},
                    "is_error": False, "output_chars": 0, "output": ""}  # fmt: skip
            self.steps[-1]["calls"].append(call)
            self.by_id[str(block.get("id") or "")] = call

    def result(self, block: dict[str, Any]) -> None:
        text = _text(block.get("content"))
        call = self.by_id.get(str(block.get("tool_use_id") or ""))
        if call is not None:
            call.update(output=text, output_chars=len(text), is_error=bool(block.get("is_error")))
        self.incoming.append({"kind": "tool_result", "name": call["name"] if call else "",
                              "is_error": bool(block.get("is_error")), "chars": len(text),
                              "text": text})  # fmt: skip

    def session(self, sid: str, depth: int, task: str) -> dict[str, Any]:
        final = next((s["text"] for s in reversed(self.steps) if s["text"]), "")
        return {
            "session_id": sid, "agent": "claude-code" if not depth else "task",
            "depth": depth, "attribution": "root" if not depth else "task_subagent",
            "turns": [{"at": None, "text": task}] if task else [],
            "model_calls": self.calls, "steps": self.steps, "skills": [],
            "compactions": [], "final_text": final,
        }  # fmt: skip


def _usage(raw: dict[str, Any]) -> dict[str, int]:
    return {
        "input_tokens": int(raw.get("input_tokens") or 0),
        "output_tokens": int(raw.get("output_tokens") or 0),
        "cache_read_tokens": int(raw.get("cache_read_input_tokens") or 0),
        "cache_write_tokens": int(raw.get("cache_creation_input_tokens") or 0),
        "reasoning_tokens": int(
            (raw.get("output_tokens_details") or {}).get("thinking_tokens") or 0
        ),
    }


def claude_code_record(rows: list[dict[str, Any]], prompt: str = "") -> dict[str, Any]:
    """The semantic record of a Claude Code run from its saved stream rows."""
    threads: dict[str, _Thread] = {"": _Thread()}
    tasks: dict[str, str] = {}
    for row in rows:
        at, event = float(row.get("recv") or 0.0), row.get("event") or {}
        thread = threads.setdefault(str(event.get("parent_tool_use_id") or ""), _Thread())
        kind = event.get("type")
        if kind == "stream_event":
            inner = event.get("event") or {}
            if inner.get("type") == "message_start":
                thread.start(at, (inner.get("message") or {}).get("usage") or {})
            elif inner.get("type") == "message_delta" and thread.calls:
                usage = _usage(inner.get("usage") or {})
                thread.calls[-1]["usage"]["output_tokens"] = usage["output_tokens"]
                thread.calls[-1]["usage"]["reasoning_tokens"] = usage["reasoning_tokens"]
        elif kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                thread.block(block)
                if block.get("type") == "tool_use" and block.get("name") in ("Task", "Agent"):
                    tasks[str(block.get("id"))] = str(
                        (block.get("input") or {}).get("prompt") or ""
                    )
        elif kind == "user":
            content = (event.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    thread.result(block)
    root = threads.pop("").session("claude-code", 0, prompt)
    children = [t.session(key, 1, tasks.get(key, "")) for key, t in threads.items()]
    return {"schema": "clio.eval.semantic.v1", "sessions": [root, *children],
            "work": {}, "plan_answers": [], "exact_prompt": {}, "unread": [
                "Claude Code: no system prompt or tool list in the stream"]}  # fmt: skip


def trace_export(handles: list[str], traces: Path = TRACES) -> tuple[dict[str, Any], list[str]]:
    """An export-shaped body from the sessions' semantic traces, and what is missing."""
    unread = ["messages, skills, work state and exact prompt (rebuilt from traces)"]
    bodies = []
    for depth, sid in enumerate(handles):
        path = traces / f"{sid}.semantic.jsonl"
        events = []
        if path.is_file():
            events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        else:
            unread.append(f"trace of {sid}")
        bodies.append({
            "session": {"id": sid}, "semantic_events": events, "messages": [],
            "relationship": {"session_id": sid, "depth": min(depth, 1),
                             "attribution": "root" if not depth else "child"},
        })  # fmt: skip
    root, *children = bodies or [{"session": {}, "semantic_events": [], "messages": []}]
    return {**root, "children": children}, unread


def rebuild(folder: Path) -> list[Path]:
    """Write the semantic record and reading report of each run in ``folder`` lacking one."""
    written = []
    for path in sorted(folder.glob("run-*.json")):
        if not is_run_file(path) or path.with_suffix(".semantic.json").is_file():
            continue
        result = json.loads(path.read_text(encoding="utf-8"))
        stream = path.with_suffix(".stream.jsonl")
        if result.get("source") == "claude_code" or stream.is_file():
            rows = [json.loads(line) for line in stream.read_text().splitlines() if line.strip()]
            record = claude_code_record(rows, str(result.get("prompt") or ""))
        else:
            export, unread = trace_export(list(result.get("handles") or []))
            record = semantic.semantic_record(export)
            record["unread"] = unread
        path.with_suffix(".semantic.json").write_text(json.dumps(record, indent=1, default=str))
        header = {k: result.get(k) for k in ("task", "run_index", "config", "wall_s", "active_s")}
        path.with_suffix(".read.md").write_text(semantic.reading_report(record, header))
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="semantic_sources", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("rebuild", help="write missing semantic records in result folders")
    cmd.add_argument("folders", nargs="+", type=Path)
    args = parser.parse_args(argv)
    for folder in args.folders:
        for path in rebuild(folder):
            print(f"wrote {path.with_suffix('.read.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
