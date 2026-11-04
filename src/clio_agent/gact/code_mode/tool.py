"""The ``run_code`` tool: run a Python script that calls the agent's tools.

The script runs in the scope's kernel (:mod:`.kernels`) with ``tools`` bound to stubs
of the agent's tools (:mod:`.stubs`); its tool calls go through :mod:`.dispatch`. The
model sees only what the script prints, its error, and the value of a final
expression, bounded like every result (12,000 characters, the rest spilled to a file),
plus how many tool calls it made.

Running a script is one non-read call, approved before it runs with the code shown
(``run_code`` asks the permission gate itself, as native tools that act do). Its nested
calls are decided by the gate under the script's grant (:mod:`.grants`). In plan mode
it runs only where the sandbox enforces the write fence, in a kernel that may write only
the session's scratch folder and temp (its nested calls still meet the plan ACL), else
it refuses with ``code_plan_mode_unconfined``.

A script that runs past ``code.call_timeout_s`` (waiting for a person's answer does
not count), or is cancelled, stops its kernel: the next script starts a fresh one.
"""

from __future__ import annotations

import contextvars
import queue
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from clio_agent import conf
from clio_agent.runtime import trace

__all__ = [
    "CODE_PLAN_MODE_UNCONFINED",
    "build_run_code_tool",
    "call_timeout_s",
    "finalize_run_code",
    "poll_s",
    "run_script",
]

#: Typed refusal: plan mode, and the sandbox does not enforce the write fence.
CODE_PLAN_MODE_UNCONFINED = "code_plan_mode_unconfined"
_DESCRIPTION = (
    "Run Python in a persistent kernel that can call your tools: `await "
    "tools.<group>.<tool>(arg=...)` returns the tool's structured result (a dict, list or "
    "text), and `asyncio.gather` runs calls at once. Use it when a task needs many calls "
    "whose results you filter, join or count (paging through a listing, reading many "
    "files, querying several servers) and print only what matters: you see the printed "
    "output, the value of a final expression and the call count, not the tools' results. "
    "The kernel is ordinary Python that can read your workspace, so a script can also open "
    "and parse many files itself (pathlib, ast, json, csv) in one process. "
    "One call or a short dependent chain is cheaper as direct calls. A script makes at most "
    "{budget} tool calls; its variables persist into the next script, so larger work "
    "continues there, and tasks it started stop when it ends. `tools.search('words')` "
    "finds tools; `help(tools.<group>.<tool>)` shows arguments; a failed call raises "
    "ToolError. The script is approved before it runs."
)


def poll_s() -> float:
    """Seconds between checks for cancellation and timeout while a script runs.

    Raises:
        ValueError: The configured interval is not positive.
    """
    value = conf.resolve("code.poll_s", env="CLIO_CODE_POLL_S", default=0.25, cast=conf.as_float)
    if not value > 0:
        raise ValueError(f"code.poll_s must be positive, not {value}")
    return value


def call_timeout_s() -> float:
    """Seconds one script may run (time waiting for a person's answer excluded)."""
    return conf.resolve(
        "code.call_timeout_s", env="CLIO_CODE_CALL_TIMEOUT_S", default=600.0, cast=float
    )


