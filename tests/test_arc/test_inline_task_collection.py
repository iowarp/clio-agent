"""A foreground shell result reaches the model once: its task notice is collected.

The shell's finished task comes back inline but as MCP text, so it was not a
``CollectedTaskResult`` by the time the step recorded it; the outcome notice stayed
pending and was given to the model again as a ``task_result`` input on the next step
(427 of 453 shell calls in the stored traces, 2026-11-03). The boundary now notes the
collected value for the calling model step, and the step collects its notice.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import types
from typing import Any

import dspy
import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context as ctx
from clio_agent.gact.agents import clio_react_record
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.builtin_boundary import BuiltinBoundary
from clio_agent.gact.tool_observation import (
    INLINE_COLLECTION,
    CollectedTaskResult,
    CollectedText,
)
from tests._scripted_engine import calls, scripted_lm

from .conftest import live_plane_context

SID, SCOPE = "inline-collection-s1", "agentA"
NOTICE = f"outcome:task_shell:{SID}"
REPLY = {"handle": "task_shell", "status": "completed", "pending": False, "result": {"x": 1}}


def _carried() -> CollectedTaskResult:
    return CollectedTaskResult(REPLY, (NOTICE,), ("task_shell",))


def _shell(command: str) -> str:
    """Like the shell path: the boundary notes the collected value, the model gets text."""
    from clio_agent.gact.tool_observation import note_inline_collection

    note_inline_collection(_carried())
    return json.dumps(REPLY)


def _plain(q: str) -> str:
    """A tool whose reply carries no task."""
    return "PLAIN"


def _failing(command: str) -> str:
    """Notes a carried task, then fails: an error observation collects nothing."""
    from clio_agent.gact.tool_observation import note_inline_collection

    note_inline_collection(_carried())
    raise RuntimeError("boom")


def _run(arc: ARCMemory, tool: Any, monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    committed: list[tuple[Any, ...]] = []

    def commit(self: Any, identity: str, content: Any, notes: tuple[str, ...], call_id: str):
        committed.append((notes, call_id, content.get("text")))

    monkeypatch.setattr(clio_react_record.StepRecorder, "_commit_collection", commit)
    lm, _ = scripted_lm(
        [
            calls((tool.name, {"command": "ls"} if tool.name != "plain" else {"q": "x"})),
            calls(("submit", {"answer": "FINAL"})),
        ]
    )
    agent = ClioReAct("question -> answer", tools=[tool], max_iters=4)
    app = types.SimpleNamespace(
        state=types.SimpleNamespace(arc=arc, bus=types.SimpleNamespace(publish=lambda e: None))
    )
    tokens = (ctx.set_session_id(SID), ctx.set_app(app))
    try:
        with live_plane_context(arc, session=SID, scope=SCOPE), dspy.context(lm=lm):
            agent(question="list the folder")
    finally:
        for token in reversed(tokens):
            ctx.reset(token)
    return committed


def test_a_foreground_shell_reply_collects_its_notice_and_keeps_its_text(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: drop ``collected_observation`` in ``ClioReAct._call`` (or the
    ``COLLECTED`` check in ``step_done``) and no collection is committed: the notice
    stays pending and is given to the model again."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    committed = _run(arc, dspy.Tool(_shell, name="shell_bash"), monkeypatch)
    assert committed == [((NOTICE,), "call_0_0", json.dumps(REPLY))]
    observation = next(s for s in arc.render_segments(SID, SCOPE) if s.kind == "observation")
    assert observation.content["notification_ids"] == [NOTICE]
    assert observation.content["text"] == json.dumps(REPLY)  # the model's text is unchanged


def test_replies_without_a_carried_task_and_errors_collect_nothing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: drop the ``if not outcome.is_error`` guard in ``ClioReAct._call`` and
    the failed call collects a notice the model never saw."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    assert _run(arc, dspy.Tool(_plain, name="plain"), monkeypatch) == []
    arc2 = ARCMemory(data_dir=str(tmp_path / "arc2"))
    assert _run(arc2, dspy.Tool(_failing, name="shell_bash"), monkeypatch) == []


def test_the_boundary_notes_a_collected_foreground_reply_for_the_calling_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``collect_inline`` returns the collected value and notes it in the caller's slot,
    which crosses the copied contexts of the application loop by reference.

    SABOTAGE: drop the ``captured.run(note_inline_collection, ...)`` line and the step
    has nothing to collect.
    """
    from clio_agent.gact import task_declarations

    monkeypatch.setattr(task_declarations, "_collected", lambda _app, _caller, v: _carried())
    history = types.SimpleNamespace(io=types.SimpleNamespace(run=_run_inline))
    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            task_history=history,
            conversations=types.SimpleNamespace(caller=lambda sid: ("caller", sid)),
        )
    )
    boundary = BuiltinBoundary.__new__(BuiltinBoundary)
    boundary.app = app
    carried: list[CollectedTaskResult] = []
    token = INLINE_COLLECTION.set(carried)
    try:
        captured = contextvars.copy_context()  # as product_call captures it
    finally:
        INLINE_COLLECTION.reset(token)
    value = asyncio.run(boundary.collect_inline(SID, dict(REPLY), captured))
    assert isinstance(value, CollectedTaskResult) and value.notification_ids == (NOTICE,)
    assert [c.notification_ids for c in carried] == [(NOTICE,)]
    # Outside a model call (a script's nested call) there is no slot: nothing is noted.
    asyncio.run(boundary.collect_inline(SID, dict(REPLY), contextvars.copy_context()))
    assert len(carried) == 1
    assert isinstance(CollectedText("t", carried), str)


async def _run_inline(function: Any, *args: Any) -> Any:
    return function(*args)
