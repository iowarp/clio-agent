"""Transport runtime helpers for the relay-backed ExpertInvoker."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

import httpx

from clio_agent.errors import ToolError

logger = logging.getLogger(__name__)
RELAY_REMOTE_AGENT_TOOL = "relay_submit_remote_agent"


class RelayInvokerRuntime:
    """Fresh-client operations over the persisted relay task-key owner."""

    def __init__(
        self,
        client_factory: Callable[[str], Any],
        *,
        cluster: str,
    ) -> None:
        self._client_factory = client_factory
        self._cluster = cluster

    def submit_and_poll(
        self,
        parent_session_id: str,
        remote_spec: Mapping[str, Any],
    ) -> tuple[Any, Any]:
        """Submit one remote_agent job and observe its initial canonical state."""

        async def call() -> tuple[Any, Any]:
            arguments = {
                "cluster": self._cluster,
                **{key: value for key, value in remote_spec.items() if value is not None},
            }
            async with self._client_factory(parent_session_id) as client:
                identity = await client.submit(RELAY_REMOTE_AGENT_TOOL, arguments)
                current = await client.poll(identity)
            return identity, current

        return run_async(call)

    def poll(self, parent_session_id: str, key: Any) -> Any:
        """Poll one reconstructed identity on a fresh client."""

        async def call() -> Any:
            from clio_agent.tools.relay_transport import RelayTaskIdentity  # noqa: PLC0415

            async with self._client_factory(parent_session_id) as client:
                return await client.poll(RelayTaskIdentity.from_key(key))

        return run_async(call)

    def resume(self, parent_session_id: str, key: Any, timeout_s: float) -> Any:
        """Resume one persisted task on a fresh client."""

        async def call() -> Any:
            async with self._client_factory(parent_session_id) as client:
                return await client.resume(key, timeout_seconds=timeout_s)

        return run_async(call)

    def cancel(self, parent_session_id: str, key: Any) -> Any:
        """Send cooperative cancellation on a fresh client."""

        async def call() -> Any:
            from clio_agent.tools.relay_transport import RelayTaskIdentity  # noqa: PLC0415

            async with self._client_factory(parent_session_id) as client:
                return await client.cancel(RelayTaskIdentity.from_key(key))

        return run_async(call)

    def message(self, parent_session_id: str, key: Any, text: str) -> None:
        """Answer the relay agent's durable post-admission input round."""

        async def call() -> None:
            from clio_agent.tools.relay_transport import RelayTaskIdentity  # noqa: PLC0415

            async with self._client_factory(parent_session_id) as client:
                await client.message(RelayTaskIdentity.from_key(key), text)

        run_async(call)

    @staticmethod
    def task_key(handle: Any) -> Any:
        """Resolve exactly one persisted composite identity for a handle."""

        from clio_agent.gact.agents.invoker import InvokerError  # noqa: PLC0415
        from clio_agent.tools.mcp_task_records import resolve_store  # noqa: PLC0415

        records = [
            record
            for record in resolve_store(None).list()
            if record.task_id == handle.task_id and record.session_id == handle.parent_session_id
        ]
        if not records:
            raise InvokerError(
                f"unknown relay task {handle.task_id!r}",
                reason="unknown_task",
            )
        if len(records) != 1:
            raise InvokerError(
                f"relay task {handle.task_id!r} has ambiguous durable identities",
                reason="ambiguous_task",
            )
        return records[0].key


