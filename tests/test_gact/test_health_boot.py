"""Cold doctor imports must leave required native attachment first in line."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from clio_agent.gact.routes.health_boot import start_boot_collection
from clio_agent.runtime.status import RuntimeReport


@pytest.mark.asyncio
@pytest.mark.parametrize("attach_failed", [False, True])
async def test_full_doctor_waits_for_arc_to_settle(attach_failed: bool) -> None:
    """The complete doctor runs after either a successful or failed ARC attach."""
    release = asyncio.Event()
    collected: list[bool] = []

    async def attach() -> None:
        await release.wait()
        app.state.arc_failed = attach_failed
        app.state.arc_settled = True

    app = SimpleNamespace(state=SimpleNamespace(server_boot=True, arc_settled=False))
    app.state.arc_boot_runner = asyncio.create_task(attach())

    def collect(_: object) -> RuntimeReport:
        assert app.state.arc_settled
        collected.append(app.state.arc_failed)
        return RuntimeReport(integrations=[])

    start_boot_collection(app, collect)
    await asyncio.sleep(0)
    assert not app.state.health_boot_task.done()
    assert collected == []
    release.set()
    await asyncio.wait_for(app.state.health_boot_task, timeout=2)
    assert collected == [attach_failed]
