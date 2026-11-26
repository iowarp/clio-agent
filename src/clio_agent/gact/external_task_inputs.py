"""External-owner input and result observations; SDK retains the only result driver."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
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
from clio_agent.gact import external_task_connections
from clio_agent.gact.native_work import now
from clio_agent.gact.task_publication import publish_task_notice
from clio_agent.tools.task_client import (
    AcceptedNativeCall,
)
from clio_agent.tools.task_receipt import validate_terminal

if TYPE_CHECKING:
    from clio_agent.gact.external_tasks import ExternalConnection, ExternalTasks

logger = logging.getLogger(__name__)

#: Seconds between the ``tasks/get`` polls made while a task's input question is open.
#: MCP has no task-status notification, so polling is how CLIO learns that the server
#: no longer needs the answer (answered elsewhere, or it moved on) and withdraws it.
INPUT_POLL_S = 2.0
#: Typed reason logged when a task's open question is withdrawn.
REASON_INPUT_WITHDRAWN = "external_input_withdrawn"


def question_id(handle: str, key: str, round_: int = 0) -> str:
    """The Question for input ``key`` of task ``handle`` (a new one per withdrawn round)."""
    seed = handle + "\0" + key + (f"\0{round_}" if round_ else "")
    return "task_" + hashlib.sha256(seed.encode()).hexdigest()


def _without_meta(raw: Any) -> Any:
    """An input request without the per-task ``_meta`` its server may stamp on it."""
    if not isinstance(raw, dict) or not isinstance(raw.get("params"), dict):
        return raw
    return {**raw, "params": {k: v for k, v in raw["params"].items() if k != "_meta"}}


def _open_sibling_question(
    self: ExternalTasks, host: ExternalConnection, task: TaskSnapshot, key: str, raw: Any
) -> str:
    """The open Question another task of this server holds for the same input, if any.

    A server can park several tasks on one ask (one ask_id, the same request). The
    person answers it once: the first task's Question is shown, the others wait on it.
    """
    wanted = _without_meta(raw)
    for call in tuple(host.calls.values()):
        if call.submission.handle == task.handle:
            continue
        sibling = self.history.task_snapshot(call.submission.handle)
        if (
            sibling is None
            or sibling.terminal_at is not None
            or sibling.owner.session_id != task.owner.session_id
        ):
            continue
        details = self._details(sibling)
        if key in details.get("answers", {}):
            continue
        if _without_meta(details.get("requests", {}).get(key)) != wanted:
            continue
        qid = question_id(sibling.handle, key, int(details.get("withdrawn", {}).get(key, 0)))
        question = self.history.task_snapshot(qid)
        if question is not None and question.terminal_at is None:
            return qid
    return ""


def close_input_questions(self: ExternalTasks, task: TaskSnapshot) -> None:
    """Dismiss this owner's inputs so its SDK driver can leave the answer phase."""
    details = self._details(task)
    withdrawn = details.get("withdrawn", {})
    for key in details.get("requests", {}):
        for round_ in range(int(withdrawn.get(key, 0)) + 1):
            question = question_id(task.handle, key, round_)
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
        self._unreported.discard(current.handle)  # the server has now reported on it
        if current.terminal_at is not None:
            finish_terminal(self, current.handle)
            return
        details = self._details(current)
        requests = details.setdefault("requests", {})
        for key, value in (reply.input_requests or {}).items():
            if key in requests and requests[key] != value:
                raise ValueError("Server reused an input key with changed content")
            requests[key] = value
        if requests:
            self.flagged.asked(current.handle)
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
        if reply.meta:  # the latest status/tasks-get _meta a server sent, kept as sent
            updates["metadata"] = reply.model_dump(mode="json", by_alias=True)["_meta"]
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
    if observed.terminal_at is not None:
        self.flagged.settled(
            self.app,
            observed.handle,
            reply.result,
            reply,
            failed=observed.status != "completed",
        )
    if observed.terminal_at is not None and reply.status == "completed" and reply.result:
        from clio_agent.gact.server_surfaces import apply_task_result  # noqa: PLC0415

        apply_task_result(
            self.app,
            current.owner.session_id,
            host.backend.server,
            current.tool or "",
            observed.handle,
            reply.result,
        )


def _request_meta(
    task: TaskSnapshot, key: str, raw: dict[str, Any], context_meta: Any = None
) -> dict[str, Any] | None:
    """An input request's namespaced ``_meta``: its own, over its carrier's.

    A 2026-07-28 embedded request has no ``_meta``; a server carries typed data for it
    on the ``tasks/get`` / status reply (kept on the task as ``metadata``) or, for a
    plain call, on the ``InputRequiredResult`` (the input context's ``meta``,
    :meth:`clio_agent.tools.task_client.TaskAwareClient._drive_input_required`).
    """

    from clio_agent.gact.elicitation_schema import namespaced_meta  # noqa: PLC0415

    own = (raw.get("params") or {}).get("_meta") if isinstance(raw, dict) else None
    carried = context_meta if key.startswith("inline:") else task.metadata
    return namespaced_meta(own, carried)


