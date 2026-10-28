"""One durable owner/subscriber notice path, separate from execution and admission."""

from __future__ import annotations

from typing import Any

from clio_agent.arc.task_types import (
    CoreReference,
    MailItem,
    MailKind,
    QuestionDefinition,
    QuestionReply,
    TaskSnapshot,
    TaskSubscription,
)
from clio_agent.gact.native_work import now
from clio_agent.gact.task_access import TaskAccess, TaskCaller
from clio_agent.gact.task_history import TaskHistory


def subscription_caller(history: TaskHistory, subscription: TaskSubscription) -> TaskCaller | None:
    """Use accepted recipient identity, with only a verified legacy assignment fallback."""
    human = subscription.recipient_human
    if human is None:
        assignment = history.latest_assignment(subscription.recipient_session)
        human = assignment.owner.human_id if assignment is not None else None
    return TaskCaller(subscription.recipient_session, human) if human else None


def task_owner_recipient(history: TaskHistory, current: TaskSnapshot) -> str | None:
    """Resolve the owning agent; a human-root completion never wakes itself."""
    if current.kind not in {"Session", "Subagent"}:
        return current.owner.session_id
    if current.kind == "Subagent" and current.owner.parent_handle is not None:
        parent = history.task_snapshot(current.owner.parent_handle)
        return parent.owner.session_id if parent is not None else None
    return None


def publish_task_notice(
    history: TaskHistory,
    current: TaskSnapshot,
    *,
    episode: str = "",
    attention_content: CoreReference | None = None,
) -> None:
    """Confirm an outcome/attention notification; reads and progress never acknowledge it."""
    _publish_task_notice(history, current, episode=episode, attention_content=attention_content)
    if current.terminal_at is not None and current.kind in {"Session", "Subagent"}:
        # Every recipient now holds the outcome: boot no longer needs to retry it.
        history.task_obligation_settled(current)


def settle_published_outcomes(history: TaskHistory) -> dict[str, int]:
    """After the one-time task-index build, finish every terminal outcome's publication.

    The build derives a ``t~`` open record for each terminal Session/Subagent outcome:
    a replay cannot see that its publication finished, because online the owner deletes
    the record then without writing an event. Left alone, the next start's startup row
    reads every one of them (one clio-core get each) and its obligations lane republishes
    them. This runs boot recovery's own action instead, :func:`publish_task_notice`, which
    skips notices already recorded, writes any still missing, and settles the record, so
    the open records match what the online path leaves.

    Returns:
        ``outcomes`` checked and ``open_tasks`` (``t~`` records) left afterwards.
    """
    from clio_agent.gact.task_index_writes import trim  # noqa: PLC0415 - import cycle

    checked = 0
    with history.index.deferred_closes():  # the settled records are deleted in batches
        for _name, row in history.index.open_records("t~"):
            task = history.task_snapshot(str(row.get("handle", "")))
            if (
                task is None
                or task.terminal_at is None
                or task.kind not in {"Session", "Subagent"}
                or task.settlement not in {"confirmed", "never_started"}
                or task.result is None
            ):
                continue
            publish_task_notice(history, task)
            checked += 1
            if checked % 64 == 0:
                trim(history)  # one tree at a time stays resident, not the whole history
    trim(history)
    return {"outcomes": checked, "open_tasks": len(history.index.open_records("t~"))}


