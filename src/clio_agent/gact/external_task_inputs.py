"""External-owner input and result observations; SDK retains the only result driver."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import mcp_types
from fastmcp_tasks.client_models import ClientGetTaskResult

from clio_agent.arc.task_types import (
    QuestionDefinition,
    QuestionReply,
    TaskProgress,
    TaskSnapshot,
)
from clio_agent.gact.native_work import now
from clio_agent.gact.task_publication import publish_task_notice
from clio_agent.tools.task_client import (
    AcceptedNativeCall,
)
from clio_agent.tools.task_receipt import validate_terminal

if TYPE_CHECKING:
    from clio_agent.gact.external_tasks import ExternalConnection, ExternalTasks


def close_input_questions(self: ExternalTasks, task: TaskSnapshot) -> None:
    """Dismiss this owner's inputs so its SDK driver can leave the answer phase."""
    for key in self._details(task).get("requests", {}):
        question = "task_" + hashlib.sha256((task.handle + "\0" + key).encode()).hexdigest()
        if self.history.task_snapshot(question) is not None:
            # The Question command preserves confirmed answers and republishes
            # missing notices if a prior closure persisted before publication.
            self.app.state.questions.cancel(question)


def finish_terminal(self: ExternalTasks, handle: str) -> None:
    """Finish publication from the saved outcome; retries never reopen execution."""
    with self.history.serialized():
        current = self.history.task_snapshot(handle)
        if current is None or current.terminal_at is None:
            return
        publish_task_notice(self.history, current)
        close_input_questions(self, current)
        self.history.task_obligation_settled(current)


def _record_reply(
    self: ExternalTasks,
    host: ExternalConnection,
    accepted: AcceptedNativeCall,
    reply: ClientGetTaskResult,
) -> None:
    with self.history.serialized():
        current = self.history.task_snapshot(accepted.submission.handle)
        if (
            current is None
            or current.submission != accepted.submission.context
            or current.backend != host.backend.model_copy(update={"task_id": accepted.task.task_id})
            or reply.task_id != accepted.task.task_id
        ):
            raise ValueError("External reply differs from its captured submission/backend identity")
        if current.terminal_at is not None:
            finish_terminal(self, current.handle)
            return
        details = self._details(current)
        requests = details.setdefault("requests", {})
        for key, value in (reply.input_requests or {}).items():
            if key in requests and requests[key] != value:
                raise ValueError("Server reused an input key with changed content")
            requests[key] = value
        detail_ref = (
            self.history.write_content(current.owner.session_id, details)
            if reply.input_requests
            else current.details
        )
        updates = {
            "raw_status": reply.status,
            "updated_at": now(),
            "connection_freshness": "fresh",
            "details": detail_ref,
            "attention_episode": None,
            "progress": TaskProgress(message=reply.status_message or ""),
            "status": "input_required" if reply.status == "input_required" else "running",
            "activity": "waiting_for_input" if reply.status == "input_required" else "executing",
        }
        if reply.status in {"completed", "failed", "cancelled"}:
            result = self.history.write_content(
                current.owner.session_id, reply.model_dump(mode="json")
            )
            status = "failed" if reply.result and reply.result.get("isError") else reply.status
            try:
                validate_terminal(accepted.submission.output_schema, reply)
            except ValueError as error:
                status = "failed"
                updates["progress"] = TaskProgress(message=str(error))
            updates.update(
                status=status,
                activity="",
                terminal_at=now(),
                settlement="external_acknowledged",
                result=result,
                backend_observation=result,
                supported_actions=("get_result",),
            )
            # The server owns protocol settlement. This is not proof that every
            # remote resource was cleaned up, even when its terminal reply arrives
            # before our deferred control-intent record.
            if reply.status == "cancelled":
                updates["progress"] = TaskProgress(
                    message="The server reported cancellation; remote cleanup remains unverified."
                )
        observed = current.model_copy(update=updates)
        self.history.record_snapshot(observed, observation_id=uuid4().hex)
        if observed.terminal_at is not None:
            finish_terminal(self, observed.handle)


