"""Human steering retains child acceptance and publishes attributed parent mail."""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import MailItem, MailKind, TaskOwner, TaskSnapshot
from clio_agent.gact.human_controls import relay_confirmed_human_input
from clio_agent.gact.session_mail import SessionMail
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_history import TaskHistory

STAMP = "2026-10-16T06:00:00Z"


def history_with_child(arc: ARCMemory) -> tuple[TaskHistory, TaskSnapshot]:
    """Record actual owner observations without starting model execution."""
    history = TaskHistory(arc)
    parent = TaskSnapshot(
        handle="parent_assignment",
        kind="Session",
        owner=TaskOwner(human_id="human", session_id="parent", assignment_id="parent_assignment"),
        label="Parent",
        invocation_id="parent_invocation",
        tool="conversation",
        status="active",
        created_at=STAMP,
        updated_at=STAMP,
    )
    child = TaskSnapshot(
        handle="child_assignment",
        kind="Subagent",
        owner=TaskOwner(
            human_id="human",
            session_id="child",
            assignment_id="child_assignment",
            parent_handle=parent.handle,
        ),
        label="Child",
        invocation_id="child_invocation",
        tool="conversation",
        status="active",
        activity="paused",
        created_at=STAMP,
        updated_at=STAMP,
    )
    history.record_snapshot(parent, observation_id="parent")
    history.record_snapshot(child, observation_id="child")
    return history, child


def input_mail(history: TaskHistory, *, kind: MailKind = "human") -> MailItem:
    """Confirm the input before asking the human-control owner to derive its notice."""
    return history.record_mail(
        MailItem(
            id="human_input",
            recipient_session="child",
            sender_human="human" if kind in {"human", "composer"} else None,
            kind=kind,
            created_at=STAMP,
            content=history.write_content(
                "child", {"assignment_id": "child_assignment", "text": "Keep the accepted work."}
            ),
        )
    )


@pytest.mark.parametrize("kind", ["human", "composer"])
def test_human_input_replay_notifies_parent_once_without_consuming_child_input(
    arc: ARCMemory, kind: MailKind
) -> None:
    history, child = history_with_child(arc)
    item = input_mail(history, kind=kind)
    relay_confirmed_human_input(history, item)
    relay_confirmed_human_input(history, item)
    assert history.pending_mail("child") == (item,)
    assert history.task_snapshot(child.handle) == child
    notices = history.pending_mail("parent")
    assert len(notices) == 1
    assert notices[0].kind == "control" and notices[0].sender_human == "human"
    assert notices[0].handle == child.handle
    assert history.read_content(notices[0].content) == {
        "id": "intervention:message:human_input",
        "handle": child.handle,
        "action": "message",
        "actor_human": "human",
        "requested_at": STAMP,
        "message": "Keep the accepted work.",
        "effect": {"accepted": True, "message_id": item.id},
    }


@pytest.mark.parametrize("kind", ["peer", "hook"])
def test_agent_or_system_input_cannot_create_human_intervention(
    arc: ARCMemory, kind: MailKind
) -> None:
    history, _ = history_with_child(arc)
    relay_confirmed_human_input(history, input_mail(history, kind=kind))
    assert history.pending_mail("parent") == ()


@pytest.mark.parametrize("kind", ["human", "composer"])
def test_parent_control_notice_distinguishes_targeted_steering_from_its_own_assignment(
    arc: ARCMemory, kind: MailKind
) -> None:
    """Keep direct child instructions actionable and their parent relay informational."""
    history, child = history_with_child(arc)
    item = input_mail(history, kind=kind)
    relay_confirmed_human_input(history, item)
    mailbox = SessionMail(history, lambda _sid, _item: True)

    parent_input = mailbox.stage(TaskCaller("parent", "human"))
    child_input = mailbox.stage(TaskCaller("child", "human"))
    assert len(parent_input.injections) == len(child_input.injections) == 1
    parent_source, parent_text = parent_input.injections[0]
    child_source, child_text = child_input.injections[0]
    assert parent_source == "control" and child_source == "steer"
    assert f"target {child.handle}" in parent_text
    assert "target session child; recipient session parent" in parent_text
    assert "Human human" in parent_text
    assert "Task-control notice" in parent_text
    assert "Quoted messages were addressed to that task" in parent_text
    assert "Do not execute their instructions in the receiving session" in parent_text
    assert parent_text.index("Task-control notice") < parent_text.index('"message":')
    assert '"message": "Keep the accepted work."' in parent_text
    assert "Keep the accepted work." in child_text and "Task-control notice" not in child_text
    assert history.pending_mail("child") == (item,)
    assert history.pending_mail("parent") == parent_input.items
    assert history.task_snapshot(child.handle) == child


def test_system_resume_control_remains_an_instruction_for_the_receiving_assignment(
    arc: ARCMemory,
) -> None:
    """The informational human relay must not relabel the engine's local resume input."""
    history, child = history_with_child(arc)
    resume = history.record_mail(
        MailItem(
            id="resume:child_assignment",
            recipient_session="child",
            kind="control",
            created_at=STAMP,
            handle=child.handle,
            content=history.write_content("child", "Resume this assignment."),
        )
    )
    staged = SessionMail(history, lambda _sid, _item: True).stage(TaskCaller("child", "human"))
    assert staged.items == (resume,)
    assert len(staged.injections) == 1
    assert "Resume this assignment." in staged.injections[0][1]
    assert "Task-control notice" not in staged.injections[0][1]


def test_replay_keeps_original_assignment_after_a_successor_exists(arc: ARCMemory) -> None:
    history, original = history_with_child(arc)
    item = input_mail(history)
    history.record_snapshot(
        original.model_copy(
            update={"status": "completed", "terminal_at": STAMP, "settlement": "confirmed"}
        ),
        observation_id="completed",
    )
    successor = original.model_copy(
        update={
            "handle": "successor",
            "owner": original.owner.model_copy(update={"assignment_id": "successor"}),
            "invocation_id": "successor_invocation",
        }
    )
    history.record_snapshot(successor, observation_id="successor")
    relay_confirmed_human_input(history, item)
    assert history.pending_mail("parent")[0].handle == original.handle
    assert history.task_snapshot(successor.handle) == successor


def test_parent_projection_failure_replays_confirmed_notice_without_duplicate_mail(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    history, _ = history_with_child(arc)
    item = input_mail(history)
    from clio_agent.gact import task_history_projection

    original = task_history_projection._project

    def fail_parent_projection(
        selected: TaskHistory, event: str, payload: dict[str, Any], **kwargs: Any
    ) -> None:
        if (
            getattr(selected, "history", selected) is history
            and event == "session.mail.accepted"
            and payload.get("recipient_session") == "parent"
        ):
            raise OSError("Parent notice projection unavailable")
        original(selected, event, payload, **kwargs)

    monkeypatch.setattr(task_history_projection, "_project", fail_parent_projection)
    with pytest.raises(OSError, match="Parent notice projection unavailable"):
        relay_confirmed_human_input(history, item)
    recovered = TaskHistory(arc)
    recovered.restore()
    restored = recovered.mail_item("child", item.id)
    assert restored is not None
    relay_confirmed_human_input(recovered, restored)
    assert len(recovered.pending_mail("parent")) == 1
    assert recovered.pending_mail("child") == (item,)
