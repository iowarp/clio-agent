"""After Stop, an outcome of the stopped work waits for the person; it does not restart it."""

from __future__ import annotations

from types import SimpleNamespace

from clio_agent.gact.session_input import admission_trigger


def _item(kind: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, id=name)


STOPPED = SimpleNamespace(terminal_at="2026-11-04T15:21:08Z", cancellation_requested_at="t")
FINISHED = SimpleNamespace(terminal_at="2026-11-04T15:21:08Z", cancellation_requested_at=None)
RUNNING = SimpleNamespace(terminal_at=None, cancellation_requested_at=None)


def test_a_stopped_assignments_outcome_does_not_start_a_turn() -> None:
    """Live: Stop during a shell command, the command's outcome arrived, and the agent ran
    again on a synthesized "continue" prompt, then once more at the next boot.

    SABOTAGE: return ``items[0]`` whatever ``latest`` is, and this outcome starts a turn.
    """
    outcome = _item("task_result", "task_result:shell")
    assert admission_trigger("sess_1", (outcome,), STOPPED) is None


def test_the_persons_next_message_is_the_trigger_and_the_outcome_comes_along() -> None:
    outcome, message = _item("task_result", "task_result:shell"), _item("human", "user_2")
    assert admission_trigger("sess_1", (outcome, message), STOPPED) is message


def test_outcomes_of_work_that_was_not_stopped_still_wake_the_agent() -> None:
    """A background task the agent waits on finishing after its turn ended is that turn's
    continuation, and so is one finishing during a running assignment."""
    outcome = _item("task_result", "task_result:late")
    assert admission_trigger("sess_1", (outcome,), FINISHED) is outcome
    assert admission_trigger("sess_1", (outcome,), RUNNING) is outcome
    assert admission_trigger("sess_1", (outcome,), None) is outcome
    assert admission_trigger("sess_1", (), STOPPED) is None
