"""Thin transport implementations for ExpertInvoker.message (#1128)."""

from __future__ import annotations

from typing import Any


def message_in_process(invoker: Any, handle: Any, text: str, metadata: Any) -> None:
    """Reuse the established child-session step-boundary steer producer.

    ``metadata`` reaches here already decided by its CALLER (docs/design/
    a2ui-compat-campaign-2026-09.md S3 adversarial review): the client-facing
    ``POST /v1/agent-tasks/{id}/steer`` route (``routes/agent_tasks.py``) is a
    genuine door onto the CHILD session -- it runs the same
    ``apply_client_metadata_guards`` POST /messages runs (against the CHILD,
    not the parent) and passes down an already-validated, already-renamed
    mapping, which is NOT stripped here (a client's own advertisement is
    never silently thrown away without a typed reason). The model-facing
    ``message_agent`` tool (the true parent->child FORWARDING path) never
    supplies a ``metadata`` argument at all, so there is nothing to strip on
    that path either -- if it ever gains one, THAT call site is where
    ``strip_renderer_metadata`` belongs (mirroring ``turn_spawn.py``'s
    ``_launch``), not this shared transport.
    """

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
