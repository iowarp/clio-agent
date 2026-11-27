"""A server action's toast: ``_meta["io.iowarp/toast"]`` of the tool result (HS2)."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from clio_agent.gact.server_actions import TOAST_META_KEY, ServerActionOutcome, result_toast


def _result(toast: object) -> dict[str, object]:
    return {"content": [], "_meta": {TOAST_META_KEY: toast}}


def test_a_valid_toast_is_kept_and_trimmed() -> None:
    undo = {"label": "Undo", "action": {"event": {"name": "restore", "context": {"id": "a"}}}}
    toast = result_toast(_result({"message": " Removed a ", "tone": "positive", "undo": undo}))

    assert toast == {"message": "Removed a", "tone": "positive", "undo": undo}


def test_tone_defaults_to_neutral_and_the_meta_attribute_is_read() -> None:
    raw = SimpleNamespace(meta={TOAST_META_KEY: {"message": "Saved"}})

    assert result_toast(raw) == {"message": "Saved", "tone": "neutral"}


def test_a_result_without_a_toast_has_none() -> None:
    assert result_toast({"content": []}) is None
    assert result_toast(SimpleNamespace(meta=None)) is None


@pytest.mark.parametrize(
    "toast",
    [
        "Saved",
        {"tone": "positive"},
        {"message": ""},
        {"message": "x" * 301},
        {"message": "Saved", "tone": "loud"},
        {"message": "Saved", "undo": {"label": "Undo"}},
        {"message": "Saved", "undo": {"label": "", "action": {"event": {"name": "a"}}}},
        {"message": "Saved", "undo": {"label": "x" * 41, "action": {"event": {"name": "a"}}}},
        {"message": "Saved", "undo": {"label": "Undo", "action": {"event": {"name": " "}}}},
        {
            "message": "Saved",
            "undo": {"label": "Undo", "action": {"event": {"name": "a", "context": 1}}},
        },
    ],
)
def test_an_invalid_toast_is_dropped_with_a_log_line(
    toast: object, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="clio_agent.gact.server_actions"):
        assert result_toast(_result(toast), tool="remove") is None

    assert "a2ui_action_toast_dropped tool=remove" in caplog.text


def test_the_wire_row_carries_the_toast_only_when_there_is_one() -> None:
    plain = ServerActionOutcome(tool="remove", outcome="completed")
    toasted = ServerActionOutcome(
        tool="remove", outcome="completed", toast={"message": "Removed", "tone": "positive"}
    )

    assert "toast" not in plain.wire("a1")
    assert toasted.wire("a1")["toast"] == {"message": "Removed", "tone": "positive"}
