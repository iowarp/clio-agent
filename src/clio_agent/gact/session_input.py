"""Durable producer inputs; admission is exclusively the conversation runner's decision."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from clio_agent.arc.task_types import MailAdmission, MailItem, MailKind
from clio_agent.gact.native_work import now
from clio_agent.gact.task_history import TaskHistory


def capture_retry_input(
    history: TaskHistory,
    session_id: str,
    identity: str,
    *,
    stopping: bool,
) -> dict[str, Any] | None:
    """Capture exact input identity at the trusted human request boundary."""
    with history.serialized():
        source = history.mail_item(session_id, identity) if identity else None
        if source is None or source.kind not in {"human", "composer"}:
            return None
        admission = source.admission
        current_admission = (
            admission is not None
            and admission.input_revision == source.revision
            and admission.content == source.content
        )
        held = source.delivery == "held" and source.held_reason in {
            "hook_blocked",
            "execution_failed_before_input_commit",
            "stopped_before_input_commit",
        }
        if not held and not (stopping and source.delivery == "eligible" and current_admission):
            return None
        return {
            "id": source.id,
            "revision": source.revision,
            "content": source.content.model_dump(mode="json"),
            "admission": admission.model_dump(mode="json")
            if current_admission and admission
            else None,
        }


def apply_retry_supersession(history: TaskHistory, item: MailItem) -> None:
    """Retire the exact held input referenced by a trusted retry acceptance."""
    if item.kind not in {"human", "composer"}:
        return
    with history.serialized():
        value = history.read_content(item.content)
        binding = value.get("supersedes_input") if isinstance(value, dict) else None
        if not isinstance(binding, dict):
            return
        identity = binding.get("id")
        if not isinstance(identity, str) or identity == item.id:
            raise ValueError("Retry input binding is malformed")
        source = history.mail_item(item.recipient_session, identity)
        captured_admission = binding.get("admission")
        same_admission = False
        if source is not None and captured_admission is not None:
            accepted = MailAdmission.model_validate(captured_admission)
            current = source.admission
            same_admission = (
                current is not None
                and current.assignment_id == accepted.assignment_id
                and current.turn_id == accepted.turn_id
                and current.content == accepted.content == source.content
                and current.input_revision == source.revision
                and accepted.input_revision == binding.get("revision")
                and (
                    source.delivery == "eligible"
                    or source.delivery == "held"
                    and source.held_reason == "stopped_before_input_commit"
                )
            )
        if (
            source is not None
            and source.kind in {"human", "composer"}
            and (
                same_admission
                or source.delivery == "held"
                and source.held_reason
                in {
                    "hook_blocked",
                    "execution_failed_before_input_commit",
                    "stopped_before_input_commit",
                }
                and source.revision == binding.get("revision")
            )
            and source.content.model_dump(mode="json") == binding.get("content")
            and source.id not in history.acknowledged_notifications(item.recipient_session)
            and not history.mail_withdrawn(item.recipient_session, source.id)
        ):
            history.withdraw_mail(item.recipient_session, source.id)


def input_delivery(item: MailItem, *, held: bool, reason: str = "") -> MailItem:
    """Preserve exact admission through an owner-only Stop hold/release transition."""
    revision = item.revision + 1
    admission = item.admission
    if (
        admission is not None
        and admission.input_revision == item.revision
        and admission.content == item.content
    ):
        admission = admission.model_copy(update={"input_revision": revision})
    return item.model_copy(
        update={
            "delivery": "held" if held else "eligible",
            "held_reason": reason if held else "",
            "revision": revision,
            "admission": admission,
        }
    )


def withdraw_assignment_input(history: TaskHistory, session_id: str, handle: str) -> None:
    """Withdraw only exact, uncommitted admitted inputs of the cancelled assignment."""
    with history.serialized():
        for item in history.pending_mail(session_id):
            admission = item.admission
            if (item.kind in {"delegation", "control"} and item.handle == handle) or (
                admission is not None
                and admission.assignment_id == handle
                and admission.input_revision == item.revision
                and admission.content == item.content
            ):
                history.withdraw_mail(session_id, item.id)


def withdraw_message_inputs(app: Any, session_id: str, message_ids: list[str]) -> None:
    """Withdraw only pending human/composer inputs removed by an explicit human edit."""
    history = app.state.task_history
    with history.serialized():
        pending = {item.id: item for item in history.pending_mail(session_id)}
        for identity in message_ids:
            item = pending.get(identity)
            if item is not None and item.kind in {"human", "composer"}:
                history.withdraw_mail(session_id, identity)


def coalesce_pending_inputs(history: TaskHistory, session_id: str) -> None:
    """Withdraw older pending producer notices while retaining their full Core history."""
    with history.serialized():
        latest: set[str] = set()
        for item in reversed(history.pending_mail(session_id)):
            if item.kind != "hook":
                continue
            value = history.read_content(item.content)
            if not isinstance(value, dict) or value.get("source") != "hook":
                continue
            metadata = value.get("metadata")
            key = metadata.get("coalesce_key") if isinstance(metadata, dict) else None
            if not isinstance(key, str) or not key:
                continue
            if key in latest:
                history.withdraw_mail(session_id, item.id)
            else:
                latest.add(key)


def accept_session_input(
    app: Any,
    session_id: str,
    text: str,
    *,
    metadata: dict[str, Any] | None = None,
    steer_message_id: str = "",
    source: str = "hook",
    handle: str | None = None,
) -> str:
    """Confirm an attributed system injection and signal its exact Core mailbox."""
    history = app.state.task_history
    if app.state.sessions.get(session_id) is None:
        raise ValueError("Session input recipient is unavailable")
    metadata = dict(metadata or {})
    kind: MailKind = (
        "schedule"
        if metadata.get("scheduled")
        else "goal"
        if metadata.get("goal_id")
        else "loop"
        if metadata.get("loop_id")
        else "approval"
        if metadata.get("approval")
        else "a2ui"
        if source == "a2ui"
        else "spotter"
        if source == "spotter"
        else "hook"
    )
    identity = steer_message_id or "input_" + uuid4().hex
    with history.serialized():
        previous = history.mail_item(session_id, identity)
        value = {"text": text, "metadata": metadata, "source": source}
        if previous is not None:
            if (
                previous.kind != kind
                or previous.handle != handle
                or history.read_content(previous.content) != value
            ):
                raise ValueError("Session input identity was reused with different content")
            return previous.id
        content = history.write_content(session_id, value)
        history.record_mail(
            MailItem(
                id=identity,
                recipient_session=session_id,
                kind=kind,
                content=content,
                created_at=now(),
                handle=handle,
            )
        )
        coalesce_pending_inputs(history, session_id)
    return identity


#: Inputs that start work by themselves: a person's message, what the person set up, and
#: their Resume of a stopped assignment (a ``control`` input).
STARTING_KINDS = frozenset({"human", "composer", "goal", "loop", "schedule", "hook", "control"})


def stopped_by_person(latest: Any) -> bool:
    """Whether the person stopped the latest assignment: Stop leaves it open as
    ``stopping`` / ``stopped`` until they write or Resume; a cancellation ends it."""
    if latest is None:
        return False
    if latest.terminal_at is None:
        return latest.activity in {"stopping", "stopped"}
    return latest.cancellation_requested_at is not None


def admission_trigger(session_id: str, items: tuple[MailItem, ...], latest: Any) -> MailItem | None:
    """The staged input a new turn answers, or ``None`` when nothing may start one.

    After the person stopped the latest assignment, an outcome of the stopped work does
    not start the agent again: it stays eligible and reaches the agent with the person's
    next message or Resume, which is then the turn's trigger. Before this, a shell
    command's outcome arriving after Stop woke the agent with a synthesized "continue"
    prompt, and again at the next boot; the agent went on installing packages and
    starting a server the person had stopped.
    """
    if not stopped_by_person(latest):
        return items[0] if items else None
    trigger = next((item for item in items if item.kind in STARTING_KINDS), None)
    if trigger is None and items:
        from clio_agent.runtime import trace  # noqa: PLC0415

        trace.event(
            "CONVERSATION",
            "admission_waits reason=stopped_by_person session=%s outcomes=%d",
            session_id,
            len(items),
        )
    return trigger
