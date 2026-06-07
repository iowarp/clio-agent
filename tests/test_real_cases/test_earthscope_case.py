"""EarthScope GNSS region — real case acceptance test.

This is the real EarthScope case test (it supersedes the EarthScope-specific
``scripts/run_demo_benchmark.py`` lane): a live CLIO session through the
``earthscope-gnss-region`` blueprint must resolve the geography, acquire real
EarthScope/NDP GNSS station CSV evidence, analyze it, render a PNG, and
synthesize — judged on the normalized trace.

Run live: ``CLIO_RUN_LIVE=1 pytest tests/test_real_cases/test_earthscope_case.py
--provider argonne_sophia``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

CASE_DIR = "benchmark/case02-earthscope-csv-seismic-geography"
PROMPT = Path(CASE_DIR, "prompt.txt").read_text().strip()


@pytest.mark.real_case
@pytest.mark.live
def test_earthscope_gnss_region(agent):
    run = agent.run({
        "task": PROMPT,
        "blueprint_id": "earthscope-gnss-region",
        "case_dir": CASE_DIR,
        "run_label": "acceptance",
        "timeout_s": 600,
    })

    # Runtime/harness invariants.
    assert run.error is None, run.error
    assert run.extra["blueprint_activated"], run.extra.get("active_agent_blueprint_id")

    # Data pathway: real catalog acquisition happened.
    assert run.called("ndp_search_datasets") or run.called("ndp_filter_earthscope_station_catalog"), run.tool_names

    # Route: acquisition -> ... -> synthesis (the workflow actually traversed).
    assert run.routed_to("data"), run.steps
    assert run.routed_to("synthesis"), run.steps

    # Real deliverable: a staged CSV was analyzed into a PNG artifact on disk.
    assert any(p.endswith(".png") for p in run.extra["artifacts"]), (
        f"no PNG artifact produced; artifacts={run.extra['artifacts']}"
    )
