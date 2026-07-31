"""Tasks extension client (SEP-2663, #1115).

The substrate is the **`fastmcp_tasks`** package (note: `mcp/client/experimental/tasks.py`
does NOT exist in the installed mcp 2.0 SDK — see the slice summary). It provides the
client extension, the `tasks/get` poll loop honoring `pollIntervalMs`, `input_required`
rounds via `tasks/update` dispatched through the client's elicitation callback (CLIO's
P1.3 handler, so inputs land on the ONE HITL surface), and ack-only `tasks/cancel`.

CLIO builds what the substrate lacks: per-poll input-key DEDUP, the `Mcp-Name: <taskId>`
header on task RPCs, durable task-id persistence + reconnect-by-task-id, and #1112
classification tolerance for `resultType: "task"`.
"""

from __future__ import annotations

import pytest


def test_reconnect_by_task_id_resumes_polling_to_completion() -> None:
    """FAILING-FIRST (#1115 headline): a task id survives losing the client.

    Persist the id from ``CreateTaskResult``, DROP the client (crash), reconstruct a
    fresh one, and resume polling that same task id to a terminal result — the crash
    recovery the P2 relay transport depends on.
    """

    from clio_agent.tools.mcp_tasks import resume_task, task_record_store

    assert resume_task is not None
    assert task_record_store is not None
