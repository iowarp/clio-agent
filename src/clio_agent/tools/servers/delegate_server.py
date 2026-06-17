"""Expert-facing delegation tools — spawn a sub-job, keep working, then collect it.

The Claude-Code-style background / monitor / wait_for pattern as MCP tools an expert can
call MID-loop (not just end-of-loop ``expert_handoffs``). A spawned job is **an expert
delegation OR a long-running command** — both return a handle with the same
monitor/wait/cancel contract:

    delegate(role, question)     -> handle        # another expert (routed by placement)
    run_command_async(command)   -> handle        # a long-running bash op
    <the expert keeps reasoning / other tool calls>
    poll(handle)                 -> status/output  # check without blocking
    wait_for(handle, timeout)    -> result         # block only when you're ready
    cancel(handle)                                 # stop one you no longer need

``build_delegate_server(tasks, invoker_factory)`` injects the task registry and an
``invoker_factory(role) -> ExpertInvoker`` — which is where *placement* lives (it picks
the role's mailbox). In-process for tests, the clio-core ``ClioCoreExpertInvoker`` over the
role queue in production. Same handle works either way.

Principle (CLAUDE.md): the MODEL decides to delegate — a tool call IS its action — and
clio carries the job + result. No clio-side routing/completion heuristic.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from fastmcp import FastMCP

from clio_agent.runtime.background_tasks import BackgroundTasks, spawn_command
from clio_agent.runtime.expert_invoker import ExpertInvoker, ExpertRequest, spawn_invocation

InvokerFactory = Callable[[str], ExpertInvoker]


def build_delegate_server(tasks: BackgroundTasks, invoker_factory: InvokerFactory) -> FastMCP:
    """Build the delegation MCP server over a task registry + a per-role invoker factory."""
    mcp = FastMCP("delegate")

    @mcp.tool()
    async def delegate(role: str, question: str, context: Optional[dict] = None) -> dict[str, Any]:
        """Start a sub-job for another expert (by ``role``) and return a HANDLE
        immediately — does NOT block. Use it when another expert can work while you keep
        reasoning; collect later with wait_for(handle). ``context`` carries what the
        other expert needs (e.g. a data handle). Where the role's workers run is a
        deployment concern — placement routes it."""
        invoker = invoker_factory(role)
        req = ExpertRequest(expert_id=role, question=question, context=dict(context or {}))
        handle = spawn_invocation(tasks, invoker, req, label=role)
        return {"handle": handle, "role": role, "status": "queued"}

    @mcp.tool()
    async def run_command_async(command: str, cwd: Optional[str] = None) -> dict[str, Any]:
        """Start a long-running shell command in the background and return a HANDLE
        immediately. Same handle/monitor/wait contract as delegate — poll/wait_for it."""
        handle = spawn_command(tasks, command, cwd=cwd, label="cmd")
        return {"handle": handle, "status": "queued"}

    @mcp.tool()
    def poll(handle: str) -> dict[str, Any]:
        """Check a job WITHOUT blocking: its status and how many output lines it has."""
        rec = tasks.get(handle)
        if rec is None:
            return {"found": False, "handle": handle}
        return {
            "found": True,
            "handle": handle,
            "status": rec.status.value,
            "output_lines": len(rec.output),
        }

    @mcp.tool()
    async def wait_for(handle: str, timeout_s: float = 60.0) -> dict[str, Any]:
        """Block until a job finishes (or ``timeout_s``), then return its result. Call
        this once you've done everything you can in the meantime. ``timed_out`` is true
        if it isn't done yet (you can keep waiting or move on)."""
        if tasks.get(handle) is None:
            return {"found": False, "handle": handle}
        rec = await tasks.wait(handle, timeout=timeout_s)
        if not rec.status.terminal:
            return {"found": True, "handle": handle, "status": rec.status.value, "timed_out": True}
        result = rec.result
        out: dict[str, Any] = {"found": True, "handle": handle, "status": rec.status.value, "error": rec.error}
        if hasattr(result, "answer"):  # an ExpertResult
            out["answer"] = str(getattr(result, "answer", "") or "")
        elif isinstance(result, dict):  # a command result
            out["result"] = result
        return out

    @mcp.tool()
    async def cancel(handle: str) -> dict[str, Any]:
        """Stop a job you no longer need."""
        return {"handle": handle, "cancelled": tasks.cancel(handle)}

    return mcp
