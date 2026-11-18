"""A plugin the person adds during a turn joins that turn, the way they chose."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.agents import turn_rebuild


def _tool(name: str, desc: str = "") -> Any:
    return SimpleNamespace(
        name=name, desc=desc or f"{name} does a thing", args={"q": {"type": "string"}}
    )


class _Program:
    def __init__(self, tools: dict[str, Any], **attrs: Any) -> None:
        self.tools = tools
        self.signature = SimpleNamespace(instructions="base")
        for key, value in attrs.items():
            object.__setattr__(self, key, value)


class _Loop:
    """The parts of a running ClioReAct loop the join touches."""

    def __init__(self) -> None:
        self.session, self.scope, self.bare = "sess_a", "standard", False
        self.agent = _Program({"search_tools": _tool("search_tools")}, _clio_unlisted=frozenset())
        self.inputs: dict[str, Any] = {"system_prompt": "old prompt", "question": "q"}
        self.system = "old prompt"
        self.tools = self._listed_tools()
        self.autocompact = SimpleNamespace(
            summarize=None, compaction_id="", awaiting_measurement=False
        )
        self.recorder = object()

    def _listed_tools(self) -> list[Any]:
        unlisted = getattr(self.agent, "_clio_unlisted", frozenset())
        return [t for t in self.agent.tools.values() if t.name not in unlisted]


@pytest.fixture
def running(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A running loop; the rebuild returns a program with the plugin's native tool."""
    loop = _Loop()
    seen: dict[str, Any] = {"loop": loop, "settled": 0, "notes": []}
    rebuilt_tools = {
        "search_tools": _tool("search_tools"),
        "connected_data_search_ndp": _tool("connected_data_search_ndp", "Find NDP datasets."),
    }

    def rebuilt(_app: Any, _sid: str) -> Any:
        unlisted = frozenset(seen.get("unlisted", ()))
        program = _Program(
            dict(rebuilt_tools), _clio_unlisted=unlisted, _clio_code_only=frozenset()
        )
        return SimpleNamespace(program=program, system_prompt="new prompt"), None

    monkeypatch.setattr(turn_rebuild, "_rebuilt", rebuilt)
    monkeypatch.setattr("clio_agent.gact.agents.clio_react.active_loop", lambda: loop)
    monkeypatch.setattr(
        "clio_agent.gact.agents.prompt_basis.settle_basis",
        lambda *_a, **_k: seen.__setitem__("settled", seen["settled"] + 1),
    )
    monkeypatch.setattr(
        "clio_agent.gact.compaction.note_system_prompt", lambda *a: seen["notes"].append(a)
    )
    return seen


def test_now_lists_the_plugins_tools_and_rebuilds_the_prompt(running: dict[str, Any]) -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    turn = turn_rebuild.join_running_turn(app, "sess_a", "now")
    loop = running["loop"]
    assert turn["joined"] == ["connected_data_search_ndp"]
    assert turn["listed"] == ["connected_data_search_ndp"] and running["settled"] == 1
    assert "connected_data_search_ndp" in {t.name for t in loop.tools}
    assert loop.system == "new prompt" and loop.inputs["system_prompt"] == "new prompt"
    assert "tool list from your next step" in turn_rebuild.joined_message(turn)


def test_note_keeps_the_prompt_and_introduces_the_unlisted_tools(
    running: dict[str, Any],
) -> None:
    running["unlisted"] = {"connected_data_search_ndp"}
    app = SimpleNamespace(state=SimpleNamespace())
    turn = turn_rebuild.join_running_turn(app, "sess_a", "note")
    loop = running["loop"]
    assert running["settled"] == 0 and loop.system == "old prompt"
    assert "connected_data_search_ndp" in loop.agent.tools  # callable
    assert "connected_data_search_ndp" not in {t.name for t in loop.tools}  # not listed
    said = turn_rebuild.joined_message(turn)
    assert "without changing your tool list" in said
    assert "connected_data_search_ndp(q: string): Find NDP datasets." in said


def test_compact_waits_for_the_step_boundary_then_compacts_and_rebuilds(
    running: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    turn = turn_rebuild.join_running_turn(app, "sess_a", "compact")
    loop = running["loop"]
    assert turn["pending"] == "compact_before_next_step" and loop.system == "old prompt"
    assert "compacted before your next step" in turn_rebuild.joined_message(turn)
    compacted: list[Any] = []

    def compact(*args: Any, **kwargs: Any) -> dict[str, Any]:
        compacted.append(kwargs)
        return {"compacted": True, "compactions": [{"compaction_id": "cmp_1"}]}

    monkeypatch.setattr("clio_agent.gact.compaction.compact_session_context", compact)
    monkeypatch.setattr("clio_agent.gact.context.active_app", lambda: app)
    monkeypatch.setattr(
        "clio_agent.gact.working_state.reinject_after_compaction", lambda _r: compacted.append("re")
    )
    assert turn_rebuild.compact_pending_join(loop) is True
    assert compacted[0]["trigger"] == "auto" and compacted[-1] == "re"
    assert loop.autocompact.compaction_id == "cmp_1" and loop.system == "new prompt"
    assert turn_rebuild.compact_pending_join(loop) is False  # once


def test_no_running_turn_and_a_failed_rebuild_are_typed(
    running: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr("clio_agent.gact.agents.clio_react.active_loop", lambda: None)
    assert turn_rebuild.join_running_turn(app, "sess_a", "now")["join_reason"] == "no_running_turn"
    monkeypatch.setattr("clio_agent.gact.agents.clio_react.active_loop", lambda: running["loop"])

    def broken(*_a: Any) -> Any:
        raise RuntimeError("fleet gone")

    monkeypatch.setattr(turn_rebuild, "_rebuilt", broken)
    turn = turn_rebuild.join_running_turn(app, "sess_a", "note")
    assert turn["join_reason"].startswith("rebuild_failed") and "could not join" in (
        turn_rebuild.joined_message(turn)
    )


def test_the_cards_choice_is_validated() -> None:
    assert turn_rebuild.apply_mode({}) == "note"
    assert turn_rebuild.apply_mode({"apply": "now"}) == "now"
    with pytest.raises(ValueError, match="apply must be one of"):
        turn_rebuild.apply_mode({"apply": "later"})
