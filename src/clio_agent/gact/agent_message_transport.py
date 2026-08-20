"""Thin transport implementations for ExpertInvoker.message (#1128)."""

from __future__ import annotations

from typing import Any


def message_in_process(invoker: Any, handle: Any, text: str, metadata: Any) -> None:
    """Reuse the established child-session step-boundary steer producer."""

    from clio_agent.gact.agents.invoker import InvokerError  # noqa: PLC0415
    from clio_agent.gact.live_handle import enqueue_steer_or_raise  # noqa: PLC0415

    task = invoker.app.state.agent_task_registry.get(handle.task_id)
    if task is None:
        raise InvokerError(f"unknown task {handle.task_id!r}", reason="unknown_task")
    enqueue_steer_or_raise(invoker.app, task, text, dict(metadata or {}))


def message_via_relay(invoker: Any, handle: Any, text: str, metadata: Any) -> None:
    """Answer relay's parked post-admission agent-message input round."""

    from clio_agent.gact.agents.invoker import InvokerError  # noqa: PLC0415

    if metadata:
        raise InvokerError(
            "relay agent messages do not carry local steer metadata",
            reason="message_metadata_unsupported",
        )
    local = invoker._require_local_task(handle)
    if local.is_terminal:
        raise InvokerError(f"task {handle.task_id!r} is terminal", reason="already_terminal")
    invoker._runtime.message(
        handle.parent_session_id,
        invoker._runtime.task_key(handle),
        text,
    )
