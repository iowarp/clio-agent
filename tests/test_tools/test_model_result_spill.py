"""An oversize tool result: the agent gets its head, and a file with the rest.

The harness does not decide what the agent needs from a big result. Past the room one
result has in the model's context window (:mod:`clio_agent.tools.result_room`) the full
result is written to the session's tool-output folder, and the agent is told how big it
is, that the first characters follow, and where the rest is for it to explore. Without
an agent loop the window is unknown, so these tests size the room with
``tools.unknown_window_result_tokens`` (characters are four a token).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from clio_agent import injections
from clio_agent.tools import mcp_result_projection
from clio_agent.tools.execution import tool_workspace_context
from clio_agent.tools.mcp_result_projection import bounded_model_tool_result
from tests._config_layer import set_config


def _spilled_path(result: str) -> Path:
    match = re.search(r"the full result is in `([^`]+)`", result)
    assert match, result[:400]
    return Path(match.group(1))


def test_a_result_that_fits_is_returned_whole() -> None:
    with injections.collect() as told:
        assert bounded_model_tool_result("small result") == "small result"
    assert told == []


def test_an_oversize_result_goes_to_a_file_and_the_agent_is_told(tmp_path: Path) -> None:
    set_config("tools.unknown_window_result_tokens", 500)
    rows = "\n".join(f"row-{i:05d},{'x' * 40}" for i in range(500))
    with tool_workspace_context(str(tmp_path)), injections.collect() as told:
        result = bounded_model_tool_result(rows)

    assert len(result) <= 2_000
    assert result.startswith("[clio: result_spilled] This result is ")
    assert f"{len(rows):,} characters" in result
    head = result.split("\n\n", 1)[1]
    assert head
    assert told == [("result_spilled", result)]
    assert len(told[0][1]) <= 2_000
    assert "row-00499" not in told[0][1]
    assert rows.startswith(head) and head.endswith(",") is False  # ends on a whole line
    spilled = _spilled_path(result)
    assert spilled.read_text(encoding="utf-8") == rows
    from clio_agent.paths import workspace_state_dir

    assert workspace_state_dir(tmp_path) / "tool-output" in spilled.parents


def test_a_json_result_spills_as_json(tmp_path: Path) -> None:
    set_config("tools.unknown_window_result_tokens", 125)
    payload = '{"rows": [' + ",".join(str(i) for i in range(1_000)) + "]}"
    with tool_workspace_context(str(tmp_path)):
        spilled = _spilled_path(bounded_model_tool_result(payload))
    assert spilled.suffix == ".json"


def test_a_failed_spill_is_told_never_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    set_config("tools.unknown_window_result_tokens", 125)

    def _refuse(_text: str) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(mcp_result_projection, "_spill", _refuse)
    with caplog.at_level("WARNING", logger="clio_agent.tools.mcp_result_projection"):
        with injections.collect() as told:
            result = bounded_model_tool_result("y" * 5_000)
    assert "could not be saved (disk full)" in result
    assert "reason=model_tool_result_spill_failed" in caplog.text
    assert len(result) <= 500
    assert told == [("result_spilled", result)]


def test_the_note_names_the_read_that_continues_a_text_result(tmp_path: Path) -> None:
    set_config("tools.unknown_window_result_tokens", 500)
    rows = "\n".join(f"row-{i:05d},{'x' * 40}" for i in range(500))
    with tool_workspace_context(str(tmp_path)), injections.collect():
        result = bounded_model_tool_result(rows)
    path = _spilled_path(result)
    shown = result.split("\n\n", 1)[1].splitlines()
    following = len(shown) + 1
    assert f'fs_read_file(path="{path}", offset={following})' in result
    # The named offset is the first line the agent was not shown.
    lines = rows.splitlines()
    assert lines[: len(shown)] == shown and lines[following - 1] not in shown


def test_a_json_result_is_saved_indented_for_reading_on(tmp_path: Path) -> None:
    """A9: a JSON result is saved one value a line, so its rest can be paged and searched
    within the file tools' line limits. SABOTAGE: write the text as given -> one line."""
    set_config("tools.unknown_window_result_tokens", 500)
    value = {"rows": ["x" * 50] * 100}
    with tool_workspace_context(str(tmp_path)), injections.collect():
        result = bounded_model_tool_result(json.dumps(value))
    assert "saved as one indented JSON document" in result
    assert "load it in a script" in result and "fs_search_text" in result
    saved = _spilled_path(result).read_text(encoding="utf-8")
    assert json.loads(saved) == value and len(saved.splitlines()) == 104


