"""A woken turn of an agent's own flow continues its assignment's run (durable state only)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from clio_agent.flows.assignment_run import AssignmentRun, assignment_run

ROOT = "task_root"
GENERIC = "Continue the retained assignment using the queued messages and task outcomes."


class _History:
    def __init__(
        self,
        mail: dict[str, Any],
        contents: dict[str, Any],
        *,
        checkpoint: Any = None,
        invocation_id: str = "user_1",
        details: str | None = None,
    ) -> None:
        self.mail, self.contents, self.checkpoint = mail, contents, checkpoint
        self.assignment = SimpleNamespace(invocation_id=invocation_id, details=details)

    def mail_item(self, session_id: str, identity: str) -> Any:
        return self.mail.get(identity)

    def workflow_checkpoint(self, root: str, invocation: str | None = None) -> Any:
        return self.checkpoint if root == ROOT else None

    def read_content(self, reference: str) -> Any:
        return self.contents[reference]

    def task_snapshot(self, handle: str) -> Any:
        return self.assignment if handle == ROOT else None


def _mail(identity: str, kind: str) -> Any:
    return SimpleNamespace(id=identity, kind=kind, content="c:" + identity)


def _run(history: _History, trigger: str, flow: str = "fanout") -> AssignmentRun | None:
    return assignment_run(history, flow, ROOT, "sess_1", trigger)


def test_a_written_request_starts_its_own_run() -> None:
    history = _History({"user_2": _mail("user_2", "human")}, {})
    assert _run(history, "user_2") is None


def test_react_keeps_the_admitted_question() -> None:
    history = _History({"recovery:x": _mail("recovery:x", "control")}, {})
    assert _run(history, "recovery:x", flow="react") is None


def test_a_restart_wake_continues_the_checkpointed_run_on_its_request() -> None:
    checkpoint = SimpleNamespace(invocation_id="flow:fanout:user_1", content="c:checkpoint")
    history = _History(
        {"recovery:x": _mail("recovery:x", "control")},
        {"c:checkpoint": {"request": "4 survey i1-0", "status": "running"}},
        checkpoint=checkpoint,
    )
    assert _run(history, "recovery:x") == AssignmentRun("flow:fanout:user_1", "4 survey i1-0")


def test_a_childs_result_without_a_checkpoint_reruns_the_opening_request() -> None:
    other = SimpleNamespace(invocation_id="flow:other:user_1", content="c:other")
    history = _History(
        {
            "task_result:a": _mail("task_result:a", "task_result"),
            "user_1": _mail("user_1", "human"),
        },
        {"c:user_1": {"text": "4 survey i1-0", "message_id": "user_1"}},
        checkpoint=other,
    )
    assert _run(history, "task_result:a") == AssignmentRun("flow:fanout:user_1", "4 survey i1-0")
    assert GENERIC not in str(_run(history, "task_result:a"))


def test_a_delegated_assignment_runs_on_the_delegated_text() -> None:
    delegation = "assignment:" + ROOT
    history = _History(
        {delegation: _mail(delegation, "delegation")},
        {"c:" + delegation: {"assignment": "part 0: survey", "parent_handle": "task_parent"}},
        invocation_id="flowrun_x:agent:site:digest:0",
    )
    assert _run(history, delegation) == AssignmentRun("flow:fanout:" + delegation, "part 0: survey")


def test_a_wake_after_the_run_ended_does_not_run_the_flow_again() -> None:
    history = _History(
        {"task_result:late": _mail("task_result:late", "task_result")},
        {"c:details": {"predecessor": "task_previous"}},
        invocation_id="task_result:late",
        details="c:details",
    )
    assert _run(history, "task_result:late") == AssignmentRun(ended="task_previous")
