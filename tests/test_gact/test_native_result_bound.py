"""Native tool results are bounded once, when the step is recorded."""

from __future__ import annotations

from pathlib import Path

from clio_agent import injections
from clio_agent.gact.agents.native_result_bound import recorded_observation
from clio_agent.tools.execution import tool_workspace_context
from tests._config_layer import set_config


def test_an_oversize_native_result_is_recorded_bounded(tmp_path: Path) -> None:
    set_config("tools.unknown_window_result_tokens", 250)  # 1,000 characters
    value = {"rows": [{"id": i, "text": "x" * 40} for i in range(200)]}
    with tool_workspace_context(str(tmp_path)), injections.collect() as told:
        recorded = recorded_observation("query_tasks", value)
    assert isinstance(recorded, str) and len(recorded) <= 1_000
    assert recorded.startswith("[clio: result_spilled]")
    assert [source for source, _ in told] == ["result_spilled"]


def test_small_results_and_skill_procedures_are_recorded_whole(tmp_path: Path) -> None:
    set_config("tools.unknown_window_result_tokens", 250)  # 1,000 characters
    assert recorded_observation("query_tasks", {"ok": True}) == {"ok": True}
    body = "Step. " * 500
    with tool_workspace_context(str(tmp_path)):
        assert recorded_observation("load_skill", body) == body