async def _input(self: ExternalTasks, host: ExternalConnection, params: Any, request: Any) -> Any:
    if host.closing or host.client is None or request.session is not host.client.session:
        raise ValueError("MCP input does not belong to a live bound transport session")

    def resolve_locked() -> tuple[TaskSnapshot, str, dict[str, Any]]:
        candidates = []
        payload = params.model_dump(mode="json", by_alias=True, exclude_none=True)
        for task_id, call in host.calls.items():
            current = self.history.task_snapshot(call.submission.handle)
            if current is None or current.backend != host.backend.model_copy(
                update={"task_id": task_id}
            ):
                continue
            for key, raw in self._details(current).get("requests", {}).items():
                if (
                    str(request.request_id) == f"task-{task_id}-{key}"
                    and raw.get("params") == payload
                ):
                    candidates.append((current, key, raw))
        inline = host.inline.get()
        if inline is None and host.isolated and host.rpcs and not host.calls:
            inline = host.invocation
        if inline is not None and any(
            call.submission.handle == inline.handle for call in host.calls.values()
        ):
            inline = None
        if not candidates and inline is not None:
            current = self.history.task_snapshot(inline.handle)
            if (
                current is None
                or current.backend is not None
                or current.submission is None
                or inline.backend != host.backend
                or self.history.read_content(current.submission) != inline.model_dump(mode="json")
            ):
                raise ValueError("Inline input has no confirmed invocation")
            key = "inline:" + str(request.request_id)
            details = self._details(current)
            raw = {"method": "elicitation/create", "params": payload}
            saved = details.setdefault("requests", {}).get(key)
            if saved is not None and saved != raw:
                raise ValueError("Inline input identity was reused with changed content")
            details["requests"][key] = raw
            reference = self.history.write_content(current.owner.session_id, details)
            self.history.record_snapshot(
                current.model_copy(update={"details": reference, "updated_at": now()}),
                observation_id=uuid4().hex,
            )
            return current, key, raw
        if len(candidates) != 1:
            raise ValueError("MCP input identity/payload is absent or ambiguous")
        return candidates[0]

    def resolve() -> tuple[TaskSnapshot, str, dict[str, Any]]:
        with self.history.serialized():
            return resolve_locked()

    task, key, raw = await self.history.io.run(resolve)

    def question() -> tuple[str, Any | None]:
        with self.history.serialized():
            current = self.history.task_snapshot(task.handle)
            details = self._details(current)
            if key in details.get("answers", {}):
                return "", details["answers"][key]
            qid = "task_" + hashlib.sha256((task.handle + "\0" + key).encode()).hexdigest()
            pending = self.history.task_snapshot(qid)
            if pending is not None and pending.raw_status == "answered":
                # The Question commit precedes the callback's canonical-response
                # copy. Recover that answer through the existing post-wait path;
                # later cancellation cannot replace it with a synthesized reply.
                return qid, None
            cancelled = current.cancellation_requested_at is not None or self.recovery.is_pending(
                "external-cancel:" + task.handle
            )
            try:
                self.app.state.conversations.require_admission(current)
            except ValueError:
                # This includes the ancestor's pre-persistence cancellation hold.
                # Never create a late Question outside its captured member set.
                cancelled = True
            if cancelled:
                value = mcp_types.ElicitResult(action="cancel").model_dump(
                    mode="json", by_alias=True, exclude_none=True
                )
                details.setdefault("answers", {})[key] = value
                reference = self.history.write_content(task.owner.session_id, details)
                self.history.record_snapshot(
                    current.model_copy(update={"details": reference, "updated_at": now()}),
                    observation_id=uuid4().hex,
                )
                return "", value
            if pending is None or not pending.raw_status:
                from clio_agent.gact.elicitation_config import trusted_origins
                from clio_agent.gact.elicitation_schema import (
                    build_form_metadata,
                    build_url_metadata,
                    check_url_trust,
                    translate_form_schema,
                )

                mode = getattr(params, "mode", "form")
                correlation = {
                    "request_id": request.request_id,
                    "namespace": host.backend.server,
                    "tool_name": task.tool,
                    "invocation_id": task.invocation_id,
                    "forwarded_from_session": task.owner.session_id,
                    "task_id": task.backend.task_id if task.backend else None,
                    "input_key": key,
                }
                if mode == "url":
                    problem = check_url_trust(params.url, trusted_origins())
                    metadata = build_url_metadata(params.url, **correlation)
                    definition = QuestionDefinition.model_validate(
                        {
                            "prompt": params.message,
                            "source": "mcp_elicitation",
                            "metadata": metadata,
                        }
                    )
                else:
                    translated = translate_form_schema(params.requested_schema)
                    problem = translated.degrade
                    metadata = build_form_metadata(translated, **correlation)
                    definition = QuestionDefinition.model_validate(
                        {
                            "prompt": params.message,
                            "kind": translated.kind,
                            "options": [
                                option.model_dump(mode="json") for option in translated.options
                            ],
                            "requested_schema": params.requested_schema,
                            "source": "mcp_elicitation",
                            "metadata": metadata,
                        }
                    )
                if problem:
                    value = mcp_types.ElicitResult(action="decline").model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    )
                    details.setdefault("answers", {})[key] = value
                    details.setdefault("declined_inputs", {})[key] = problem
                    reference = self.history.write_content(task.owner.session_id, details)
                    self.history.record_snapshot(
                        current.model_copy(update={"details": reference, "updated_at": now()}),
                        observation_id=uuid4().hex,
                    )
                    return "", value
                self.app.state.questions.create(
                    task.owner.model_copy(update={"parent_handle": task.handle}),
                    definition,
                    invocation_id=task.invocation_id,
                    handle=qid,
                )
            return qid, None

    qid, previous = await self.history.io.run(question)
    if previous is not None:
        return mcp_types.ElicitResult.model_validate(previous)
    from clio_agent.gact.agent_elicitation import audience_hint, await_question_input

    await await_question_input(
        self.app,
        qid,
        namespace=host.backend.server,
        audience=audience_hint(params),
        mode=getattr(params, "mode", "form"),
    )

    def answer() -> Any:
        with self.history.serialized():
            q = self.history.task_snapshot(qid)
            if q.status == "completed":
                reply = QuestionReply.model_validate(self.history.read_content(q.result))
                value = mcp_types.ElicitResult.model_validate(
                    {
                        "action": reply.action,
                        "content": reply.metadata
                        if reply.action == "accept" and getattr(params, "mode", "form") == "form"
                        else None,
                    }
                )
            else:
                value = mcp_types.ElicitResult(action="cancel")
            current = self.history.task_snapshot(task.handle)
            details = self._details(current)
            details.setdefault("answers", {})[key] = value.model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
            reference = self.history.write_content(task.owner.session_id, details)
            self.history.record_snapshot(
                current.model_copy(update={"details": reference, "updated_at": now()}),
                observation_id=uuid4().hex,
            )
            return value

    return await self.history.io.run(answer)