def test_text_that_only_looks_like_json_is_saved_as_text(tmp_path: Path) -> None:
    set_config("tools.unknown_window_result_tokens", 500)
    rows = "{not json}\n" + "\n".join(f"row-{i:05d},{'x' * 40}" for i in range(500))
    with tool_workspace_context(str(tmp_path)), injections.collect():
        result = bounded_model_tool_result(rows)
    assert _spilled_path(result).suffix == ".txt" and "fs_read_file(path=" in result


def test_a_spill_that_cannot_be_written_is_traced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A24: the typed reason reaches the trace, not only the log.
    SABOTAGE: drop the ``trace.event`` in the spill's ``except`` -> red."""
    from clio_agent.runtime import trace

    events: list[str] = []
    monkeypatch.setattr(trace, "event", lambda _area, message, *a: events.append(message % a))

    def full(*args: object, **kwargs: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(mcp_result_projection, "_spill", full)
    set_config("tools.unknown_window_result_tokens", 500)
    with injections.collect():
        result = bounded_model_tool_result("y" * 10_000)
    assert "could not be saved (disk full)" in result
    assert any(e.startswith("model_tool_result_spill_failed") for e in events)


def test_a_large_result_gets_a_small_sample_its_size_and_how_to_explore_it(
    tmp_path: Path,
) -> None:
    """A spilled result is a pointer, not a context-filling head: the room may be large,
    but the agent gets its size, the file, how to explore it and a short sample."""
    set_config("tools.unknown_window_result_tokens", 25_000)
    set_config("tools.spilled_sample_chars", 800)
    rows = "\n".join(f"row-{i:06d},{'x' * 60}" for i in range(20_000))
    with tool_workspace_context(str(tmp_path)), injections.collect():
        result = bounded_model_tool_result(rows)
    note, sample = result.split("\n\n", 1)
    assert len(sample) <= 800 and rows.startswith(sample)
    assert f"about {len(rows) // 4:,} tokens ({len(rows):,} characters)" in note
    assert "Explore it rather than reading it whole" in note
    assert "it is not the whole result" in note
    assert len(result) < 2_000


def test_a_json_result_comes_with_its_structure(tmp_path: Path) -> None:
    set_config("tools.unknown_window_result_tokens", 2_000)
    value = {
        "datasets": [
            {
                "id": f"d{n}",
                "coverage": {"location": None if n % 2 else {"bbox": [0, 0, 1, 1]}},
                "resources": [{"id": f"r{k}", "format": "TIFF"} for k in range(n)],
            }
            for n in range(40)
        ],
        "total": 40,
    }
    with tool_workspace_context(str(tmp_path)), injections.collect():
        result = bounded_model_tool_result(json.dumps(value))
    note = result.split("\n\n", 1)[0]
    assert "Its structure, to write that query:" in note
    assert (
        "datasets: list of 40 x object {id, coverage: object {location: object {bbox: "
        "list of 4 x int} | null}, resources: list of 0-39 x object {id, format}}"
    ) in note
    assert "  total: int" in note


def test_a_text_result_has_no_structure_section(tmp_path: Path) -> None:
    set_config("tools.unknown_window_result_tokens", 500)
    rows = "\n".join(f"row-{i:05d}" for i in range(2_000))
    with tool_workspace_context(str(tmp_path)), injections.collect():
        result = bounded_model_tool_result(rows)
    assert "Its structure" not in result
