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


def test_notices_of_a_stop_do_not_start_a_turn() -> None:
    """RF trial run 2: after the person's Stop, the parent notices of the tasks cancelled
    with it (``control`` mail) and the stopped turn's children's outcomes started a new
    turn when the stopping turn retired.

    SABOTAGE: let every ``control`` input start work, and these notices start a turn.
    """
    notices = (
        _item("control", "intervention_0a1b:parent"),  # a task cancelled by the person
        _item("control", "intervention:message:user_3:parent"),
        _item("control", "agent-control:task_2"),  # an agent's control of a child
        _item("control", "recovery:task_1:input_1"),  # a boot recovery of the stopped turn
        _item("task_result", "task_result:child"),
    )
    for stopped in (STOPPED, STOPPING, CANCELLED):
        assert admission_trigger("sess_1", notices, stopped) is None
    restart = _item("control", "restart:task_9")  # the person's Restart
    assert admission_trigger("sess_1", (*notices, restart), STOPPED) is restart
    assert admission_trigger("sess_1", notices, FINISHED) is notices[0]


def test_a_stop_hooks_redrive_waits_for_the_person() -> None:
    """A Stop hook's "not done, continue" after the person's Stop is the agent's loop."""
    redrive, saved = _item("hook", "input_1"), _item("hook", "document-save:wc_1:3")
    content = {
        "input_1": {"metadata": {"stop_loop_redrive": True}},
        "document-save:wc_1:3": {"metadata": {"coalesce_key": "document-save:wc_1"}},
    }
    redrive.content, saved.content = "input_1", "document-save:wc_1:3"
    assert admission_trigger("sess_1", (redrive,), STOPPED, content.__getitem__) is None
    assert admission_trigger("sess_1", (redrive, saved), STOPPED, content.__getitem__) is saved
    assert admission_trigger("sess_1", (redrive,), FINISHED, content.__getitem__) is redrive
