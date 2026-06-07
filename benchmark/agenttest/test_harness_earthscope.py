"""Harness driver test — EarthScope.

This is not the EarthScope *acceptance* test. It uses the known-good EarthScope
blueprint as the driver to prove the agent-test harness itself works end to end:
the SUT can set the provider/model, activate a blueprint, run a live turn, and
normalize the trace into a `Run` with tools, route, and structured state. If
this passes, the harness is trustworthy for the wildfire grind.
"""

from __future__ import annotations

from pathlib import Path

import pytest

CASE_DIR = "benchmark/case02-earthscope-csv-seismic-geography"
PROMPT = Path(CASE_DIR, "prompt.txt").read_text().strip()


@pytest.mark.live
def test_earthscope_harness_drives_clio(agent):
    run = agent.run({
        "task": PROMPT,
        "blueprint_id": "earthscope-gnss-region",
        "case_dir": CASE_DIR,
        "run_label": "harness-driver",
        "timeout_s": 540,
    })

    # The turn settled without a transport/runtime error.
    assert run.error is None, run.error
    # The SUT actually activated the requested blueprint.
    assert run.extra["blueprint_activated"], run.extra.get("active_agent_blueprint_id")
    # The harness captured real tool calls and a real expert route.
    assert run.tool_calls, "no tool calls normalized from the trace"
    assert run.routed_to("data"), run.steps
    # And it wrote the trace where the SUT convention says it should.
    assert Path(run.extra["trace_path"]).is_file()