class CodeError(RuntimeError):
    """``run_code`` could not run the script; ``reason`` is typed."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _approve(app: Any, code: str) -> None:
    from clio_agent.gact.permission_gate import _invoke_permission_gate  # noqa: PLC0415

    gate = getattr(app.state, "pending_permission_gate", None) or app.state.make_permission_gate()
    first = next((line.strip() for line in code.splitlines() if line.strip()), "")
    summary = f"Run a Python script ({len(code.splitlines())} lines): {first}"[:500]
    context = {"kind": "native", "summary": summary}
    if _invoke_permission_gate(gate, "run_code", {"code": code}, context) != "allow":
        raise PermissionError("The script was not approved, so it did not run.")


def _plan_mode_refusal(app: Any, session_id: str) -> bool:
    """Whether the session is in plan mode (the script then runs read-only); refuses when
    plan mode has no enforced fence."""
    from clio_agent.runtime import sandbox  # noqa: PLC0415

    session = app.state.sessions.get(session_id)
    if str(getattr(session, "mode", "") or "") != "plan":
        return False
    if sandbox.fence_enforced():
        return True
    raise CodeError(
        "run_code is not available in Plan mode here: this host's sandbox does not enforce "
        "the write fence, so nothing would keep a script read-only. Use the read tools.",
        CODE_PLAN_MODE_UNCONFINED,
    )


def _workspace() -> Path:
    from clio_agent.tools.execution import get_active_tool_workspace_root  # noqa: PLC0415
    from clio_agent.tools.write_scope import active_tool_write_root  # noqa: PLC0415

    root = str(active_tool_write_root() or get_active_tool_workspace_root() or "")
    if not root:
        raise CodeError("run_code needs the conversation's workspace.", "code_no_workspace")
    return Path(root).resolve()


def _search(tools: Mapping[str, Any]) -> Any:
    """``tools.search`` in a script: the tools a script can call, deferred and code-only
    ones included."""
    from clio_agent.gact.agents.deferred_tools import _schema  # noqa: PLC0415
    from clio_agent.gact.tool_catalog import CatalogEntry, ToolCatalog  # noqa: PLC0415

    def search(query: str, limit: int) -> list[dict[str, str]]:
        source = ToolCatalog(CatalogEntry(n, "", *_schema(t)) for n, t in tools.items())
        found, _ = source.search(query, limit)
        return [{"name": e.name, "owner": e.owner, "description": e.description} for e in found]

    return search


def _callers(agent: Any) -> tuple[frozenset[str], frozenset[str]]:
    """``(direct_only, code_only)`` from the agent's tool settings."""
    return (
        frozenset(getattr(agent, "_clio_direct_only", frozenset()) or ()),
        frozenset(getattr(agent, "_clio_code_only", frozenset()) or ()),
    )


def run_script(
    agent: Any, code: str, *, session_id: str, scope: str, read_only: bool = False
) -> dict[str, Any]:
    """Run ``code`` in the scope's kernel (a read-only one in plan mode); the outcome."""
    from clio_agent.agent import cancellation_requested  # noqa: PLC0415
    from clio_agent.gact.artifacts.observer_bridge import current_call_id  # noqa: PLC0415
    from clio_agent.gact.code_mode import kernels  # noqa: PLC0415
    from clio_agent.gact.code_mode.dispatch import Dispatcher  # noqa: PLC0415
    from clio_agent.gact.code_mode.grants import (  # noqa: PLC0415
        ScriptGrant,
        bind_grant,
        reset_grant,
    )
    from clio_agent.gact.code_mode.stubs import stub_table  # noqa: PLC0415
    from clio_agent.gact.tool_call_nesting import PARENT_CALL_ID  # noqa: PLC0415

    servers = tuple(getattr(agent, "_clio_tool_servers", ()) or ())
    grant = ScriptGrant(current_call_id(), code)
    grant_token, parent_token = bind_grant(grant), PARENT_CALL_ID.set(grant.call_id)
    try:
        context = contextvars.copy_context()
    finally:
        PARENT_CALL_ID.reset(parent_token)
        reset_grant(grant_token)
    direct_only, code_only = _callers(agent)
    callable_tools = {n: t for n, t in agent.tools.items() if n not in direct_only}
    dispatcher = Dispatcher(callable_tools, servers, context, _search(callable_tools))
    kernel = kernels.kernel_for(session_id, scope, _workspace(), read_only=read_only)
    outcome: dict[str, Any] = {"status": "failed", "output": "", "value": None, "error": ""}
    started = time.monotonic()
    limit, poll = call_timeout_s(), poll_s()
    try:
        with kernel.lock:
            kernel.bind(
                stub_table(agent.tools, servers, direct_only=direct_only, code_only=code_only)
            )
            kernel.send({"type": "exec", "id": 1, "code": code})
            while True:
                if cancellation_requested():
                    kernels.discard(kernel, "cancelled")
                    outcome.update(status="cancelled", error="The script was stopped.")
                    break
                elapsed = time.monotonic() - started - grant.waited_s
                if limit > 0 and elapsed > limit and not grant.waiting:
                    kernels.discard(kernel, "timeout")
                    outcome.update(
                        status="timed_out",
                        error=f"The script ran past {limit:g} s (code.call_timeout_s) and was "
                        "stopped; its variables are gone, so the next script starts fresh.",
                    )
                    break
                try:
                    message = kernel.receive(poll)
                except queue.Empty:
                    continue
                if message is None:
                    kernels.discard(kernel, "exited")
                    outcome.update(error=f"The Python kernel exited: {kernel.diagnostics()}")
                    break
                if message.get("type") == "call":
                    dispatcher.submit(message, kernel.send)
                elif message.get("type") == "done":
                    outcome.update(
                        status="completed" if message.get("ok") else "failed",
                        output=str(message.get("output") or ""),
                        value=message.get("value"),
                        error=str(message.get("error") or ""),
                    )
                    break
    finally:
        dispatcher.close()
        kernel.last_used = time.monotonic()
    calls = list(dispatcher.records)
    outcome.update(
        code=code,
        calls=len(calls),
        failed_calls=sum(1 for c in calls if not c.ok),
        call_tools=sorted({c.tool for c in calls}),
        duration_ms=int((time.monotonic() - started) * 1000),
        python=kernel.python,
    )
    trace.event(
        "CODE",
        "run_code status=%s calls=%d failed=%d ms=%d",
        outcome["status"],
        outcome["calls"],
        outcome["failed_calls"],
        outcome["duration_ms"],
    )
    if outcome["status"] == "cancelled":
        from clio_agent.gact.agents.clio_react import _turn_cancelled  # noqa: PLC0415

        raise _turn_cancelled()
    return outcome