class RelayEventPump:
    """Reconnectable daemon consumers for relay TaskEvent SSE streams."""

    def __init__(self, app: Any, client_factory: Callable[[str], Any]) -> None:
        self._app = app
        self._client_factory = client_factory
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}

    def start(
        self,
        handle: Any,
        key: Any,
    ) -> None:
        """Start at most one live stream for this invoker and task."""

        with self._lock:
            existing = self._threads.get(handle.task_id)
            if existing is not None and existing.is_alive():
                return
            thread = threading.Thread(
                target=self._pump,
                args=(handle, key),
                name=f"clio-relay-task-events-{handle.task_id}",
                daemon=True,
            )
            self._threads[handle.task_id] = thread
            thread.start()

    def _pump(
        self,
        handle: Any,
        key: Any,
    ) -> None:
        try:
            run_async(lambda: self._stream(handle, key))
        except (httpx.HTTPError, OSError, RuntimeError, TimeoutError, ToolError):
            logger.warning(
                "relay TaskEvent stream disconnected reason=relay_event_stream_disconnected "
                "task=%s; tasks/get remains authoritative",
                handle.task_id,
                exc_info=True,
            )

    async def _stream(
        self,
        handle: Any,
        key: Any,
    ) -> None:
        from clio_agent.gact.agents.invoker import InvokerError  # noqa: PLC0415
        from clio_agent.tools.relay_transport import RelayTaskIdentity  # noqa: PLC0415

        async with self._client_factory(handle.parent_session_id) as client:
            async for raw in client.stream_events(RelayTaskIdentity.from_key(key), cursor=1):
                if not isinstance(raw, Mapping):
                    self._record_drop(
                        handle,
                        "relay_timeline_malformed",
                        raw,
                        "relay event-stream item is not a mapping",
                    )
                    continue
                try:
                    event = relay_task_event(handle, raw)
                except InvokerError as exc:
                    reason = (
                        "relay_timeline_task_identity_mismatch"
                        if exc.reason == "task_identity_mismatch"
                        else "relay_timeline_malformed"
                    )
                    self._record_drop(handle, reason, raw, str(exc))
                    continue
                except (TypeError, ValueError) as exc:
                    self._record_drop(handle, "relay_timeline_malformed", raw, str(exc))
                    continue
                if event is not None:
                    self._consume_lifecycle(handle, event)
                else:
                    self._consume_timeline(handle, raw)

    def _consume_lifecycle(self, handle: Any, event: Any) -> None:
        """Fold one classified lifecycle event through the committed owner seam."""

        local = self._app.state.agent_task_registry.get(handle.task_id)
        if local is None:
            self._record_drop(
                handle,
                "relay_timeline_unknown_task",
                event.to_wire(),
                "retained handle is absent from AgentTaskRegistry",
            )
            return
        if local.is_terminal or local.status == event.status:
            return
        from clio_agent.gact.agents import invoker as invoker_module  # noqa: PLC0415

        invoker_module.fold_agent_task_event(self._app, event)

    def _consume_timeline(self, handle: Any, raw: Mapping[str, Any]) -> None:
        """Forward one application event into the bounded live-view sink."""

        from clio_agent.gact.relay_timeline import route_relay_timeline_event  # noqa: PLC0415

        route_relay_timeline_event(self._app, handle, raw)

    def _record_drop(self, handle: Any, reason: str, raw: Any, message: str) -> None:
        """Record a pump classification refusal through the typed drop catalog."""

        from clio_agent.gact.relay_timeline import (  # noqa: PLC0415
            record_relay_timeline_drop,
        )

        record_relay_timeline_drop(self._app, handle, reason, raw=raw, message=message)


def relay_task_event(handle: Any, raw: Mapping[str, Any]) -> Any | None:
    """Project an exact relay TaskEvent envelope onto the local child identity."""

    from clio_agent.gact.agents.invoker import (  # noqa: PLC0415
        InvokerError,
        TaskEvent,
    )

    candidate: Mapping[str, Any] = raw
    nested = raw.get("task_event")
    if isinstance(nested, Mapping):
        candidate = nested
    else:
        metadata = raw.get("metadata")
        if isinstance(metadata, Mapping) and isinstance(metadata.get("task_event"), Mapping):
            candidate = metadata["task_event"]
    event = TaskEvent.from_wire(candidate)
    if not event.event_type.startswith("agent.task."):
        return None
    if event.task_id and event.task_id != handle.task_id:
        raise InvokerError(
            "relay TaskEvent task_id disagrees with the retained handle",
            reason="task_identity_mismatch",
        )
    payload = dict(event.payload)
    payload.update(
        {
            "task_id": handle.task_id,
            "parent_session_id": handle.parent_session_id,
            "child_session_id": handle.child_session_id,
            "status": event.status,
        }
    )
    return replace(
        event,
        task_id=handle.task_id,
        session_id=handle.child_session_id,
        payload=payload,
    )


def run_async(factory: Callable[[], Any]) -> Any:
    """Run a fresh coroutine from a synchronous protocol method."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(factory())).result()


def find_task_result_wire(value: Any) -> dict[str, Any] | None:
    """Find the bounded TaskResult record in a relay completion envelope."""

    from clio_agent.gact.agents.invoker import InvokerError  # noqa: PLC0415

    current = value
    for _depth in range(8):
        if not isinstance(current, Mapping):
            return None
        if {"status", "result"} <= set(current) and (
            "task_id" in current or "parent_session_id" in current
        ):
            return dict(current)
        for key in ("task_result", "structuredContent", "structured_content", "data"):
            nested = current.get(key)
            if isinstance(nested, Mapping):
                current = nested
                break
        else:
            return None
    raise InvokerError(
        "relay result nesting exceeds the adapter bound", reason="relay_result_invalid"
    )


def relay_error_reason(error: Any) -> str:
    """Extract the remote typed task reason, defaulting protocol failure honestly."""

    if isinstance(error, Mapping):
        for candidate in (
            error.get("reason"),
            error.get("data", {}).get("reason") if isinstance(error.get("data"), Mapping) else None,
        ):
            if isinstance(candidate, str) and candidate:
                return candidate
    return "agent_error"
