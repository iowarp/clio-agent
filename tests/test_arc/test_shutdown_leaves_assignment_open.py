"""A turn that ends with an error while the server stops leaves its assignment open.

``clio stop`` signals the server's whole process group, so a provider's command-line
child dies with the server's own SIGTERM and the turn can end with an error before the
shutdown drain cancels it. That segment must be left open, as the drain leaves the turns
it cancels, so the restart resumes its assignment in place (CC-F4). Settled ``failed``,
a running subagent was lost at every graceful restart that won this race, while its
parent, cancelled by the drain, resumed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.conversations import ConversationOutcome, ConversationSegment
from tests.test_arc.test_conversation_owner_contracts import create_environment
from tests.test_arc.test_task_retirement_contracts import accept, idle


@pytest.mark.asyncio
@pytest.mark.parametrize("stopping", [True, False], ids=["server-stopping", "server-running"])
async def test_an_error_while_the_server_stops_leaves_the_assignment_for_recovery(
    arc: ARCMemory, tmp_path: Path, stopping: bool
) -> None:
    """SABOTAGE: drop ``ended_by_shutdown`` from ``Conversations``' segment loop and the
    stopping case settles the assignment ``failed`` and terminal, which recovery skips.
    """
    app, owner, root = await create_environment(arc, tmp_path)
    sid = root.owner.session_id
    app.state.uvicorn_server = SimpleNamespace(should_exit=False)

    async def model(segment: ConversationSegment) -> ConversationOutcome:
        # The server's SIGTERM arrives while the model call runs; the call fails.
        app.state.uvicorn_server.should_exit = stopping
        return ConversationOutcome(
            {},
            error={
                "error": "cancelled" if stopping else "agent_error",
                "message": "claude agent sdk transport failed mid-stream: exit code 143",
                "recoverable": True,
            },
        )

    owner.run_segment = model
    owner.start()
    try:
        await accept(owner, sid, "input-1", "Write the essay")
        owner.runner.signal(sid)
        await idle(owner, sid)
        task = owner.history.task_snapshot(root.handle)
        assert task is not None
        if stopping:
            # Open, as a drained turn leaves it: the restart resumes it in place.
            assert task.terminal_at is None, task
            assert task.status != "failed"
        else:
            # An ordinary failure still settles, with its outcome.
            assert task.terminal_at is not None and task.status == "failed"
    finally:
        app.state.uvicorn_server.should_exit = False
        await owner.shutdown()
        await owner.recovery.drain()