def model_text(outcome: Mapping[str, Any]) -> str:
    """What the model reads: output, error, final value, then the call count."""
    parts = []
    if outcome.get("output"):
        parts.append(str(outcome["output"]).rstrip())
    if outcome.get("error"):
        parts.append(f"Error:\n{str(outcome['error']).rstrip()}")
    if outcome.get("value") is not None:
        parts.append(f"Value: {outcome['value']}")
    calls, failed = int(outcome.get("calls") or 0), int(outcome.get("failed_calls") or 0)
    status = {"completed": "Script finished", "failed": "Script failed"}.get(
        str(outcome.get("status")), "Script stopped"
    )
    count = f"{calls} tool call{'s' if calls != 1 else ''}" + (
        f", {failed} failed" if failed else ""
    )
    parts.append(f"({status}; {count}.)")
    if not outcome.get("output") and outcome.get("value") is None and not outcome.get("error"):
        parts.insert(0, "The script printed nothing.")
    return "\n\n".join(parts)


def build_run_code_tool() -> Any:
    """The ``run_code`` native tool (its description gains the tool index per agent)."""
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.agents.clio_react import active_loop  # noqa: PLC0415
    from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
        declare_structured_content,
        native_tool,
    )
    from clio_agent.gact.code_mode.dispatch import max_nested_calls  # noqa: PLC0415
    from clio_agent.gact.code_mode.presentation import present, starting  # noqa: PLC0415
    from clio_agent.tools.tool_settings import self_gated  # noqa: PLC0415

    def run_code(code: str) -> str:
        """Run a Python script that can call this agent's tools."""
        app, session_id = _ctx.active_app(), _ctx.active_session_id() or ""
        loop = active_loop()
        if app is None or loop is None or not session_id:
            raise CodeError("run_code runs inside an agent step.", "code_no_agent")
        if not str(code or "").strip():
            raise CodeError("code is empty.", "code_empty")
        read_only = _plan_mode_refusal(app, session_id)
        _approve(app, code)
        scope = _ctx.run_keyed_scope(_ctx.active_react_scope() or "")
        outcome = run_script(
            loop.agent, code, session_id=session_id, scope=scope, read_only=read_only
        )
        declare_structured_content(outcome)
        return model_text(outcome)

    return self_gated(
        native_tool(
            run_code,
            name="run_code",
            callers="direct",
            presentation=present,
            presentation_start=starting,
            domain="workflows",
            title="Run code",
            desc=_DESCRIPTION.format(budget=max_nested_calls()),
            args={
                "code": {"type": "string", "description": "Python source; top-level await works."}
            },
        )
    )


def finalize_run_code(program: Any, servers: frozenset[str]) -> None:
    """Add the agent's tool index to ``run_code``'s description, once its tools are final."""
    from clio_agent.gact.code_mode.stubs import index_text, stub_table  # noqa: PLC0415

    tools: Mapping[str, Any] = getattr(program, "tools", {}) or {}
    tool = tools.get("run_code")
    if tool is None:
        return
    object.__setattr__(program, "_clio_tool_servers", servers)
    direct_only, code_only = _callers(program)
    index = index_text(stub_table(tools, servers, direct_only=direct_only, code_only=code_only))
    if index and "\nTools in scripts:" not in str(tool.desc):
        tool.desc = f"{_DESCRIPTION}\n\nTools in scripts:\n{index}"
