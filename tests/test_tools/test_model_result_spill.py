"""An oversize tool result: the agent gets its head, and a file with the rest.

The harness does not decide what the agent needs from a big result. Past
``limits.model_tool_result_chars`` the full result is written to the session's
tool-output folder, and the agent is told how big it is, that the first characters
follow, and where the rest is for it to explore.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from clio_agent.tools import mcp_result_projection
from clio_agent.tools.execution import tool_workspace_context
from clio_agent.tools.mcp_result_projection import bounded_model_tool_result
from tests._config_layer import set_config


def _spilled_path(result: str) -> Path:
    match = re.search(r"the full result is in `([^`]+)`", result)
    assert match, result[:400]
    return Path(match.group(1))


def test_a_result_that_fits_is_returned_whole() -> None:
    assert bounded_model_tool_result("small result") == "small result"


def test_an_oversize_result_goes_to_a_file_and_the_agent_is_told(tmp_path: Path) -> None:
    set_config("limits", {"model_tool_result_chars": 2_000})
    rows = "\n".join(f"row-{i:05d},{'x' * 40}" for i in range(500))
    with tool_workspace_context(str(tmp_path)):
        result = bounded_model_tool_result(rows)

    assert len(result) <= 2_000
    assert result.startswith("[clio: result_spilled] This result is ")
    assert f"{len(rows):,} characters" in result
    head = result.split("\n\n", 1)[1]
    assert rows.startswith(head) and head.endswith(",") is False  # ends on a whole line
    spilled = _spilled_path(result)
    assert spilled.read_text(encoding="utf-8") == rows
    assert tmp_path / ".clio" / "tool-output" in spilled.parents


def test_a_json_result_spills_as_json(tmp_path: Path) -> None:
    set_config("limits", {"model_tool_result_chars": 500})
    payload = '{"rows": [' + ",".join(str(i) for i in range(1_000)) + "]}"
    with tool_workspace_context(str(tmp_path)):
        spilled = _spilled_path(bounded_model_tool_result(payload))
    assert spilled.suffix == ".json"


def test_a_failed_spill_is_told_never_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    set_config("limits", {"model_tool_result_chars": 500})

    def _refuse(_text: str) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(mcp_result_projection, "_spill", _refuse)
    with caplog.at_level("WARNING", logger="clio_agent.tools.mcp_result_projection"):
        result = bounded_model_tool_result("y" * 5_000)
    assert "could not be saved (disk full)" in result
    assert "reason=model_tool_result_spill_failed" in caplog.text
    assert len(result) <= 500
