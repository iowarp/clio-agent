"""One tool result's room: a share of the context window, never more than the room left."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.runtime import context_tokens
from clio_agent.runtime import trace
from clio_agent.tools import injections, result_room
from clio_agent.tools.execution import tool_workspace_context
from clio_agent.tools.mcp_result_projection import bounded_model_tool_result
from tests._config_layer import set_config


@pytest.fixture
def window(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Set the calling model's window and its last prompt; four characters a token."""
    monkeypatch.setattr(context_tokens, "_estimate_text_tokens", lambda text: len(text) // 4)
    monkeypatch.setattr(context_tokens, "_autocompact_threshold", lambda: 0.85)

    def set_window(tokens: int, prompt: int = 0) -> None:
        monkeypatch.setattr("clio_agent.gact.context.active_react_context_window", lambda: tokens)
        monkeypatch.setattr(context_tokens, "_last_prompt_tokens", lambda: prompt)

    return set_window


def test_a_result_that_fits_the_window_share_comes_back_whole(window: Any) -> None:
    """A 200k window gives one result 25,000 tokens: 90,000 characters (22,500 tokens)
    are shown whole, far past the old fixed 12,000. SABOTAGE: return the old
    ``_from_tokens(3_000, ...)`` from ``_measure`` and this result is spilled."""
    window(200_000, prompt=20_000)
    room = result_room.result_room()
    assert (room.tokens, room.chars, room.reason) == (25_000, 100_000, "window_share")
    text = "\n".join(f"row-{i:06d},{'x' * 40}" for i in range(1_800))
    assert 80_000 < len(text) < 100_000
    with injections.collect() as told:
        assert bounded_model_tool_result(text) == text
    assert told == []


def test_the_share_scales_with_the_window(window: Any) -> None:
    window(1_000_000)
    assert result_room.result_room().tokens == 125_000
    window(32_000)
    assert result_room.result_room().tokens == 4_000
    set_config("tools.result_share", 0.5)
    assert result_room.result_room().tokens == 16_000


def test_a_result_past_the_share_is_spilled_with_the_note(window: Any, tmp_path: Path) -> None:
    """SABOTAGE: drop the ``room.fits`` check's spill (return ``text``) and the 120,000
    characters reach the model."""
    window(200_000)
    text = "\n".join(f"row-{i:06d},{'x' * 40}" for i in range(2_400))
    with tool_workspace_context(str(tmp_path)), injections.collect() as told:
        shown = bounded_model_tool_result(text)
    assert shown.startswith("[clio: result_spilled] This result is ")
    assert f"{len(text):,} characters" in shown
    assert 90_000 < len(shown) <= 100_000  # the head fills the room
    assert told == [("result_spilled", shown)]


def test_the_room_left_before_compaction_caps_the_share(window: Any) -> None:
    """170,000 tokens before compaction less a 165,000-token prompt leaves 5,000.
    SABOTAGE: ignore ``room_left`` in ``_measure`` and the share (25,000) is used."""
    window(200_000, prompt=165_000)
    room = result_room.result_room()
    assert (room.tokens, room.reason) == (5_000, "room_left")
    assert not room.fits("x" * 21_000)
    window(200_000, prompt=180_000)
    assert result_room.result_room().tokens == 0


def test_an_unknown_window_uses_the_configured_tokens_and_is_traced(
    window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: remove the ``trace.event`` in ``_measure`` and the miss is silent."""
    events: list[str] = []
    monkeypatch.setattr(trace, "event", lambda _area, message, *args: events.append(message % args))
    window(0)
    room = result_room.result_room()
    assert (room.tokens, room.reason, room.window) == (25_000, "tool_result_window_unknown", 0)
    assert events and events[-1].startswith("tool_result_window_unknown")
    set_config("tools.unknown_window_result_tokens", 100)
    assert result_room.result_room().chars == 400


def test_a_room_measured_at_the_call_travels_with_work_run_apart(window: Any) -> None:
    """Native work runs outside its caller's context; the caller's room is recorded and
    pinned for it. SABOTAGE: make ``room_from_limits`` return ``None`` and the work
    measures an unknown window."""
    window(64_000)
    measured = result_room.result_room()
    limits = result_room.room_limits(measured)
    window(0)
    with result_room.pinned_room(result_room.room_from_limits(limits)):
        assert result_room.result_room() == measured
    assert result_room.result_room().reason == "tool_result_window_unknown"
    assert result_room.room_from_limits({}) is None


def test_a_share_outside_zero_to_one_is_refused() -> None:
    set_config("tools.result_share", 1.5)
    with pytest.raises(ValueError, match="tools.result_share"):
        result_room.result_share()
