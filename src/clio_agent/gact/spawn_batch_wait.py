"""``spawn_agents_parallel(..., wait=True)``: the batch's reports in the same call.

The children were spawned together; this waits for all of them through the task
layer's own wait (the one ``wait_tasks`` uses), so cancellation, Stop, mail
interruptions and uncertain members behave exactly as they do there. Each report
gets an equal share of the result bound (:func:`clio_agent.gact.task_result_spill.
shared_result_bound`): a longer one is saved to its own file with an excerpt, so a
batch of long reports never spills as a whole. Children that failed to start are
reported beside the reports, by their index.
"""

from __future__ import annotations

from typing import Any

__all__ = ["wait_for_batch"]


async def wait_for_batch(outcomes: list[dict[str, Any]]) -> Any:
    """Wait for every spawned child in ``outcomes``; return the collected reports."""
    from clio_agent.gact.task_declarations import _runtime  # noqa: PLC0415
    from clio_agent.gact.task_result_spill import shared_result_bound  # noqa: PLC0415
    from clio_agent.gact.task_tool_collection import collected  # noqa: PLC0415

    handles = [str(row["handle"]) for row in outcomes if row.get("handle")]
    failed = [row for row in outcomes if not row.get("handle")]
    if not handles:
        return {"spawned": outcomes, "outcomes": outcomes}
    app, caller = _runtime()
    with shared_result_bound(len(handles)):
        value = await app.state.task_operations.wait(
            caller, handles, "all", None, interrupt_on_mail=True
        )
        if failed:
            value = {**value, "not_started": failed}
        return await app.state.task_history.io.run(collected, app, caller, value)
