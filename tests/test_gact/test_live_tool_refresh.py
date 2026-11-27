"""Tools a connected MCP server unlocks mid-turn reach the agent at its next step.

The executor side (re-listing over the persistent connection) is covered in
``tests/test_tools/test_mcp_tools_changed.py``; here a fake executor stands in for it
and the real :class:`ClioReAct` loop runs against a scripted engine (no network).
"""

from __future__ import annotations

import copy
from typing import Any

import dspy
import pytest

from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.agents.live_tool_refresh import (
    LiveToolRefresher,
    LiveToolScope,
    live_tool_refresher,
    refresh_executor_tools,
)
from clio_agent.gact.agents.person_servers import attach_person_servers, executor_person_servers
from tests._scripted_engine import calls, scripted_lm


class _ColabishExecutor:
    """Starts with ``nb_open``; calling it unlocks ``nb_cell`` on the next refresh."""

    def __init__(self) -> None:
        self.tools_revision = 0
        self.refreshes = 0
        self._pending = False
        self._defs: dict[str, Any] = {"nb_open": self._open, "other_tool": lambda: "x"}

    def _open(self) -> str:
        """Open the notebook connection."""
        self._pending = True
        return "true"

    @staticmethod
    def _cell(code: str) -> str:
        """Add a cell."""
        return f"ran {code}"

    def refresh_changed_namespaces(self) -> dict[str, Any]:
        self.refreshes += 1
        if not self._pending:
            return {}
        self._pending = False
        self._defs["nb_cell"] = self._cell
        self._defs["other_new"] = lambda: "not this agent's"
        self.tools_revision += 1
        return {"nb": (["nb_cell"], [])}

    def drop_cell(self) -> None:
        self._defs.pop("nb_cell")
        self.tools_revision += 1

    def to_dspy_tools(self) -> list[dspy.Tool]:
        return [dspy.Tool(func, name=name) for name, func in self._defs.items()]


def _identity(tools: list[Any]) -> list[Any]:
    return tools


def test_the_refresher_adds_and_removes_only_its_namespaces() -> None:
    executor = _ColabishExecutor()
    tools: dict[str, Any] = {t.name: t for t in executor.to_dspy_tools()}
    tools["submit"] = object()
    refresher = LiveToolRefresher(executor, {"nb"}, wrap=_identity)

    assert refresher(tools) is False  # nothing changed yet
    tools["nb_open"].func()
    assert refresher(tools) is True

    assert "nb_cell" in tools
    assert "other_new" not in tools  # a namespace this agent was not given whole
    assert list(tools)[-1] == "submit"  # the final-output tool stays last

    executor.drop_cell()
    assert refresher(tools) is True
    assert "nb_cell" not in tools
    assert "nb_open" in tools


def test_refresher_survives_dspy_deepcopy_without_cloning_the_executor() -> None:
    executor = _ColabishExecutor()
    refresher = LiveToolRefresher(executor, {"nb"}, wrap=_identity)

    clone = copy.deepcopy(refresher)

    assert clone is not refresher
    assert clone._executor is executor


def test_no_refresher_without_whole_namespaces() -> None:
    assert live_tool_refresher(LiveToolScope()) is None
    assert live_tool_refresher(LiveToolScope(executor=_ColabishExecutor())) is None
    assert live_tool_refresher(LiveToolScope(executor=object(), namespaces={"nb"})) is None
    assert isinstance(
        live_tool_refresher(LiveToolScope(executor=_ColabishExecutor(), namespaces={"nb"})),
        LiveToolRefresher,
    )


def test_a_failing_executor_refresh_is_typed_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        "clio_agent.runtime.trace.event", lambda _tag, fmt, *args: events.append(fmt % args)
    )

    class _Broken:
        def refresh_changed_namespaces(self) -> None:
            raise RuntimeError("loop gone")

    refresh_executor_tools(_Broken())
    refresh_executor_tools(object())  # an executor without the seam: nothing to do

    assert events and events[0].startswith("mcp_tools_refresh_failed")


def test_person_servers_are_taken_whole_and_a_when_asked_one_is_deferred() -> None:
    class _Spec:
        def __init__(self, source: str, always_load: bool) -> None:
            self.source, self.always_load = source, always_load

    class _Executor:
        _clio_namespace_specs = {
            "nb": _Spec("user", True),
            "notes": _Spec("workspace", False),
            "pack": _Spec("plugin:p", False),
        }

        def to_dspy_tools(self) -> list[dspy.Tool]:
            return [
                dspy.Tool(lambda: "true", name=name)
                for name in ("nb_open", "notes_add", "pack_run")
            ]

    picked: dict[str, Any] = {}
    requested: list[str] = []

    whole = attach_person_servers(_Executor(), picked, {}, requested)

    assert whole == {"nb", "notes"}
    assert set(picked) == {"nb_open", "notes_add"}
    rows = executor_person_servers(_Executor())
    assert {name: row["defer"] for name, row in rows.items()} == {"nb": None, "notes": True}


@pytest.mark.usefixtures("clio_core_plane")
def test_a_tool_unlocked_by_a_call_is_offered_and_callable_at_the_next_step() -> None:
    executor = _ColabishExecutor()
    lm, engine = scripted_lm(
        [
            calls(("nb_open", {})),
            calls(("nb_cell", {"code": "print(1)"})),
            calls(("submit", {"answer": "done"})),
        ]
    )
    with dspy.context(lm=lm):
        agent = ClioReAct("question -> answer", tools=executor.to_dspy_tools(), max_iters=6)
        object.__setattr__(
            agent, "_clio_tool_refresh", LiveToolRefresher(executor, {"nb"}, wrap=_identity)
        )
        pred = agent(question="connect and run")

    first, second = ({t.name for t in r.tools} for r in engine.requests[:2])
    assert "nb_cell" not in first
    assert {"nb_open", "nb_cell", "submit"} <= second
    assert "other_new" not in second
    assert pred.answer == "done"
    results = [
        part
        for message in engine.requests[2].messages
        for part in message.parts
        if type(part).__name__ == "ToolResultPart"
    ]
    assert any("ran print(1)" in getattr(p, "text", "") for r in results for p in r.content)
