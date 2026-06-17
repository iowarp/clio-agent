"""The delegate tools + placement, for REAL: cross-process over the clio_run daemon
with a real ALCF expert, and the unified handle for an expert OR a command.

The mid-loop async pattern end to end: the model calls delegate() (routed by placement
to a role queue, served by a worker PROCESS running a real ALCF expert), keeps working,
then wait_for()s the result. Gated CLIO_RUN_CROSS_PROCESS=1 (+ CLIO_RUN_LIVE=1 for ALCF).
"""

from __future__ import annotations

import json
import os

import pytest
from fastmcp import Client

from clio_agent.runtime.background_tasks import BackgroundTasks
from clio_agent.runtime.clio_core_transport import ClioCoreExpertInvoker, ClioCoreMailbox
from clio_agent.runtime.placement import make_placement
from clio_agent.tools.servers.delegate_server import build_delegate_server

pytestmark = pytest.mark.cross_process

_ALCF = {
    "CLIO_LM_PROVIDER": "argonne",
    "CLIO_LM_API_BASE": "https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1",
    "CLIO_LM_MODEL": "openai/gpt-oss-120b",
    "CLIO_RUN_LIVE": "1",
}


def _parse(result: object) -> dict:
    data = getattr(result, "data", result)
    return data if isinstance(data, dict) else json.loads(data)


def _delegate_server(cross_arc, placement, tasks):
    # placement lives in the factory: role -> ClioCoreExpertInvoker over that role's queue
    def factory(role: str):
        return ClioCoreExpertInvoker(ClioCoreMailbox(cross_arc, prefix=placement.mailbox_for(role)), timeout=150)

    return build_delegate_server(tasks, factory)


@pytest.mark.skipif(os.environ.get("CLIO_RUN_LIVE") != "1", reason="real ALCF trace")
async def test_delegate_tool_routes_to_alcf_worker_cross_process(cross_arc, spawn_worker):
    """delegate() (placement → role queue) lands a job in a worker PROCESS running a
    real ALCF expert; poll() shows it in flight (didn't block); wait_for() collects the
    tool-grounded answer across the process boundary."""
    placement = make_placement("role")
    spawn_worker(placement.mailbox_for("analysis"), mode="alcf", n=1, extra_env=_ALCF)
    tasks = BackgroundTasks()
    async with Client(_delegate_server(cross_arc, placement, tasks)) as client:
        h = _parse(await client.call_tool("delegate", {
            "role": "analysis",
            "question": "Use the lookup tool to get the 'latency' metric and report its exact value.",
        }))["handle"]
        assert _parse(await client.call_tool("poll", {"handle": h}))["status"] in ("queued", "running")
        r = _parse(await client.call_tool("wait_for", {"handle": h, "timeout_s": 150}))
        assert r["status"] == "completed"
        assert "42.7" in r["answer"]  # real ALCF, tool-grounded, from a different process
    cross_arc.put("context", placement.mailbox_for("analysis") + "STOP", b"1")


@pytest.mark.skipif(os.environ.get("CLIO_RUN_LIVE") != "1", reason="real ALCF trace")
async def test_delegate_expert_and_command_concurrently(cross_arc, spawn_worker):
    """A job is an expert OR a command: spawn a cross-process ALCF expert AND a local
    long-running bash op at once, keep working, then wait on BOTH via the same handles."""
    placement = make_placement("role")
    spawn_worker(placement.mailbox_for("data"), mode="alcf", n=1, extra_env=_ALCF)
    tasks = BackgroundTasks()
    async with Client(_delegate_server(cross_arc, placement, tasks)) as client:
        h_expert = _parse(await client.call_tool("delegate", {
            "role": "data",
            "question": "Use the lookup tool to get the 'throughput' metric and report its exact value.",
        }))["handle"]
        h_cmd = _parse(await client.call_tool("run_command_async", {
            "command": "sleep 1; echo COMMAND_DONE",
        }))["handle"]
        # both run while the parent could be doing other work; collect each by handle
        r_cmd = _parse(await client.call_tool("wait_for", {"handle": h_cmd, "timeout_s": 30}))
        r_expert = _parse(await client.call_tool("wait_for", {"handle": h_expert, "timeout_s": 150}))
        assert r_cmd["status"] == "completed" and r_cmd["result"]["exit_code"] == 0
        assert r_expert["status"] == "completed" and "42.7" in r_expert["answer"]
    cross_arc.put("context", placement.mailbox_for("data") + "STOP", b"1")
