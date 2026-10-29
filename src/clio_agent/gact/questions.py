"""Question commands over the common Core task record, without blocking ask modes."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError
from referencing import Registry

from clio_agent.arc.task_types import (
    BackendReference,
    QuestionDefinition,
    QuestionOption,
    QuestionReply,
    TaskOwner,
    TaskSnapshot,
    question_surface_resource,
)
from clio_agent.gact.task_access import TaskAccess, TaskCaller
from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.task_publication import publish_task_notice


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ObsoleteQuestionGate(ValueError):
    """A confirmed approval no longer addresses its original assignment or input."""


class QuestionCommands:
    """The sole owner of Question post/answer/cancel decisions.

    Trusted routes and short FastMCP controls authorize their caller first. The
    shared publication path turns confirmed outcomes into owner/subscriber mail;
    these commands neither acknowledge delivery nor start model execution.
    """

    def __init__(self, history: TaskHistory) -> None:
        self.history = history
        self.on_change: Callable[[TaskSnapshot], None] | None = None
        self.before_post: Callable[[TaskSnapshot, QuestionDefinition], None] | None = None

    def create(
        self,
        owner: TaskOwner,
        definition: QuestionDefinition,
        *,
        invocation_id: str,
        handle: str | None = None,
    ) -> TaskSnapshot:
        """Confirm one Question identity and post it through the sole Question owner."""
        definition = self._normalize(definition)
        self.validate_definition(definition)
        if definition.expires_at is not None:
            self._deadline(definition)
        with self.history.serialized():
            identity, stamp = (
                handle
                or "task_"
                + hashlib.sha256(
                    (owner.session_id + "\0question\0" + invocation_id).encode()
                ).hexdigest(),
                _now(),
            )
            previous = self.history.task_snapshot(identity)
            if previous is not None:
                if (
                    previous.owner == owner
                    and previous.invocation_id == invocation_id
                    and previous.details is None
                    and not previous.raw_status
                ):
                    return self.post(previous, definition)
                if (
                    previous.owner != owner
                    or previous.invocation_id != invocation_id
                    or previous.details is None
                    or (
                        QuestionDefinition.model_validate(
                            self.history.read_content(previous.submission)
                        )
                        if previous.submission is not None
                        else self._definition(previous)
                    )
                    != definition
                ):
                    raise ValueError(
                        "Question invocation identity was reused with different content"
                    )
                return previous if previous.raw_status else self.post(previous, definition)
            surface = str(definition.metadata.get("a2ui_surface_id") or "")
            resource = question_surface_resource(owner.session_id, surface) if surface else None
            if resource is not None and any(
                (task := self.history.task_snapshot(candidate)) is not None
                and task.terminal_at is None
                for candidate in self.history.resource_handles(resource)
            ):
                raise ValueError("This surface already belongs to an open Question")
            content = self.history.write_content(
                owner.session_id, definition.model_dump(mode="json")
            )
            accepted = TaskSnapshot(
                handle=identity,
                kind="Question",
                label=definition.prompt[:100],
                description=definition.prompt,
                description_missing=False,
                owner=owner,
                invocation_id=invocation_id,
                tool=definition.source,
                backend=BackendReference(owner="question", server="clio", task_id=identity),
                created_at=stamp,
                updated_at=stamp,
                details=content,
                submission=content,
                resource_id=resource,
            )
            self.history.record_snapshot(accepted, observation_id=uuid4().hex)
            return self.post(accepted, definition)

    def visual_question(
        self, session_id: str, surface_id: str, action_name: str
    ) -> TaskSnapshot | None:
        """Preserve answer identity even after closure; reused surfaces require an exact ID."""
        with self.history.serialized():
            candidates = []
            for handle in self.history.resource_handles(
                question_surface_resource(session_id, surface_id)
            ):
                current = self.history.task_snapshot(handle)
                if current is None or current.kind != "Question":
                    continue
                definition = self._definition(current)
                if definition.metadata.get("a2ui_answer_action", "question.submit") == action_name:
                    candidates.append(current)
            if len(candidates) > 1:
                raise ValueError("Visual Question identity is ambiguous")
            return candidates[0] if candidates else None

    @staticmethod
    def validate_definition(definition: QuestionDefinition) -> None:
        """Reject unanswerable content before a submission header is accepted."""
        if not definition.prompt.strip():
            raise ValueError("Question prompt cannot be empty")
        if definition.source == "ask_user" and re.search(
            r"\[\[\s*##\s*[A-Za-z_][\w-]*\s*##", definition.prompt
        ):
            raise ValueError("Question contains an internal parser marker; submit a clean question")
        if definition.requested_schema is not None:
            Draft202012Validator.check_schema(definition.requested_schema)
        if definition.kind != "freeform" and not definition.options:
            raise ValueError("Choice and confirmation Questions require offered options")
        if (
            definition.kind == "freeform"
            and not definition.allow_freeform
            and not definition.metadata.get("question_bundle")
        ):
            raise ValueError("Freeform Questions must allow a freeform answer")
        if len({option.value for option in definition.options}) != len(definition.options):
            raise ValueError("Question option values must be unique")

    def restore(self, session_id: str | None = None) -> None:
        """Finish posting confirmed Question headers after an interrupted owner write.

        With ``session_id``, only that conversation's open Questions (obligations lane).
        """
        with self.history.serialized():
            for handle in self.history.task_handles(
                kind="Question", session_id=session_id, active_only=True
            ):
                current = self.history.task_snapshot(handle)
                if current is not None and not current.raw_status and current.details is not None:
                    try:
                        self.post(current, self._definition(current))
                    except ObsoleteQuestionGate:
                        self._close(current, "cancelled", reason="obsolete_gate")

    @staticmethod
    def _normalize(definition: QuestionDefinition) -> QuestionDefinition:
        if definition.source == "ask_user":
            definition = definition.model_copy(update={"prompt": definition.prompt.strip()})
        if definition.kind == "confirmation" and not definition.options:
            return definition.model_copy(
                update={
                    "options": (
                        QuestionOption(label="Yes", value="yes"),
                        QuestionOption(label="No", value="no"),
                    )
                }
            )
        return definition

    def post(self, accepted: TaskSnapshot, definition: QuestionDefinition) -> TaskSnapshot:
        """Post content under the already persisted public handle and return immediately."""
        with self.history.serialized():
            current = self._question(accepted.handle)
            if (
                current.model_dump(exclude={"description_reference"})
                != accepted.model_dump(exclude={"description_reference"})
                or current.raw_status
            ):
                raise ValueError("Question was already posted or its acceptance changed")
            definition = self._normalize(definition)
            self.validate_definition(definition)
            if definition.expires_at is not None:
                self._deadline(definition)
            if self.before_post is not None:
                self.before_post(current, definition)
            details = self.history.write_content(
                current.owner.session_id, definition.model_dump(mode="json")
            )
            posted = current.model_copy(
                update={
                    "raw_status": "pending",
                    "status": "input_required",
                    "activity": "waiting_for_input",
                    "details": details,
                    "submission": current.submission or details,
                    "updated_at": _now(),
                    "supported_actions": ("observe", "wait", "cancel", "answer"),
                }
            )
            self.history.record_snapshot(posted, observation_id=uuid4().hex)
            if self.on_change is not None:
                self.on_change(posted)
            return posted

    def annotate(self, handle: str, metadata: dict[str, Any]) -> TaskSnapshot:
        """Record trusted routing facts without replacing a Question's input contract."""
        with self.history.serialized():
            current = self._question(handle)
            if current.raw_status != "pending":
                return current
            definition = self._definition(current)
            for key in ("elicitation_helper", "elicitation_helper_context"):
                if (
                    key in definition.metadata
                    and key in metadata
                    and definition.metadata[key] != metadata[key]
                ):
                    raise ValueError("An elicitation helper binding cannot change")
            updated = QuestionDefinition.model_validate(
                {
                    **definition.model_dump(mode="json"),
                    "metadata": {**definition.metadata, **metadata},
                }
            )
            reference = self.history.write_content(
                current.owner.session_id, updated.model_dump(mode="json")
            )
            observed = current.model_copy(update={"details": reference, "updated_at": _now()})
            self.history.record_snapshot(observed, observation_id=uuid4().hex)
            if self.on_change is not None:
                self.on_change(observed)
            return observed

    def resolution(self, handle: str) -> dict[str, Any] | None:
        """Read immutable server decision data without replacing a Question's answer."""
        with self.history.serialized():
            task = self._question(handle)
            reference = self._definition(task).resolution
            if reference is None:
                return None
            value = self.history.read_content(reference)
            if (
                not isinstance(value, dict)
                or value.get("question_handle") != handle
                or value.get("answer_reference")
                != (task.result.model_dump(mode="json") if task.result else None)
                or value.get("raw_status") != task.raw_status
                or value.get("terminal_at") != task.terminal_at
            ):
                raise ValueError("Question resolution differs from its accepted decision")
            return value

    def freeze_resolution(self, handle: str, value: dict[str, Any]) -> dict[str, Any]:
        """Confirm content before attaching one decision reference to the original Question."""
        with self.history.serialized():
            task = self._question(handle)
            value = {
                **value,
                "question_handle": handle,
                "answer_reference": task.result.model_dump(mode="json") if task.result else None,
                "raw_status": task.raw_status,
                "terminal_at": task.terminal_at,
            }
            existing = self.resolution(handle)
            if existing is not None:
                if existing != value:
                    raise ValueError("A frozen Question decision cannot change")
                return existing
            if task.terminal_at is None:
                raise ValueError("Question decision remains pending")
            definition = self._definition(task)
            content = self.history.write_content(task.owner.session_id, value)
            details = self.history.write_content(
                task.owner.session_id,
                definition.model_copy(update={"resolution": content}).model_dump(mode="json"),
            )
            self.history.record_snapshot(
                task.model_copy(update={"details": details}), observation_id=uuid4().hex
            )
            return value

    def answer(self, handle: str, reply: QuestionReply) -> TaskSnapshot:
        """Confirm one validated answer; a concurrent answer/cancel cannot overwrite it."""
        with self.history.serialized():
            current = self._question(handle)
            definition = self._definition(current)
            if definition.metadata.get("question_bundle") and reply.action == "accept":
                from clio_agent.gact.question_bundle import validate_question_answer

                reply = reply.model_copy(
                    update={
                        "answer": validate_question_answer(
                            definition, reply.answer, dict(reply.metadata)
                        )
                    }
                )
            if current.raw_status != "pending":
                if (
                    current.raw_status == "answered"
                    and current.result is not None
                    and QuestionReply.model_validate(self.history.read_content(current.result))
                    == reply
                ):
                    publish_task_notice(self.history, current)
                    return current
                raise ValueError("Question is no longer awaiting an answer")
            definition = self._definition(current)
            deadline = self._deadline(definition)
            if deadline is not None and datetime.now(timezone.utc) >= deadline:
                self._close(current, "expired")
                raise ValueError("Question's explicit answer deadline expired")
            values = {option.value for option in definition.options}
            if len(set(reply.selected_options)) != len(reply.selected_options):
                raise ValueError("An option cannot be selected twice")
            if any(value not in values for value in reply.selected_options):
                raise ValueError("Answer names an option that was not offered")
            if definition.kind != "multi_choice" and len(reply.selected_options) > 1:
                raise ValueError("Question accepts only one selected option")
            if (
                reply.action == "accept"
                and not definition.allow_freeform
                and not definition.metadata.get("question_bundle")
                and not reply.selected_options
            ):
                raise ValueError("Question requires an offered option")
            elicitation = definition.metadata.get("elicitation")
            empty_protocol_answer = definition.source == "mcp_elicitation" and (
                definition.requested_schema is not None
                or isinstance(elicitation, dict)
                and elicitation.get("mode") == "url"
            )
            if (
                not reply.answer
                and not reply.selected_options
                and not reply.parts
                and not reply.metadata
                and reply.action == "accept"
                and not empty_protocol_answer
            ):
                raise ValueError("Question answer is empty")
            if definition.requested_schema is not None and reply.action == "accept":
                try:
                    Draft202012Validator(definition.requested_schema, registry=Registry()).validate(
                        reply.metadata
                    )
                except SchemaValidationError as error:
                    raise ValueError(
                        "The answer does not match the requested form schema"
                    ) from error
            result = self.history.write_content(
                current.owner.session_id, reply.model_dump(mode="json")
            )
            answered = current.model_copy(
                update={
                    "raw_status": "answered",
                    "status": "completed",
                    "activity": "",
                    "result": result,
                    "updated_at": _now(),
                    "terminal_at": _now(),
                    "settlement": "confirmed",
                    "supported_actions": ("get_result",),
                }
            )
            self.history.record_snapshot(answered, observation_id=uuid4().hex)
            if self.on_change is not None:
                self.on_change(answered)
            publish_task_notice(self.history, answered)
            return answered

    def cancel(self, handle: str, *, caller: TaskCaller | None = None) -> TaskSnapshot:
        """Dismiss an open Question; historical terminal outcomes stay unchanged."""
        with self.history.serialized():
            if caller is not None:
                TaskAccess(self.history, caller).require(handle, "control")
            current = self._question(handle)
            if current.raw_status in {"answered", "cancelled", "expired"}:
                publish_task_notice(self.history, current)
                return current
            return self._close(current, "cancelled")

    def expire(self, handle: str) -> TaskSnapshot:
        """Arbitrate an explicit Question deadline against answers under the same writer lock."""
        with self.history.serialized():
            current = self._question(handle)
            if current.raw_status != "pending":
                return current
            deadline = self._deadline(self._definition(current))
            if deadline is not None and datetime.now(timezone.utc) >= deadline:
                return self._close(current, "expired")
            return current

    def _close(
        self, current: TaskSnapshot, status: str, *, reason: str | None = None
    ) -> TaskSnapshot:
        stamp = _now()
        result = self.history.write_content(
            current.owner.session_id, {"status": status, **({"reason": reason} if reason else {})}
        )
        closed = current.model_copy(
            update={
                "raw_status": status,
                "status": "cancelled" if status == "cancelled" else "failed",
                "activity": "",
                "updated_at": stamp,
                "terminal_at": stamp,
                "settlement": "confirmed",
                "result": result,
                "supported_actions": ("get_result",),
            }
        )
        self.history.record_snapshot(closed, observation_id=uuid4().hex)
        if self.on_change is not None:
            self.on_change(closed)
        publish_task_notice(self.history, closed)
        return closed

    def _question(self, handle: str) -> TaskSnapshot:
        current = self.history.task_snapshot(handle)
        if current is None or current.kind != "Question":
            raise ValueError("Question handle is unavailable")
        return current

    def _definition(self, current: TaskSnapshot) -> QuestionDefinition:
        if current.details is None:
            raise ValueError("Question content has not been confirmed")
        return QuestionDefinition.model_validate(self.history.read_content(current.details))

    @staticmethod
    def _deadline(definition: QuestionDefinition) -> datetime | None:
        if definition.expires_at is None:
            return None
        deadline = datetime.fromisoformat(definition.expires_at)
        if deadline.tzinfo is None:
            raise ValueError("Question deadline requires a timezone")
        return deadline