def _publish_task_notice(
    history: TaskHistory,
    current: TaskSnapshot,
    *,
    episode: str,
    attention_content: CoreReference | None,
) -> None:
    with history.serialized():
        if current.terminal_at is not None:
            kind: MailKind = (
                "question_answer"
                if current.kind == "Question" and current.status == "completed"
                else "task_result"
            )
            identity, stamp = f"outcome:{current.handle}", current.terminal_at
            content = current.result
        elif episode:
            kind, identity, stamp = (
                "attention",
                f"attention:{current.handle}:{episode}",
                current.updated_at,
            )
            content = attention_content
        else:
            return
        recipients: dict[str, str] = {}
        owner_recipient = task_owner_recipient(history, current)
        if owner_recipient is not None:
            recipients[owner_recipient] = f"{identity}:{owner_recipient}"
        for subscription in history.subscriptions(current.handle):
            if not subscription.enabled:
                continue
            caller = subscription_caller(history, subscription)
            if caller is None:
                continue
            access = TaskAccess(history, caller)
            if {"read", "observe"} <= access.rights(current):
                if subscription.recipient_session != owner_recipient:
                    recipients[subscription.recipient_session] = (
                        f"{identity}:subscriber:{subscription.recipient_session}:{subscription.generation}"
                    )
        if not recipients:
            return
        recipients = {
            recipient: notice_id
            for recipient, notice_id in recipients.items()
            if history.mail_item(recipient, notice_id) is None
        }
        if not recipients:
            return
        if (
            current.terminal_at is not None
            and current.kind == "Question"
            and current.details is not None
        ):
            definition = QuestionDefinition.model_validate(history.read_content(current.details))
            if definition.source == "mcp_elicitation" and definition.requested_schema is not None:
                reply = (
                    QuestionReply.model_validate(history.read_content(current.result))
                    if current.raw_status == "answered" and current.result is not None
                    else None
                )
                message = (
                    "Answer recorded for the MCP form."
                    if reply is not None and reply.action == "accept"
                    else "The MCP form was declined."
                    if reply is not None and reply.action == "decline"
                    else "The MCP form was cancelled or expired."
                )
                content = history.write_content(
                    current.owner.session_id,
                    {
                        "handle": current.handle,
                        "status": current.status,
                        "message": message,
                        "answer_reference": current.result.model_dump(mode="json")
                        if current.result is not None
                        else None,
                    },
                )
        elif (
            current.terminal_at is not None
            and current.kind == "Subagent"
            and (current.resource_id or "").startswith("elicitation-helper:")
        ):
            content = history.write_content(
                current.owner.session_id,
                {
                    "handle": current.handle,
                    "status": current.status,
                    "message": f"MCP answer helper {current.status}.",
                    "result_reference": current.result.model_dump(mode="json")
                    if current.result is not None
                    else None,
                },
            )
        if content is None:
            content = history.write_content(
                current.owner.session_id,
                {
                    "handle": current.handle,
                    "status": current.status,
                    "settlement": current.settlement,
                    "episode": episode,
                    "message": "Task state is uncertain; inspect effects before deciding to retry."
                    if episode
                    else "Task settled.",
                    **({"details": current.progress.message} if episode else {}),
                },
            )
        for recipient, notice_id in recipients.items():
            outcome = (
                history.read_content(current.result)
                if current.kind == "Question" and current.result is not None
                else None
            )
            control_only = (
                isinstance(outcome, dict)
                and outcome.get("reason") == "obsolete_gate"
                or current.kind == "Question"
                and current.tool == "hook_defer"
                and recipient == owner_recipient
            )
            # Publication retries reuse the recipient's confirmed content/reference.
            if history.mail_item(recipient, notice_id) is not None:
                continue
            history.record_mail(
                MailItem(
                    id=notice_id,
                    recipient_session=recipient,
                    kind=kind,
                    content=content,
                    created_at=stamp,
                    handle=current.handle,
                    episode=episode or None,
                    delivery="held" if control_only else "eligible",
                    held_reason="control_only" if control_only else "",
                )
            )


def publish_control_outcome(
    history: TaskHistory,
    caller: TaskCaller,
    task: TaskSnapshot,
    action: str,
    outcome: dict[str, Any],
    identity: str,
) -> None:
    """Relay agent controls and deferred restart outcomes through Core mail.

    Human synchronous controls already have their attributed intervention notice.
    Restart additionally needs the later successor/failure outcome, which was not
    available in its initial acknowledgement.
    """
    if caller.human_action and action != "restart":
        return
    recipient = task_owner_recipient(history, task)
    if recipient is None or history.mail_item(recipient, identity) is not None:
        return
    content = history.write_content(
        recipient,
        {
            "action": action,
            "handle": task.handle,
            "outcome": outcome,
            "actor_human": caller.human_id if caller.human_action else None,
            "actor_session": None if caller.human_action else caller.session_id,
        },
    )
    history.record_mail(
        MailItem(
            id=identity,
            recipient_session=recipient,
            sender_human=caller.human_id if caller.human_action else None,
            sender_session=None if caller.human_action else caller.session_id,
            kind="control",
            content=content,
            handle=task.handle,
            created_at=now(),
        )
    )
