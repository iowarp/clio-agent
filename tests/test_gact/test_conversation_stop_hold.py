"""After "Stop everything", nothing but the person's message starts a turn.

Found live (FQ12): each "Stop everything" cancelled the running shell command, and the
cancellation's notices (the task's outcome and the person's intervention) woke the agent,
which started a new turn and a new command.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from clio_agent.arc.task_types import CoreReference, MailItem
from clio_agent.gact import conversation_stop_hold as hold


class _Sessions:
    def __init__(self) -> None:
        self.metadata: dict[str, Any] = {}

    def get(self, _sid: str) -> Any:
        return SimpleNamespace(metadata=self.metadata)

    def update(self, _sid: str, *, metadata_patch: dict[str, Any]) -> None:
        self.metadata.update(metadata_patch)


class _History:
    def __init__(self, pending: list[MailItem]) -> None:
        self.pending = pending

    def pending_mail(self, _sid: str) -> list[MailItem]:
        return self.pending


def _mail(kind: str, ident: str) -> MailItem:
    return MailItem(
        id=ident,
        recipient_session="sess_1",
        kind=kind,  # type: ignore[arg-type]
        content=CoreReference(
            session_id="sess_1", scope="sess_1", content_id="c_" + ident, sha256="0" * 64
        ),
        created_at="2026-11-16T03:00:00+00:00",
    )


def test_a_stopped_conversation_is_not_woken_by_what_the_stop_cancelled() -> None:
    sessions = _Sessions()
    outcome, control = _mail("task_result", "outcome"), _mail("control", "intervention")
    history = _History([outcome, control])
    hold.stop(sessions, "sess_1")
    assert hold.holds(sessions, history, "sess_1", outcome)
    assert hold.holds(sessions, history, "sess_1", control)


def test_the_persons_message_carries_what_waited_and_ends_the_stop() -> None:
    sessions = _Sessions()
    outcome, message = _mail("task_result", "outcome"), _mail("human", "message")
    history = _History([outcome, message])
    hold.stop(sessions, "sess_1")
    assert not hold.holds(sessions, history, "sess_1", outcome)  # goes with the message
    assert not hold.holds(sessions, history, "sess_1", message)
    assert not sessions.metadata[hold.STOPPED_KEY]
    history.pending = [_mail("task_result", "later")]
    assert not hold.holds(sessions, history, "sess_1", history.pending[0])


def test_a_conversation_that_was_not_stopped_is_unchanged() -> None:
    sessions = _Sessions()
    item = _mail("task_result", "outcome")
    assert not hold.holds(sessions, _History([item]), "sess_1", item)
