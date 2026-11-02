"""The explore note: a fact after a run of reading calls, never a spawn (2026-11-02)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import explore_nudge


class _Recorder:
    def __init__(self) -> None:
        self.notes: list[tuple[str, str]] = []

    def injections(self, items: list[tuple[str, str]]) -> None:
        self.notes.extend(items)


def _loop() -> Any:
    tools = {"fs_read_file": object(), "spawn_agents_parallel": object()}
    return SimpleNamespace(agent=SimpleNamespace(tools=tools), recorder=_Recorder())


@pytest.fixture
def reading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(explore_nudge, "_read_only", lambda name: name.startswith("fs_"))
    monkeypatch.setattr(explore_nudge, "_can_explore", lambda loop: "spawn_agents_parallel")
    monkeypatch.setattr(explore_nudge, "_explore_model", lambda: "claude-haiku-5-5")
    monkeypatch.setenv("CLIO_EXPLORE_NUDGE_AFTER", "4")


@pytest.mark.usefixtures("reading")
def test_after_a_run_of_reads_the_loop_notes_explore_once() -> None:
    """The note states the count and the cheaper model; it spawns nothing and comes once.
    SABOTAGE: drop the ``_clio_explore_noted`` check and the second note appears."""
    loop = _loop()
    explore_nudge.note_reading_run(loop, ["fs_glob", "fs_read_file"])
    assert loop.recorder.notes == []
    explore_nudge.note_reading_run(loop, ["fs_read_file", "fs_search_text", "shell_bash"])
    [(source, text)] = loop.recorder.notes
    assert source == explore_nudge.EXPLORE_NUDGE_SOURCE
    assert "4 reading calls" in text and "claude-haiku-5-5" in text
    assert "`spawn_agents_parallel`" in text
    explore_nudge.note_reading_run(loop, ["fs_read_file"] * 9)
    assert len(loop.recorder.notes) == 1


@pytest.mark.usefixtures("reading")
def test_delegating_restarts_the_count_and_the_switch_turns_it_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _loop()
    explore_nudge.note_reading_run(loop, ["fs_read_file"] * 3)
    explore_nudge.note_reading_run(loop, ["spawn_agents_parallel"])
    explore_nudge.note_reading_run(loop, ["fs_read_file"] * 3)
    assert loop.recorder.notes == []
    monkeypatch.setenv("CLIO_EXPLORE_NUDGE", "false")
    explore_nudge.note_reading_run(loop, ["fs_read_file"] * 5)
    assert loop.recorder.notes == []


@pytest.mark.usefixtures("reading")
def test_the_note_is_a_template_and_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_EXPLORE_NUDGE_TEXT", "${calls} reads; $(whoami) ${unknown}")
    loop = _loop()
    explore_nudge.note_reading_run(loop, ["fs_read_file"] * 4)
    assert loop.recorder.notes == [
        (explore_nudge.EXPLORE_NUDGE_SOURCE, "4 reads; $(whoami) ${unknown}")
    ]


def test_no_explore_role_means_no_note(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the spawn tools there is nothing to suggest."""
    monkeypatch.setattr(explore_nudge, "_read_only", lambda name: True)
    monkeypatch.setenv("CLIO_EXPLORE_NUDGE_AFTER", "1")
    loop = SimpleNamespace(agent=SimpleNamespace(tools={"fs_read_file": 1}), recorder=_Recorder())
    explore_nudge.note_reading_run(loop, ["fs_read_file"] * 3)
    assert loop.recorder.notes == []


def test_explore_must_be_a_subagent_of_this_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spawn tools alone are not enough: the session's agent must define ``explore``."""
    from clio_agent.gact import context as ctx
    from clio_agent.gact.agents import resolution

    monkeypatch.setattr(ctx, "active_app", lambda: object())
    monkeypatch.setattr(ctx, "active_session_id", lambda: "sess_x")
    rows: list[dict[str, Any]] = [{"id": "review"}]
    monkeypatch.setattr(
        resolution, "session_resolution", lambda app, sid: SimpleNamespace(subagents=rows)
    )
    assert explore_nudge._can_explore(_loop()) == ""
    rows.append({"id": "explore"})
    assert explore_nudge._can_explore(_loop()) == "spawn_agents_parallel"
