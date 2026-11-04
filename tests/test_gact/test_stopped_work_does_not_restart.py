"""After Stop, an outcome of the stopped work waits for the person; it does not restart it."""

from __future__ import annotations

from types import SimpleNamespace

from clio_agent.gact.session_input import admission_trigger


def _item(kind: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, id=name)


def _assignment(terminal: bool, activity: str = "", cancelled: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        terminal_at="2026-11-04T15:21:08Z" if terminal else None,
        cancellation_requested_at="2026-11-04T15:21:07Z" if cancelled else None,
        activity=activity,
    )


#: Stop leaves the assignment open (the live case); a cancellation ends it.
STOPPED = _assignment(False, "stopped")
STOPPING = _assignment(False, "stopping")
CANCELLED = _assignment(True, cancelled=True)
FINISHED = _assignment(True)
RUNNING = _assignment(False, "busy")
YIELDED = _assignment(False, "yielded")


def test_a_stopped_assignments_outcome_does_not_start_a_turn() -> None:
    """Live: Stop during a shell command, the command's outcome arrived, and the agent ran
    again on a synthesized "continue" prompt, then once more at the next boot.

    SABOTAGE: return ``items[0]`` whatever ``latest`` is, and this outcome starts a turn.
    """
    outcome = _item("task_result", "task_result:shell")
    for stopped in (STOPPED, STOPPING, CANCELLED):
        assert admission_trigger("sess_1", (outcome,), stopped) is None


def test_the_persons_next_message_is_the_trigger_and_the_outcome_comes_along() -> None:
    outcome, message = _item("task_result", "task_result:shell"), _item("human", "user_2")
    assert admission_trigger("sess_1", (outcome, message), STOPPED) is message
    resume = _item("control", "resume:task_1:stop:turn_1")  # the person's Resume
    assert admission_trigger("sess_1", (outcome, resume), STOPPED) is resume


def test_outcomes_of_work_that_was_not_stopped_still_wake_the_agent() -> None:
    """A background task the agent waits on finishing after its turn ended is that turn's
    continuation, and so is one finishing during a running assignment."""
    outcome = _item("task_result", "task_result:late")
    assert admission_trigger("sess_1", (outcome,), FINISHED) is outcome
    assert admission_trigger("sess_1", (outcome,), RUNNING) is outcome
    assert admission_trigger("sess_1", (outcome,), YIELDED) is outcome  # after Resume
    assert admission_trigger("sess_1", (outcome,), None) is outcome
    assert admission_trigger("sess_1", (), STOPPED) is None