async def _answered_or_left(
    host: ExternalConnection, task: TaskSnapshot, key: str, waiting: asyncio.Future[Any]
) -> str:
    """Wait for the answer, polling ``tasks/get``; return why the input is no longer needed.

    ``""`` means the question settled (answered, dismissed or its wait ended). Otherwise
    the server stopped asking: the task left ``input_required`` (another task's answer
    moved it, it was cancelled, it finished) or no longer lists this input.
    """
    task_id = task.backend.task_id if task.backend is not None else ""
    try:
        while True:
            done, _ = await asyncio.wait({waiting}, timeout=INPUT_POLL_S)
            if done:
                waiting.result()
                return ""
            call = host.calls.get(task_id) if task_id else None
            if call is None or host.closing or key.startswith("inline:"):
                continue  # nothing to poll; the question alone decides
            try:
                reply = await call.task.status()
            except Exception as error:  # noqa: BLE001 - a failed poll just retries
                logger.debug("External input poll failed task=%s: %s", task.handle, error)
                continue
            if reply.status != "input_required":
                return reply.status
            if key not in (reply.input_requests or {}):
                return "input_changed"
    finally:
        if not waiting.done():
            waiting.cancel()
            with contextlib.suppress(BaseException):
                await waiting


def _withdraw(
    self: ExternalTasks, task: TaskSnapshot, key: str, qid: str, reason: str, last: bool
) -> mcp_types.ElicitResult | None:
    """Withdraw a prompt the task no longer needs; ``None`` when it was answered first."""
    with self.history.serialized():
        question = self.history.task_snapshot(qid)
        if question is not None and question.status == "completed":
            return None  # answered meanwhile: deliver that answer
        current = self.history.task_snapshot(task.handle)
        details = self._details(current)
        if key in details.get("answers", {}):
            return mcp_types.ElicitResult.model_validate(details["answers"][key])
        if last and question is not None and question.terminal_at is None:
            self.app.state.questions.cancel(qid)
        details.setdefault("withdrawing", {})[key] = True
        rounds = details.setdefault("withdrawn", {})
        rounds[key] = int(rounds.get(key, 0)) + 1  # a later re-ask gets a fresh prompt
        reference = self.history.write_content(task.owner.session_id, details)
        self.history.record_snapshot(
            current.model_copy(update={"details": reference, "updated_at": now()}),
            observation_id=uuid4().hex,
        )
    logger.info(
        "Task input reason=%s task=%s key=%s status=%s prompt_closed=%s",
        REASON_INPUT_WITHDRAWN,
        task.handle,
        key,
        reason,
        last,
    )
    return mcp_types.ElicitResult(action="cancel")


