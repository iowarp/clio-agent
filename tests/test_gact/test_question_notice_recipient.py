"""Who is told a Question was answered: the agent that asked, not a tool's own round."""

from __future__ import annotations

from clio_agent.arc.task_types import TaskOwner, TaskSnapshot
from clio_agent.gact.task_publication import task_owner_recipient


def _task(
    handle: str, kind: str, parent: str | None = None, status: str = "completed"
) -> TaskSnapshot:
    return TaskSnapshot.model_construct(
        handle=handle,
        kind=kind,
        status=status,
        owner=TaskOwner(
            human_id="h", session_id="sess_agent", assignment_id="a", parent_handle=parent
        ),
        invocation_id="inv",
    )


class _History:
    def __init__(self, *tasks: TaskSnapshot) -> None:
        self.tasks = {t.handle: t for t in tasks}

    def task_snapshot(self, handle: str) -> TaskSnapshot | None:
        return self.tasks.get(handle)


def test_an_agents_own_question_is_answered_to_the_agent() -> None:
    turn = _task("task_turn", "Session")
    question = _task("task_q", "Question", parent="task_turn")
    assert task_owner_recipient(_History(turn, question), question) == "sess_agent"


def test_a_tools_input_round_is_answered_to_the_tool_only() -> None:
    """A search source consent: the search call consumes the answer and goes on."""
    call = _task("task_search", "MCP")
    question = _task("task_q", "Question", parent="task_search")
    assert task_owner_recipient(_History(call, question), question) is None


def test_a_tools_cancelled_input_round_is_still_the_owners_outcome() -> None:
    """The call ended before the person answered: the owner hears the round closed."""
    call = _task("task_search", "MCP")
    question = _task("task_q", "Question", parent="task_search", status="cancelled")
    assert task_owner_recipient(_History(call, question), question) == "sess_agent"
