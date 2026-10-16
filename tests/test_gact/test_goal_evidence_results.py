"""A long tool command must not hide the exit code and verification evidence."""

from __future__ import annotations

import json
from types import SimpleNamespace

from clio_agent.gact.goal_evidence import _tool_result_fact


def test_judge_sees_command_outcome_and_tail_after_large_command() -> None:
    result = {
        "command": "authoring code " * 2000,
        "exit_code": 0,
        "timed_out": False,
        "stdout": "verbose diagnostics\n" * 1000 + "32 passed in 1.4s",
        "stderr": "",
    }
    for part in (
        SimpleNamespace(structured_content=result),
        SimpleNamespace(metadata={"result": {"structuredContent": result}}),
    ):
        fact = _tool_result_fact(part)
        parsed = json.loads(fact)
        assert parsed["exit_code"] == 0 and parsed["timed_out"] is False
        assert "32 passed in 1.4s" in parsed["stdout"]
        assert "command" not in parsed and len(fact) < 2600
