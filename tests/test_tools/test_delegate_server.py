"""Expert-facing delegation tools: delegate / run_command_async / poll / wait_for /
cancel — the mid-loop async pattern (spawn, keep working, then wait), in-memory.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastmcp import Client

from clio_agent.runtime.background_tasks import BackgroundTasks
from clio_agent.runtime.expert_invoker import ExpertRequest, ExpertResult, InProcessExpertInvoker
from clio_agent.tools.servers.delegate_server import build_delegate_server


def _parse(result: object) -> dict:
    data = getattr(result, "data", result)
    return data if isinstance(data, dict) else json.loads(data)


def _server(handler):
    tasks = BackgroundTasks()
    return build_delegate_server(tasks, lambda role: InProcessExpertInvoker(handler))


@pytest.mark.asyncio
async def test_delegate_returns_a_handle_then_wait_for_collects():
    async def handler(req: ExpertRequest) -> ExpertResult:
        await asyncio.sleep(0.2)
        return ExpertResult(expert_id=req.expert_id, answer=f"done:{req.question}")

    async with Client(_server(handler)) as client:
        h = _parse(await client.call_tool("delegate", {"role": "analysis", "question": "q"}))["handle"]
        # immediately: it did NOT block — the job is still queued/running
        p = _parse(await client.call_tool("poll", {"handle": h}))
        assert p["status"] in ("queued", "running")
        # ...the expert could do other work here...
        r = _parse(await client.call_tool("wait_for", {"handle": h, "timeout_s": 5}))
        assert r["status"] == "completed" and r["answer"] == "done:q"


@pytest.mark.asyncio
async def test_run_command_async_is_the_same_handle_contract():
    async def handler(req):
        return ExpertResult(expert_id=req.expert_id, answer="x")

    async with Client(_server(handler)) as client:
        h = _parse(await client.call_tool("run_command_async", {"command": "echo cmd-ok"}))["handle"]
        r = _parse(await client.call_tool("wait_for", {"handle": h, "timeout_s": 10}))
        assert r["status"] == "completed"
        assert r["result"]["exit_code"] == 0


@pytest.mark.asyncio
async def test_cancel_stops_a_running_job():
    async def handler(req):
        await asyncio.sleep(10)
        return ExpertResult(expert_id=req.expert_id, answer="late")

    async with Client(_server(handler)) as client:
        h = _parse(await client.call_tool("delegate", {"role": "x", "question": "q"}))["handle"]
        c = _parse(await client.call_tool("cancel", {"handle": h}))
        assert c["cancelled"] is True


@pytest.mark.asyncio
async def test_wait_for_unknown_handle_is_clean():
    async def handler(req):
        return ExpertResult(expert_id=req.expert_id, answer="x")

    async with Client(_server(handler)) as client:
        r = _parse(await client.call_tool("wait_for", {"handle": "nope", "timeout_s": 1}))
        assert r["found"] is False
