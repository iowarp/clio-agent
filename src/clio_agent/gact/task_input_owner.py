"""Preserve the existing elicitation surface with an accepted task's exact identity."""

from __future__ import annotations

from typing import Any

from clio_agent.gact.elicitation_correlation import task_input_owner
from clio_agent.tools.mcp_handlers import MCPInvocationContext
from clio_agent.tools.mcp_task_records import TaskKey, TaskRecordStore
from clio_agent.tools.mcp_tasks import session_elicitation_callback


def owned_input_callback(app: Any, session: Any, key: TaskKey, store: TaskRecordStore) -> Any:
    """Wrap only an already wired input callback; never advertise a new capability."""
    callback = session_elicitation_callback(session)
    row = store.get(key)
    if callback is None or row is None:
        return callback
    invocation = MCPInvocationContext(
        row.invocation_id, key.session_id, key.server_id, row.tool, task_id=key.task_id
    )

    async def answer(request_context: Any, params: Any) -> Any:
        with task_input_owner(app, invocation, session):
            return await callback(request_context, params)

    return answer