def _covered_confirm(
    self: ExternalTasks,
    task: TaskSnapshot,
    current: TaskSnapshot,
    details: dict[str, Any],
    key: str,
    qid: str,
    params: Any,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Answer a plain confirm of a call a person already allowed; ``None`` to ask.

    Runs under the history lock, right after the Question was posted: it stays under
    the call, answered, with the server's message and the typed reason.
    """
    from clio_agent.gact import mcp_confirm_coverage as coverage  # noqa: PLC0415

    content = coverage.plain_confirm(params, meta)
    if content is None:
        return None
    permission = coverage.person_allowed(self.app, task.owner.session_id, task.invocation_id)
    if not permission:
        return None
    self.app.state.questions.answer(
        qid,
        QuestionReply(
            answer=coverage.COVERED_ANSWER,
            selected_options=("yes",) if content else (),
            metadata=content,
            action="accept",
        ),
    )
    decision = {"reason": coverage.REASON_COVERED, "permission_id": permission}
    self.app.state.questions.freeze_resolution(qid, decision)
    value = mcp_types.ElicitResult(action="accept", content=content).model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    details.setdefault("answers", {})[key] = value
    details.setdefault("covered_inputs", {})[key] = decision
    reference = self.history.write_content(task.owner.session_id, details)
    self.history.record_snapshot(
        current.model_copy(update={"details": reference, "updated_at": now()}),
        observation_id=uuid4().hex,
    )
    logger.info(
        "Task input reason=%s task=%s key=%s permission=%s",
        coverage.REASON_COVERED,
        task.handle,
        key,
        permission,
    )
    return value


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
        inline = host.inline.get() if host.pool is None else None
        if inline is None and host.pool is not None and host.rpcs:
            # Parallel calls share a kept-alive connection; inputs naming a task
            # (task-<id>-...) went above. The rest belong to the call waiting on a plain
            # reply, when only one is: several make it ambiguous, and it is refused.
            waiting = external_task_connections.waiting_inline(host)
            if len(waiting) == 1 and not any(
                str(request.request_id).startswith(f"task-{task_id}-") for task_id in host.calls
            ):
                inline = waiting[0]
        elif inline is None and host.isolated and host.rpcs and not host.calls:
            inline = host.invocation
        if inline is not None and any(
            call.submission.handle == inline.handle for call in host.calls.values()
        ):
            inline = None
        if not candidates and inline is not None:
            lazy = self._lazy.pop(inline.handle, None)
            if lazy is not None:  # a read-only call asking for input: admit it first
                lazy.ensure()
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
    self.flagged.asked(task.handle)  # a server-confirmed call's card reached the person
    url_scope = None
    if getattr(params, "mode", "form") == "url":
        from clio_agent.gact.off_loop import run_off_loop
        from clio_agent.gact.server_surfaces import server_scope

        url_scope = await run_off_loop(
            server_scope, self.app, task.owner.session_id, host.backend.server
        )

    def question() -> tuple[str, Any | None, bool]:
        with self.history.serialized():
            current = self.history.task_snapshot(task.handle)
            details = self._details(current)
            if key in details.get("answers", {}):
                return "", details["answers"][key], False
            qid = question_id(task.handle, key, int(details.get("withdrawn", {}).get(key, 0)))
            pending = self.history.task_snapshot(qid)
            if pending is not None and pending.raw_status == "answered":
                # The Question commit precedes the callback's canonical-response
                # copy. Recover that answer through the existing post-wait path;
                # later cancellation cannot replace it with a synthesized reply.
                return qid, None, False
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
                return "", value, False
            if pending is None or not pending.raw_status:
                shared = _open_sibling_question(self, host, current, key, raw)
                if shared:  # one prompt per ask: this task waits on the one shown
                    return shared, None, True
                from clio_agent.gact.elicitation_config import trusted_origins
                from clio_agent.gact.elicitation_schema import (
                    build_form_metadata,
                    build_url_metadata,
                    classify_url_trust,
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
                    declared = dict((url_scope.host if url_scope else {}).get("elicitation") or {})
                    problem, origin_trust = classify_url_trust(
                        params.url,
                        trusted_origins(),
                        plugin_origins=tuple(declared.get("origins") or ()),
                        loopback=bool(declared.get("loopback")),
                        launched="stdio" in host.backend.transport.casefold(),
                    )
                    metadata = build_url_metadata(
                        params.url,
                        **correlation,
                        origin_trust=origin_trust,
                        plugin=url_scope.plugin if url_scope else "",
                        server=url_scope.server if url_scope else host.backend.server,
                        plugin_title=url_scope.owner.get("plugin_title", "") if url_scope else "",
                        meta=_request_meta(current, key, raw, getattr(request, "meta", None)),
                    )
                    definition = QuestionDefinition.model_validate(
                        {
                            "prompt": params.message,
                            "source": "mcp_elicitation",
                            "metadata": metadata,
                        }
                    )
                else:
                    request_meta = _request_meta(current, key, raw, getattr(request, "meta", None))
                    translated = translate_form_schema(params.requested_schema, request_meta)
                    problem = translated.degrade
                    metadata = build_form_metadata(
                        translated,
                        **correlation,
                        meta=request_meta,
                    )
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
                    return "", value, False
                created = self.app.state.questions.create(
                    task.owner.model_copy(update={"parent_handle": task.handle}),
                    definition,
                    invocation_id=task.invocation_id,
                    handle=qid,
                )
                if created.raw_status == "pending" and mode == "form":
                    covered = _covered_confirm(
                        self,
                        task,
                        current,
                        details,
                        key,
                        qid,
                        params,
                        metadata["elicitation"].get("meta"),
                    )
                    if covered is not None:
                        return "", covered, False
            return qid, None, False

    qid, previous, shared = await self.history.io.run(question)
    if previous is not None:
        return mcp_types.ElicitResult.model_validate(previous)
    from clio_agent.gact.agent_elicitation import audience_hint, await_question_input

    if shared:
        caller = self.app.state.conversations.caller(task.owner.session_id)
        waiting = asyncio.ensure_future(self.app.state.task_operations.wait(caller, qid))
    else:
        waiting = asyncio.ensure_future(
            await_question_input(
                self.app,
                qid,
                namespace=host.backend.server,
                audience=audience_hint(params),
                mode=getattr(params, "mode", "form"),
            )
        )
    waiters = self.question_waiters.setdefault(qid, set())
    waiters.add(task.handle)
    try:
        left = await _answered_or_left(host, task, key, waiting)
    finally:
        waiters.discard(task.handle)
        if not waiters:
            self.question_waiters.pop(qid, None)
    if left:
        withdrawn = await self.history.io.run(
            _withdraw, self, task, key, qid, left, not self.question_waiters.get(qid)
        )
        if withdrawn is not None:
            return withdrawn
    elif shared:
        settled = await self.history.io.run(self.history.task_snapshot, qid)
        if settled is None or settled.status != "completed":
            # The task that showed this prompt left before it was answered; this task
            # still asks, so it gets a prompt of its own.
            return await _input(self, host, params, request)

    def answer() -> Any:
        with self.history.serialized():
            q = self.history.task_snapshot(qid)
            if q.status == "completed":
                reply = QuestionReply.model_validate(self.history.read_content(q.result))
                approval = {"comment": reply.comment} if reply.comment else None
                value = mcp_types.ElicitResult.model_validate(
                    {
                        "action": reply.action,
                        "content": reply.metadata
                        if reply.action == "accept" and getattr(params, "mode", "form") == "form"
                        else None,
                        "_meta": {"io.iowarp/approval": approval} if approval else None,
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
