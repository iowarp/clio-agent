"""Run a script's tool calls through the same path as the agent's direct calls.

A stub call arrives from the kernel as ``{"type": "call", "id", "tool", "args"}``.
:class:`Dispatcher` runs it on a worker thread, in a copy of the ``run_code`` call's
context, through the agent's instrumented tool, which is what
:meth:`ClioReAct._run_tool` does for a direct call: the observer records it (as a child
of the ``run_code`` call), the permission gate decides it (with the script's grant
bound, :mod:`.grants`), the circuit breaker counts it. It never runs on the
application's event loop, where an MCP call would wait on itself.

The script gets the call's structured value: an MCP call's ``structuredContent`` (else
its text), read from the observer's completed notification
(:data:`clio_agent.tools.tool_observation.RESULT_SINK`), or a native tool's return
value. The model never sees nested results; it sees what the script prints.

Bounds: at most ``code.max_nested_calls`` calls per script, and the per-server limit
a step's calls share (:mod:`clio_agent.gact.agents.tool_concurrency`).
"""

from __future__ import annotations

import contextvars
import inspect
import json
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from clio_agent import conf
from clio_agent.gact.agents.tool_concurrency import WORKERS, ServerGates
from clio_agent.gact.code_mode.stubs import NOT_IN_SCRIPTS, tool_group

__all__ = ["CallRecord", "Dispatcher", "max_nested_calls"]

_SEARCH = "__search__"


def max_nested_calls() -> int:
    """Tool calls one script may make."""
    return conf.resolve(
        "code.max_nested_calls", env="CLIO_CODE_MAX_NESTED_CALLS", default=500, cast=conf.as_int
    )


@dataclass
class CallRecord:
    """One nested call, for the ``run_code`` result."""

    tool: str
    ok: bool
    duration_ms: int
    error: str = ""


def _mcp_value(result: Any) -> tuple[bool, Any]:
    """``(found, value)`` from an observer result: structured content, else its text."""
    if not isinstance(result, Mapping) or not ({"content", "structuredContent"} & set(result)):
        return False, None
    structured = result.get("structuredContent")
    if structured is not None:
        return True, structured
    texts = [
        str(block.get("text") or "")
        for block in result.get("content") or []
        if isinstance(block, Mapping) and block.get("type") == "text"
    ]
    return True, "\n".join(texts)


def _native_value(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in "[{":
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


class Dispatcher:
    """The host side of one script's tool calls."""

    def __init__(
        self,
        tools: Mapping[str, Any],
        servers: tuple[str, ...],
        context: contextvars.Context,
        search: Callable[[str, int], Any],
    ) -> None:
        self.tools = tools
        self.servers = servers
        self.context = context
        self.search = search
        self.budget = max_nested_calls()
        self.gates = ServerGates()
        self.pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="clio-code-call")
        self.records: list[CallRecord] = []
        self.started = 0
        self.lock = threading.Lock()

    def submit(self, message: Mapping[str, Any], reply: Callable[[dict[str, Any]], None]) -> None:
        """Start one call; ``reply`` gets the kernel's ``result`` message when it ends."""
        call_id = message.get("id")
        tool = str(message.get("tool") or "")
        args = message.get("args") if isinstance(message.get("args"), Mapping) else {}
        if tool == _SEARCH:
            query = str(args.get("query") or "")
            reply({"type": "result", "id": call_id, "ok": True, "value": self.search(query, 5)})
            return
        refusal = self._refusal(tool)
        if refusal:
            reply({"type": "result", "id": call_id, "ok": False, "error": refusal})
            return
        context = self.context.copy()

        def done(outcome: tuple[bool, Any, str]) -> None:
            ok, value, error = outcome
            reply({"type": "result", "id": call_id, "ok": ok, "value": value, "error": error})

        future = self.pool.submit(context.run, self._run, tool, dict(args))
        future.add_done_callback(lambda f: done(f.result()))

    def _refusal(self, tool: str) -> str:
        if tool not in self.tools or tool in NOT_IN_SCRIPTS:
            return f"No tool named {tool!r} can be called from a script; see tools.search()."
        with self.lock:
            if self.started >= self.budget:
                return (
                    f"This script reached its limit of {self.budget} tool calls "
                    "(code.max_nested_calls); split the work across scripts."
                )
            self.started += 1
        return ""

    def _run(self, tool: str, args: dict[str, Any]) -> tuple[bool, Any, str]:
        from clio_agent.gact.artifacts.observer_bridge import MODEL_CALL_SLOT  # noqa: PLC0415
        from clio_agent.runtime.fresh_loop import run_in_new_loop  # noqa: PLC0415
        from clio_agent.tools.tool_observation import RESULT_SINK  # noqa: PLC0415

        target = self.tools[tool]
        group, _ = tool_group(tool, self.servers)
        sink: list[tuple[str | None, Any]] = []
        MODEL_CALL_SLOT.set({})  # this call's own observer id, not the script's
        RESULT_SINK.set(sink)
        started = time.monotonic()
        with self.gates(group or tool):
            try:
                if inspect.iscoroutinefunction(getattr(target, "func", None)):
                    value = run_in_new_loop(target.acall(**args))
                else:
                    value = target(**args)
                ok, error = True, ""
            except Exception as exc:  # noqa: BLE001 - the script receives the error
                value, ok, error = None, False, f"{type(exc).__name__}: {exc}"
        if ok and sink:
            sink_error, observed = sink[-1]
            found, structured = _mcp_value(observed)
            if sink_error:
                ok, error = False, str(sink_error)
            elif found:
                value = structured
        if ok and not sink:
            value = _native_value(value)
        duration = int((time.monotonic() - started) * 1000)
        with self.lock:
            self.records.append(CallRecord(tool, ok, duration, error[:500]))
        return ok, value if ok else None, error

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
