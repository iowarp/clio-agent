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

No count bounds a script's calls (taskable run_code CT3); the per-server limit applies
to this script's own calls (:mod:`clio_agent.gact.agents.tool_concurrency`).
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

from clio_agent.gact.agents.tool_concurrency import ServerGates, call_workers
from clio_agent.gact.code_mode.stubs import tool_group

__all__ = ["CONCURRENCY_GROUP_ATTR", "CallRecord", "Dispatcher"]

#: The attribute a native tool's callable sets to share a concurrency cap with other tools.
CONCURRENCY_GROUP_ATTR = "_clio_concurrency_group"

_SEARCH = "__search__"


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


#: Characters of a failed result's own text the script's ``ToolError`` carries.
_ERROR_DETAIL_CHARS = 2000


def _error_detail(result: Any) -> str:
    """What a failed result says about its failure: its text blocks, else its structured
    content, bounded."""
    if not isinstance(result, Mapping):
        return ""
    texts = [
        str(block.get("text") or "")
        for block in result.get("content") or []
        if isinstance(block, Mapping) and block.get("type") == "text"
    ]
    detail = "\n".join(t for t in texts if t)
    if not detail and result.get("structuredContent") is not None:
        detail = json.dumps(result["structuredContent"], default=str)
    return detail[:_ERROR_DETAIL_CHARS]


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
        self.gates = ServerGates()
        self.pool = ThreadPoolExecutor(
            max_workers=call_workers(), thread_name_prefix="clio-code-call"
        )
        self.records: list[CallRecord] = []
        self.lock = threading.Lock()

    def submit(self, message: Mapping[str, Any], reply: Callable[[dict[str, Any]], None]) -> None:
        """Start one call; ``reply`` gets the kernel's ``result`` message when it ends."""
        call_id = message.get("id")
        tool = str(message.get("tool") or "")
        raw_args = message.get("args")
        args: Mapping[str, Any] = raw_args if isinstance(raw_args, Mapping) else {}
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
        if tool not in self.tools:  # direct-only and blocked tools are not given here
            return f"No tool named {tool!r} can be called from a script; see tools.search()."
        return ""

    def _run(self, tool: str, args: dict[str, Any]) -> tuple[bool, Any, str]:
        from clio_agent.gact.artifacts.observer_bridge import MODEL_CALL_SLOT  # noqa: PLC0415
        from clio_agent.gact.runtime.tool_permission_layers import native_gate  # noqa: PLC0415
        from clio_agent.gact.tool_observation import INLINE_COLLECTION  # noqa: PLC0415
        from clio_agent.runtime.fresh_loop import run_in_new_loop  # noqa: PLC0415
        from clio_agent.tools.tool_observation import RESULT_SINK  # noqa: PLC0415

        target = self.tools[tool]
        group, _ = tool_group(tool, self.servers)
        # A native tool may declare the group it shares a concurrency cap with (the browser).
        group = group or str(getattr(getattr(target, "func", None), CONCURRENCY_GROUP_ATTR, ""))
        sink: list[tuple[str | None, Any]] = []
        MODEL_CALL_SLOT.set({})  # this call's own observer id, not the script's
        INLINE_COLLECTION.set(None)  # the script reads the reply, not the model
        RESULT_SINK.set(sink)
        started = time.monotonic()
        with self.gates(group or tool), native_gate(target, tool, args) as refused:
            try:
                if refused:  # a rule or the tool's default (the gate's layers)
                    raise PermissionError(refused)
                if inspect.iscoroutinefunction(getattr(target, "func", None)):
                    value = run_in_new_loop(target.acall(**args))
                else:
                    value = target(**args)
                ok, error = True, ""
            except Exception as exc:  # noqa: BLE001 - forwards: to the script as ToolError
                value, ok, error = None, False, f"{type(exc).__name__}: {exc}"
        if ok and sink:
            sink_error, observed = sink[-1]
            found, structured = _mcp_value(observed)
            if sink_error:
                # The code says that it failed; the result's text says why (a validation
                # message, the server's error), which the script needs to correct itself.
                detail = _error_detail(observed)
                ok, error = False, f"{sink_error}: {detail}" if detail else str(sink_error)
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
