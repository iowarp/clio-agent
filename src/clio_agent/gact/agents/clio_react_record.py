"""What :class:`~clio_agent.gact.agents.clio_react.ClioReAct` records and reads back.

One step of the loop is recorded three ways, all span-correlated:

* the ARC live plane -- a ``thought`` segment (the step's visible text and its thinking
  parts with their provider continuation state, byte-exact), then a ``tool_call`` +
  ``observation`` segment per call (call id, name, arguments; result text, error flag),
  after a pre-execution ``step_open`` breadcrumb;
* the semantic highway -- ``react.step.completed`` per step (every call of the step)
  and the expert lifecycle (``expert.lifecycle.started`` / ``expert.extract.completed``
  / ``expert.lifecycle.failed``);
* the step's context for the tool observer (step thought + parent span).

Each forward first records its user message (a ``user`` segment: the question and the
user's own attachments). Nothing is wiped between forwards: the scope's plane holds the
whole conversation, and only a recorded op (compaction, delete) removes content.

The loop's context is read back from the same plane as typed ``dspy.lm15`` messages
(:func:`fold_steps`): each user message as itself, per step one assistant message
(thinking, text, tool calls) and one tool message (the results), so a step with
concurrent calls renders as the one step it was, and a later turn sees the earlier
turns as the messages they were. A read failure is a typed :class:`ContextReadError`
-- there is no fallback.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from dspy.lm15 import (
    Message,
    ThinkingPart,
    ToolCallPart,
)

from clio_agent.errors import ClioError
from clio_agent.gact.agents.clio_react_codec import (
    _text as _text,
)
from clio_agent.gact.agents.clio_react_codec import (
    fold_steps as fold_steps,
)
from clio_agent.gact.agents.clio_react_codec import (
    thinking_from_record as thinking_from_record,
)
from clio_agent.gact.agents.clio_react_codec import (
    thinking_to_record as thinking_to_record,
)
from clio_agent.gact.agents.clio_react_codec import (
    user_from_record as user_from_record,
)
from clio_agent.gact.agents.clio_react_codec import (
    user_to_record as user_to_record,
)
from clio_agent.gact.agents.context_errors import (
    ContextFoldError as ContextFoldError,
)
from clio_agent.gact.agents.context_errors import (
    ContextReadError as ContextReadError,
)
from clio_agent.gact.agents.context_errors import (
    ContextWriteError as ContextWriteError,
)
from clio_agent.gact.agents.context_reader import ContextReader
from clio_agent.gact.agents.recorded_media import (
    _media as _media,
)
from clio_agent.gact.agents.recorded_media import (
    _observation_text as _observation_text,
)
from clio_agent.gact.agents.recorded_media import (
    _split_data_url as _split_data_url,
)
from clio_agent.gact.agents.recorded_media import (
    result_part as result_part,
)
from clio_agent.gact.injection_parts import emit_injection

logger = logging.getLogger(__name__)


def arc_scope() -> tuple[Any, str, str]:
    """Resolve ``(ARCMemory, session_id, scope)`` for the live plane, or ``(None, '', '')``.

    ``arc`` is the app's clio-core ARC, or its in-memory ``HistoryPlane`` in the loud
    History mode; ``None`` whenever there is no app, no plane, or no react scope -- the
    loop then fails typed (``NoContextStoreError``). The in-process variant try index
    is folded into the ARC key only (#953).
    """
    from clio_agent.arc.history_plane import plane_for  # noqa: PLC0415
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    app = _ctx.active_app()
    scope = _ctx.run_keyed_scope(_ctx.active_react_scope())
    session = _ctx.active_react_session()
    arc = plane_for(app) if (app is not None and scope) else None
    if arc is None:
        return None, "", ""
    return arc, session, scope


def read_steps(arc: Any, session: str, scope: str) -> list[Message]:
    """Fold the scope's live plane into typed messages; typed failure on a read error.

    A one-off read; the loop keeps a :class:`ContextReader` (its recorder's) so each
    step re-folds only the open tail step.
    """
    return ContextReader(arc, session, scope).read()


# --------------------------------------------------------------------------- #
# Recording                                                                   #
# --------------------------------------------------------------------------- #
class StepRecorder:
    """Writes one expert forward's steps to the ARC live plane and the highway."""

    def __init__(self, arc: Any, session: str, scope: str, *, expert_id: str) -> None:
        from clio_agent.gact.runtime.globals import _active_semantic_turn_id  # noqa: PLC0415

        self.arc = arc
        self.session = session
        self.scope = scope
        self.expert_id = expert_id
        self.turn_id = _active_semantic_turn_id()
        self.expert_span_id = ""
        self.head_id = ""  # this forward's user message on the scope
        self._mail_head: dict[str, Any] | None = None
        self._reader = ContextReader(arc, session, scope)

    def read_steps(self) -> list[Message]:
        """The scope's context as messages; only the open tail step is re-folded."""
        return self._reader.read()

    def carry_over(self, ledger: Sequence[Any], *, allow_transcript: bool = True) -> None:
        """Restore validated context for this exact session before the current input."""
        from clio_agent.gact.agents.context_identity import restore_context

        note = restore_context(
            self.arc,
            self.session,
            self.scope,
            self.turn_id,
            ledger,
            allow_transcript=allow_transcript,
        )
        if note:
            emit_injection("context_recovery", note, agent_id=self.expert_id)

    def started(self, expert_span_id: str, inputs: Mapping[str, Any]) -> None:
        """Open the expert lifecycle on the highway."""
        from clio_agent.gact.agents.context_identity import context_identity
        from clio_agent.gact.runtime.globals import _emit_expert_lifecycle_event  # noqa: PLC0415
        from clio_agent.tools.mcp_runtime import wire_value  # noqa: PLC0415

        self.expert_span_id = expert_span_id
        _emit_expert_lifecycle_event(
            "expert.lifecycle.started",
            expert_id=self.expert_id,
            expert_span_id=expert_span_id,
            status="running",
            payload={
                "input": wire_value(dict(inputs), mode="gact_runtime"),
                "context": context_identity(self.arc, self.session, self.scope),
            },
        )

    def user_message(self, message: Message) -> None:
        """Record the forward's user message; the projection starts every turn with it."""
        content = user_to_record(message)
        from clio_agent.gact import context

        identity = context.current_turn().input_notification_id
        if identity:
            self._mail_head = content
            return  # only the confirmed input record admits this head to context
        self.head_id = self._write("user", content, 0, "")

    def commit_input(self, step: int, *, attempt: int = 0) -> None:
        """Confirm staged Core input immediately before constructing the model request."""
        from clio_agent.arc.task_types import ContextBoundary
        from clio_agent.gact import context

        app = context.active_app()
        owner = getattr(getattr(app, "state", None), "conversations", None)
        if app is None or owner is None:
            return
        assignment = context.active_assignment_handle()
        if not assignment:
            raise ValueError("Model input has no confirmed conversation assignment")
        from clio_agent.gact.agents.step_clock import mark  # noqa: PLC0415

        mark("")
        caller = owner.caller(self.session)
        identity = context.current_turn().input_notification_id
        with owner.history.serialized():
            trigger = owner.history.mail_item(self.session, identity)
        mark("commit_head")
        human_head = (
            self._mail_head
            if trigger is not None and trigger.kind in {"human", "composer"}
            else None
        )
        from clio_agent.gact.session_mail import StagedInput

        segments = self.arc.context_view(self.session, self.scope).segments
        mark("commit_view")
        committed = owner.history.committed_observations(self.session, self.scope)
        mark("commit_committed")
        from clio_agent.gact.task_access import TaskAccessDenied

        for row in segments:
            if (
                row.kind == "observation"
                and (
                    row.content.get("notification_ids")
                    or row.content.get("task_read_handles")
                    or row.content.get("task_observe_handles")
                )
                and str(row.id) not in committed
            ):
                try:
                    self._commit_collection(
                        str(row.id),
                        row.content,
                        tuple(row.content.get("notification_ids", ())),
                        str(row.content["call_id"]),
                    )
                except TaskAccessDenied:
                    # ContextReader keeps this observation behind its existing safe
                    # placeholder. Revocation must not strand unrelated accepted input.
                    continue
        mark("commit_collect")
        segments = self.arc.context_view(self.session, self.scope).segments
        mark("commit_view")
        collected = (identity,) if identity and human_head else ()
        staged = (
            StagedInput((), ())
            if context.current().react_run >= 0
            else owner.mail.stage(caller, collected=collected)
        )
        rendered = {}
        for source, text in staged.injections:
            for notification in text.notification_ids:
                item = next(item for item in staged.items if item.id == notification)
                payload = owner.history.read_content(item.content)
                if item.kind == "delegation" and isinstance(payload, dict) and item.handle:
                    from clio_agent.gact.assignment_evidence import render_delegation

                    rendered[notification] = render_delegation(owner.history, item.handle, payload)
                elif item.kind == "question_answer" and item.handle:
                    from clio_agent.gact.question_surface import render_answer_input

                    rendered[notification] = render_answer_input(
                        app, caller, item.handle, str(text)
                    )
                elif (
                    item.kind in {"human", "composer"}
                    and isinstance(payload, dict)
                    and payload.get("message")
                ):
                    from clio_agent.gact.messaging import queued_user_input

                    rendered[notification] = queued_user_input(app, self.session, payload)
                else:
                    rendered[notification] = {
                        "text": str(text),
                        "source": source,
                        "actor": "user" if source == "steer" else "algorithm",
                    }
        if identity and human_head:
            rendered[identity] = human_head
        staged = owner.mail.bound_rendered(staged, rendered)
        from clio_agent.gact.session_store import project_accepted_mail_messages

        project_accepted_mail_messages(app, owner.history, self.session, staged.items)
        mark("commit_stage")
        references = tuple(
            owner.history.write_content(self.session, rendered[item.id])
            for item in staged.items
            if item.id in rendered
        )
        boundary = ContextBoundary(
            session_id=self.session,
            scope=self.scope,
            logical_time=max((row.logical_time for row in segments), default=0),
        )
        owner.mail.commit(
            caller,
            staged,
            assignment_id=assignment,
            iteration_id=f"{self.turn_id}:{self.expert_span_id}:{step}"
            + (f":recovery:{attempt}" if attempt else ""),
            boundary=boundary,
            turn_id=self.turn_id,
            rendered_content=references,
        )
        mark("commit_mail")
        for source, text in staged.injections:
            if source != "steer":
                for notification in text.notification_ids:
                    value = rendered[notification]
                    emit_injection(source, str(value.get("text", "")), agent_id=self.expert_id)

    def injections(self, injections: Sequence[tuple[str, str]]) -> None:
        """Record changed CLIO additions; retain identical existing context for prefix reuse.

        A source whose reminder is switched off is not recorded (traced
        ``injection_inactive``; :mod:`clio_agent.injections`).
        """
        from clio_agent.injections import active  # noqa: PLC0415

        injections = [
            (src, text) for src, text in injections if active(src, session_id=self.session)
        ]
        if self.arc is None or not injections:
            return
        latest: dict[str, str] = {}
        included_ids: set[str] = set()
        for seg in self.arc.render_working_set(self.session, self.scope):
            content = getattr(seg, "content", None) or {}
            included_ids.update(content.get("notification_ids", []))
            if getattr(seg, "kind", "") == "user" and content.get("actor") == "algorithm":
                latest[str(content.get("source"))] = str(content.get("text"))
        for source, text in injections:
            identities = getattr(text, "notification_ids", ())
            if identities and all(identity in included_ids for identity in identities):
                continue
            if text and latest.get(source) != text:
                record: dict[str, Any] = {"text": str(text), "source": source, "actor": "algorithm"}
                identities = getattr(text, "notification_ids", ())
                if identities:
                    record["notification_ids"] = list(identities)
                self._write("user", record, 0, "")
                included_ids.update(identities)
                emit_injection(source, str(text), agent_id=self.expert_id)
                latest[source] = text

    def arrivals(self, arrivals: Sequence[tuple[str, str]], step: int) -> None:
        """Record what arrived mid-turn, in order; the projection reads them back.

        A ``steer`` is the user's own message; anything else (a finished child's
        result) is a CLIO addition, headed with its source.
        """
        for source, text in arrivals:
            actor = "user" if source == "steer" else "algorithm"
            record: dict[str, Any] = {"text": str(text), "source": source, "actor": actor}
            identities = getattr(text, "notification_ids", ())
            if identities:
                existing = {
                    identity
                    for segment in self.arc.render_working_set(self.session, self.scope)
                    for identity in segment.content.get("notification_ids", [])
                }
                if all(identity in existing for identity in identities):
                    continue
                record["notification_ids"] = list(identities)
            self._write("user", record, step, "")
            if actor == "algorithm":
                emit_injection(source, str(text), agent_id=self.expert_id)

    def step_open(self, step: int, span: str, text: str, calls: Sequence[ToolCallPart]) -> None:
        """The pre-execution breadcrumb: a crash mid-step still leaves the step's opening."""
        from clio_agent.arc.working_set_fold import emit_step_open  # noqa: PLC0415

        emit_step_open(
            self.arc,
            self.session,
            self.scope,
            {"thought": text, "tools": [c.name for c in calls]},
            step=step,
            turn_id=self.turn_id,
            expert_span_id=self.expert_span_id,
            run_span_id=span,
        )

    def step_done(
        self,
        step: int,
        span: str,
        *,
        text: str,
        thinking: Sequence[ThinkingPart],
        calls: Sequence[ToolCallPart],
        results: Mapping[str, tuple[Any, bool]],
        thinking_source: str = "",
        thinking_complete: bool = True,
        refs: Mapping[str, str] | None = None,
    ) -> None:
        """Write the step's segments and put ``react.step.completed`` on the highway.

        ``thinking_source`` labels the step's thinking (``provider_field`` /
        ``inline_tag``); an inline block the reply never closed is marked
        ``thinking_incomplete``. ``refs`` carries each call's ``call:`` reference,
        which the observation keeps so the model sees it with the result.
        """
        from clio_agent.gact.agents.native_result_bound import recorded_observation  # noqa: PLC0415
        from clio_agent.gact.runtime.globals import _emit_react_step_event  # noqa: PLC0415

        thought: dict[str, Any] = {
            "text": text,
            "thinking": [thinking_to_record(t, thinking_source) for t in thinking],
        }
        if not thinking_complete:
            thought["thinking_incomplete"] = True
        writes: list[tuple[str, dict[str, Any]]] = [("thought", thought)]
        collections: list[tuple[int, dict[str, Any], tuple[str, ...], str]] = []
        rows: list[dict[str, Any]] = []
        for call in calls:
            value, is_error = results.get(call.id, ("", False))
            from clio_agent.gact.tool_observation import COLLECTED, CollectedText

            notification_ids = list(value.notification_ids) if isinstance(value, COLLECTED) else []
            read_handles = list(value.read_handles) if isinstance(value, COLLECTED) else []
            observe_handles = list(value.observe_handles) if isinstance(value, COLLECTED) else []
            collect = not isinstance(value, COLLECTED) or value.collect
            value = str(value) if isinstance(value, CollectedText) else value  # stored as text
            writes.append(
                ("tool_call", {"id": call.id, "name": call.name, "args": dict(call.input)})
            )
            observation = {
                "call_id": call.id,
                "text": recorded_observation(call.name, value),
                "is_error": is_error,
                **({"call_ref": refs[call.id]} if refs and refs.get(call.id) else {}),
                **({"notification_ids": notification_ids} if notification_ids else {}),
                **({"task_read_handles": read_handles} if read_handles else {}),
                **({"task_observe_handles": observe_handles} if observe_handles else {}),
                **({"task_collection": False} if not collect else {}),
            }
            writes.append(("observation", observation))
            if notification_ids or read_handles or observe_handles:
                collections.append((len(writes) - 1, observation, tuple(notification_ids), call.id))
            rows.append(
                {
                    "id": call.id,
                    "name": call.name,
                    "args": dict(call.input),
                    "observation": value,
                    "is_error": is_error,
                }
            )
        from clio_agent.gact.agents.step_clock import mark  # noqa: PLC0415

        mark("")
        ids = self._write_many(writes, step, span)  # the step as one write (one lane put)
        mark("record_write")
        thought_id = ids[0] if ids else ""
        for position, observation, notifications, call_id in collections:
            from clio_agent.gact.task_access import TaskAccessDenied

            try:
                self._commit_collection(ids[position], observation, notifications, call_id)
            except TaskAccessDenied:
                # The original observation stays protected by ContextReader.
                # Legitimate revocation does not fail the unrelated assignment.
                logger.info("Task disclosure withheld before input commit: %s", ids[position])
        mark("record_collect")
        from clio_agent.gact import context

        owner = getattr(getattr(context.active_app(), "state", None), "conversations", None)
        if (
            owner is not None
            and context.current().react_run < 0
            and context.active_assignment_handle()
        ):
            import hashlib

            from clio_agent.arc.task_types import CoreReference
            from clio_agent.gact.task_history import _encoded

            owner.record_progress(
                context.active_assignment_handle(),
                CoreReference(
                    session_id=self.session,
                    scope=self.scope,
                    content_id=thought_id,
                    sha256=hashlib.sha256(_encoded(thought)).hexdigest(),
                ),
                tuple(call.name for call in calls),
                step,
            )
        mark("record_progress")
        _emit_react_step_event(
            expert_id=self.expert_id,
            expert_span_id=self.expert_span_id,
            step_span_id=span,
            step_index=step,
            thought=text,
            reasoning="".join(t.text for t in thinking),
            tool_calls=rows,
            is_finish=any(call.name == "submit" for call in calls),
        )
        mark("record_event")

    def _commit_collection(
        self, identity: str, content: dict[str, Any], notifications: tuple[str, ...], call_id: str
    ) -> None:
        """Delegate durable result disclosure to the existing session input owner."""
        import hashlib

        from clio_agent.arc.task_types import ContextBoundary, CoreReference
        from clio_agent.gact import context
        from clio_agent.gact.task_history import _encoded

        app = context.active_app()
        if app is None:
            raise RuntimeError("Task collection requires its owning application")
        segments = self.arc.context_view(self.session, self.scope).segments
        app.state.conversations.mail.commit_collection(
            app,
            app.state.conversations.caller(self.session),
            CoreReference(
                session_id=self.session,
                scope=self.scope,
                content_id=identity,
                sha256=hashlib.sha256(_encoded(content)).hexdigest(),
            ),
            notifications,
            assignment_id=context.active_assignment_handle(),
            iteration_id=f"tool:{self.turn_id}:{self.expert_span_id}:{call_id}",
            turn_id=self.turn_id,
            boundary=ContextBoundary(
                session_id=self.session,
                scope=self.scope,
                logical_time=max((row.logical_time for row in segments), default=0),
            ),
        )

    def completed(
        self,
        outputs: Mapping[str, Any] | None,
        step_count: int,
        *,
        extracted: list[str] | None = None,
    ) -> None:
        """Close the expert lifecycle with the final answer and structured outputs
        (``extracted``: the outputs DSPy's extract filled from the trajectory)."""
        from clio_agent.gact.runtime.globals import _emit_expert_lifecycle_event  # noqa: PLC0415

        outputs = outputs or {}
        _emit_expert_lifecycle_event(
            "expert.extract.completed",
            expert_id=self.expert_id,
            expert_span_id=self.expert_span_id,
            status="completed",
            payload={
                "output": str(outputs.get("answer", "") or ""),
                "structured": {
                    k: v for k, v in outputs.items() if k != "answer" and v not in (None, "")
                },
                "step_count": step_count,
                "extracted": list(extracted or []),
            },
        )

    def failed(self, exc: ClioError, step: int, span: str) -> None:
        """Close the lifecycle ``failed`` and record why on the plane (never raises)."""
        from clio_agent.gact.runtime.globals import _emit_expert_lifecycle_event  # noqa: PLC0415

        reason = str(getattr(exc, "reason", "") or type(exc).__name__)
        try:
            _emit_expert_lifecycle_event(
                "expert.lifecycle.failed",
                expert_id=self.expert_id,
                expert_span_id=self.expert_span_id,
                status="failed",
                payload={"reason": reason, "error": str(exc)},
            )
            if step >= 0:  # a CLIO addition the model can tell apart, never an observation
                note = {"text": f"{reason}: {exc}", "source": "turn_escalated"}
                self._write("user", {**note, "actor": "algorithm"}, step, span)
        except Exception:  # noqa: BLE001 - cleanup must never mask the real error
            logger.warning(
                "clio_react escalation cleanup failed expert_id=%s reason=%s",
                self.expert_id,
                reason,
                exc_info=True,
            )

    def cancelled(self, exc: Exception) -> None:
        """Close the expert lifecycle on cooperative cancellation without adding a note."""
        from clio_agent.gact.runtime.globals import _emit_expert_lifecycle_event  # noqa: PLC0415

        reason = str(getattr(exc, "reason", "") or type(exc).__name__)
        try:
            _emit_expert_lifecycle_event(
                "expert.lifecycle.cancelled",
                expert_id=self.expert_id,
                expert_span_id=self.expert_span_id,
                status="cancelled",
                payload={"reason": reason, "error": str(exc)},
            )
        except Exception:  # noqa: BLE001 - cleanup must never mask the real cancellation
            logger.exception(
                "clio_react cancellation cleanup failed expert_id=%s reason=%s",
                self.expert_id,
                reason,
            )

    def _write(self, kind: str, content: dict[str, Any], step: int, span: str) -> str:
        """Append one segment to the scope; its id (typed failure on a lost write)."""
        if self.arc is None:
            return ""
        try:
            segment = self.arc.append_segment(
                self.session,
                self.scope,
                kind,
                content,
                step=step,
                token_count=_token_estimate(content),
                turn_id=self.turn_id,
                expert_span_id=self.expert_span_id,
                run_span_id=span,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised typed, never swallowed
            raise ContextWriteError(self.scope, kind, exc) from exc
        return str(getattr(segment, "id", "") or "")

    def _write_many(
        self, writes: list[tuple[str, dict[str, Any]]], step: int, span: str
    ) -> list[str]:
        """Append ``(kind, content)`` segments in order as one write; their ids."""
        if self.arc is None:
            return ["" for _ in writes]
        rows = [(kind, content, _token_estimate(content)) for kind, content in writes]
        try:
            segments = self.arc.append_segments(
                self.session, self.scope, rows, step=step, turn_id=self.turn_id,
                expert_span_id=self.expert_span_id, run_span_id=span,
            )  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - re-raised typed, never swallowed
            raise ContextWriteError(self.scope, "step", exc) from exc
        return [str(getattr(segment, "id", "") or "") for segment in segments]


_MEDIA_TOKENS = 1_500  # a rough per-attachment share of the context window


def _token_estimate(content: Mapping[str, Any]) -> int:
    """A segment's rough token count: its text by length, each attachment a flat share."""
    media = content.get("media") or []
    text = {k: v for k, v in content.items() if k != "media"}
    return max(1, len(json.dumps(text, default=str)) // 4 + _MEDIA_TOKENS * len(media))
