"""Relay-backed ExpertInvoker (#1126) — owner module split out of ``invoker.py``.

Maps the same serializable ``TaskSpec`` request onto durable relay remote-agent
jobs (``relay_submit_agent``) and folds their observations into the same
``AgentTaskRegistry`` the in-process invoker feeds. The #671 seam itself — the
``ExpertInvoker`` protocol, the ``TaskHandle``/``TaskResult``/``TaskEvent`` wire
shapes, and the ``RELAY_STATE_MAP`` observation catalog — stays in
``invoker.py``; this module owns only the relay-backed implementation.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from clio_agent.gact.agent_message_transport import message_via_relay
from clio_agent.gact.agent_tasks import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    TERMINAL_STATUSES,
    AgentTask,
    persist_agent_task,
    seed_agent_task,
)
from clio_agent.gact.agents.invoker import (
    RELAY_STATE_MAP,
    InvokerError,
    TaskHandle,
    TaskResult,
)
from clio_agent.gact.agents.relay_invoker_runtime import (
    RelayEventPump,
    RelayInvokerRuntime,
    find_task_result_wire,
    relay_error_reason,
    relay_job_failure_reason,
)
from clio_agent.gact.spawn_context import validate_task_spec
from clio_agent.gact.task_fold import fold_agent_task_event
from clio_agent.gact.turn_spawn import TaskSpec

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = ["RelayExpertInvoker"]


class RelayExpertInvoker:
    """Relay-backed ExpertInvoker using one durable task identity across clients.

    The client factory is owner-session aware: every operation asks it for a fresh
    client bound to the handle parent. Relay task records supply reconnect identity;
    this class keeps no second remote-task registry.
    """

    def __init__(
        self,
        app: "FastAPI",
        client_factory: Callable[[str], Any],
        *,
        cluster: str,
        prompt_path: str,
        mcp_config_path: str | None = None,
        model: str | None = None,
        workdir: str | None = None,
    ) -> None:
        if not cluster.strip():
            raise ValueError("relay cluster must be a non-empty string")
        if not prompt_path.strip():
            raise ValueError("relay prompt_path must be a non-empty string")
        self._app = app
        self._runtime = RelayInvokerRuntime(client_factory, cluster=cluster)
        self._placement = f"relay:{cluster}"
        self._events = RelayEventPump(app, client_factory)
        self._prompt_path = prompt_path
        self._mcp_config_path = mcp_config_path
        self._model = model
        self._workdir = workdir
        self._spawn_lock = threading.Lock()

    @property
    def app(self) -> "FastAPI":
        """The FastAPI app whose durable AgentTask owner receives relay folds."""

        return self._app

    def remote_agent_task_spec(self, spec: TaskSpec) -> dict[str, Any]:
        """Map one self-contained TaskSpec to the relay RemoteAgentTaskSpec wire.

        #1222: the door's real ``relay_submit_agent`` ``inputSchema`` is
        ``additionalProperties: false`` and carries no inline task-content argument
        (confirmed live against ``127.0.0.1:18796/mcp`` and the installed
        ``clio_relay`` package source) -- ``context`` is REJECTED at submission,
        never reaching a relay job. ``prompt_path`` is a fixed, app-wide system
        prompt (see ``relay_wiring.py::configure_relay_expert_invokers``), not a
        per-task channel. The ONE exposed per-task channel is the bounded
        post-admission follow-up round this opts into (``request_followup_message``);
        ``invoke()`` answers it immediately below with ``spec``'s own task text.
        """

        return {
            "prompt_path": self._prompt_path,
            "mcp_config_path": self._mcp_config_path,
            "model": self._model,
            "workdir": self._workdir,
            "request_followup_message": True,
        }

    def invoke(self, spec: TaskSpec) -> TaskHandle:
        """Submit remote_agent work and return the relay job id as the task id."""

        workspace_id, session_mode, scope = validate_task_spec(self._app, spec)
        spec = replace(spec, placement=self._placement)
        identity, current = self._runtime.submit_and_poll(
            spec.parent_session_id,
            self.remote_agent_task_spec(spec),
        )
        if str(getattr(current, "status", "")) == "input_required":
            # #1222: deliver the spawn's own task text over the one bounded
            # post-admission round the submission opted into -- this relays what the
            # parent already decided at spawn time, over the door's only exposed
            # per-task channel; it does not decide anything new on the parent's behalf.
            self._runtime.message(spec.parent_session_id, identity.key, spec.task_text)
            current = self._runtime.poll(spec.parent_session_id, identity.key)
        observation, _projection = self._relay_projection(current)
        # Only local run-index allocation + registry mutation need serialization.
        # The relay submit/poll round trip above is independent per invocation and
        # must remain concurrent for advertised parallel fan-out.
        with self._spawn_lock:
            from clio_agent.gact.turn_spawn import _next_run_index  # noqa: PLC0415

            run_index = _next_run_index(self._app, spec)
            seeded = seed_agent_task(
                self._app,
                parent_session_id=spec.parent_session_id,
                agent_ref={
                    "expert_id": spec.child_expert_id,
                    "requesting_expert_id": spec.requesting_expert_id,
                },
                parent_turn_id=spec.parent_turn_id,
                depth=spec.depth,
                task_id=identity.task_id,
                workspace_id=workspace_id,
                session_mode=session_mode,
                session_scope_metadata=scope,
                run_index=run_index,
                fanout_bound=spec.fanout_bound,
                queued_reason="",
                placement=self._placement,
                host=self._placement.split(":", 1)[1],
                spawn_group_id=spec.spawn_group_id,
                group_size=spec.group_size,
            )
            handle = TaskHandle.from_task(seeded)
            self._apply_poll(handle, current)
            current_task = self._require_local_task(handle)
            handle = TaskHandle.from_task(current_task)
        self._start_event_pump(handle)
        return handle

    def wait(self, handle: TaskHandle, timeout_s: float) -> TaskResult:
        """Reconnect by retained task id and wait within the caller's budget.

        A timeout returns the latest non-terminal record. A fresh client and the
        persisted composite task key are used on every call, so losing the client
        that submitted the work does not lose the job.
        """

        local = self._require_local_task(handle)
        if local.is_terminal:
            return TaskResult.from_task(local)
        if timeout_s <= 0:
            return self.check([handle])[0]
        self._start_event_pump(handle)
        key = self._runtime.task_key(handle)
        try:
            current = self._runtime.resume(handle.parent_session_id, key, timeout_s)
        except TimeoutError:
            current = self._runtime.poll(handle.parent_session_id, key)
        self._apply_poll(handle, current)
        return TaskResult.from_task(self._require_local_task(handle))

    def check(self, handles: Sequence[TaskHandle]) -> list[TaskResult]:
        """Poll relay once per handle and preserve caller order."""

        results: list[TaskResult] = []
        for handle in handles:
            local = self._require_local_task(handle)
            if not local.is_terminal:
                self._start_event_pump(handle)
                key = self._runtime.task_key(handle)
                current = self._runtime.poll(handle.parent_session_id, key)
                self._apply_poll(handle, current)
                local = self._require_local_task(handle)
            results.append(TaskResult.from_task(local))
        return results

    def cancel(self, handle: TaskHandle) -> bool:
        """Request cooperative cancellation and return after the relay ack.

        The acknowledgement is not terminal evidence. The local task remains
        non-terminal until a later check or wait observes canonical relay state.
        """

        local = self._require_local_task(handle)
        if local.is_terminal:
            return False
        key = self._runtime.task_key(handle)
        self._runtime.cancel(handle.parent_session_id, key)
        return True

    def message(self, handle: TaskHandle, text: str, metadata: Any = None) -> None:
        message_via_relay(self, handle, text, metadata)

    def _task_key(self, handle: TaskHandle) -> Any:
        return self._runtime.task_key(handle)

    def _require_local_task(self, handle: TaskHandle) -> AgentTask:
        task = self._app.state.agent_task_registry.get(handle.task_id)
        if task is None:
            raise InvokerError(f"unknown task {handle.task_id!r}", reason="unknown_task")
        if (
            task.parent_session_id != handle.parent_session_id
            or task.child_session_id != handle.child_session_id
        ):
            raise InvokerError(
                f"task handle identity disagrees for {handle.task_id!r}",
                reason="task_identity_mismatch",
            )
        return task

    @staticmethod
    def _result_is_error(result: Any) -> bool:
        if not isinstance(result, Mapping):
            return False
        if result.get("isError") is True or result.get("is_error") is True:
            return True
        structured = result.get("structuredContent")
        return isinstance(structured, Mapping) and structured.get("isError") is True

    def _relay_projection(self, current: Any) -> tuple[str, dict[str, str | bool]]:
        status = str(getattr(current, "status", ""))
        observation = str(getattr(current, "relay_state", "") or "")
        if not observation and status == "working":
            message = str(getattr(current, "status_message", "") or "")
            prefix = "Relay job is "
            if message.startswith(prefix):
                observation = message[len(prefix) :].strip()
        if not observation:
            if status == "input_required":
                observation = "input_required"
            elif status == "completed":
                observation = (
                    "tool-fail"
                    if self._result_is_error(getattr(current, "result", None))
                    else "succeeded"
                )
            elif status == "failed":
                observation = "protocol"
            elif status == "cancelled":
                observation = "canceled"
        projection = RELAY_STATE_MAP.get(observation)
        if projection is None:
            reason = "relay_state_missing" if not observation else "relay_state_unknown"
            raise InvokerError(
                f"relay task status {status!r} has no committed observation {observation!r}",
                reason=reason,
            )
        if projection.get("status") != status:
            raise InvokerError(
                f"relay observation {observation!r} projects to "
                f"{projection.get('status')!r}, not {status!r}",
                reason="relay_state_mismatch",
            )
        expected_error = projection.get("isError")
        if expected_error is not None and expected_error is not self._result_is_error(
            getattr(current, "result", None)
        ):
            raise InvokerError(
                f"relay observation {observation!r} disagrees with isError",
                reason="relay_state_mismatch",
            )
        return observation, projection

    @staticmethod
    def _agent_status(observation: str, projection: Mapping[str, str | bool]) -> str:
        if observation == "queued":
            return STATUS_QUEUED
        status = projection["status"]
        if status in {"working", "input_required"}:
            return STATUS_RUNNING
        if status == "completed":
            return STATUS_COMPLETED
        if status == "failed":
            return STATUS_FAILED
        if status == "cancelled":
            return STATUS_CANCELLED
        raise InvokerError(f"unknown projected status {status!r}", reason="unknown_status")

    def _apply_poll(self, handle: TaskHandle, current: Any) -> None:
        observation, projection = self._relay_projection(current)
        target = self._agent_status(observation, projection)
        local = self._require_local_task(handle)
        if local.is_terminal:
            return
        live_state = "input_required" if observation == "input_required" else target
        if local.status == target:
            if local.live_state != live_state:
                persist_agent_task(self._app, replace(local, live_state=live_state))
            return
        if local.status == STATUS_QUEUED and target != STATUS_QUEUED:
            running = replace(
                TaskResult.from_task(local), status=STATUS_RUNNING, live_state=live_state
            )
            fold_agent_task_event(self._app, running)
            local = self._require_local_task(handle)
        if target == STATUS_RUNNING:
            if local.live_state != live_state:
                persist_agent_task(self._app, replace(local, live_state=live_state))
            return
        terminal = self._terminal_result(handle, local, current, target)
        fold_agent_task_event(self._app, terminal)

    def _terminal_result(
        self,
        handle: TaskHandle,
        local: AgentTask,
        current: Any,
        target: str,
    ) -> TaskResult:
        wire = find_task_result_wire(getattr(current, "result", None))
        if wire is not None:
            remote_task_id = str(wire.get("task_id") or "")
            if remote_task_id and remote_task_id != handle.task_id:
                raise InvokerError(
                    "relay TaskResult task_id disagrees with the retained handle",
                    reason="task_identity_mismatch",
                )
            merged = {
                **TaskResult.from_task(local).to_wire(),
                **wire,
                "task_id": handle.task_id,
                "parent_session_id": handle.parent_session_id,
                "child_session_id": handle.child_session_id,
            }
            result = TaskResult.from_wire(merged)
            if result.status not in TERMINAL_STATUSES:
                raise InvokerError(
                    "relay terminal response carried a non-terminal TaskResult",
                    reason="relay_result_invalid",
                )
            return result
        # #1222: the real relay_submit_agent completion envelope is a raw JARVIS-CD
        # job/artifact record, never the TaskResult boundary shape above -- check
        # for the door's OWN failure signal before falling through to the strict
        # boundary-shape error, so a genuinely failed remote job (e.g. a cluster
        # missing its JARVIS-CD executable) surfaces honestly instead of an opaque
        # shape-mismatch. ``error_reason`` stays the generic typed catch-all
        # (AgentTaskRegistry.transition rejects any reason outside its closed
        # ERROR_REASONS vocabulary -- no free-form strings on that field); the raw
        # relay detail travels in ``result.answer_excerpt`` instead, same as any
        # other tool-fail completion's message body.
        relay_failure = relay_job_failure_reason(getattr(current, "result", None))
        if relay_failure is not None:
            return replace(
                TaskResult.from_task(local),
                status=STATUS_FAILED,
                error_reason="agent_error",
                result={"message_ref": "", "answer_excerpt": relay_failure, "workflow_state": {}},
            )
        if target == STATUS_CANCELLED:
            return replace(TaskResult.from_task(local), status=STATUS_CANCELLED)
        if target == STATUS_FAILED:
            reason = relay_error_reason(getattr(current, "error", None))
            return replace(
                TaskResult.from_task(local),
                status=STATUS_FAILED,
                error_reason=reason,
            )
        raise InvokerError(
            "relay completion omitted its TaskResult boundary record",
            reason="relay_result_invalid",
        )

    def _start_event_pump(self, handle: TaskHandle) -> None:
        self._events.start(handle, self._runtime.task_key(handle))
