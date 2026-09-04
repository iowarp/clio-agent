"""EarthScope FLAT — the scientist's handcrafted pack, retested UNMODIFIED.

``earthscope-flat`` is the scientist's own handcrafted blueprint (a real user
artifact discovered from the user blueprint registry, NOT a pack this test
suite built), which makes it the authentic MCP v1 fleet evidence for the
client-unification campaign: 4 declared clio-kit v1 servers (ndp/geo/pandas/
plot) driven through the production gateway path by a depth-1 spawn tree.

Owner ruling (2026-09-03): run the pack AS-IS with flat-appropriate
acceptance — the ground-truth pipeline only (NDP discover -> stage -> profile
-> PNG, provenance on the staged station), and NO ``routed_to`` asserts. The
flat topology (main over four self-sufficient leaves, ``ndp`` owning the whole
data branch) is not the gnss-region spawn tree, and pinning its internal
routing would be modifying the acceptance to fit our harness rather than
testing the user's artifact.

The data-pathway matchers are imported from ``test_earthscope_case`` — they
read structured tool evidence only (tool_calls / artifacts), so they are
topology-agnostic and apply to both pack shapes unchanged.

Run live:
  ``CLIO_RUN_LIVE=1 pytest tests/test_real_cases/test_earthscope_flat_case.py \
      -o addopts="" --provider claude_code --model sonnet``
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_real_cases.test_earthscope_case import (
    CASE_DIR,
    PROMPT,
    _staged_station_id,
    _tool_result,
    produced_nonempty_png,
    ran_acquisition_to_plot_pipeline,
    staged_station_on_region,
)

# One positive cell: the prompt's default San Diego geography, proven reliable
# in this same fleet configuration by the gnss-region matrix (5/5 San Diego
# positives). The flat retest is a v1-evidence gate, not a repeatability grind.
FLAT_CELLS: tuple[str, ...] = ("flat_sandiego",)


@pytest.mark.real_case
@pytest.mark.live
@pytest.mark.parametrize("label", FLAT_CELLS)
def test_earthscope_flat(agent, gact_server, label, tmp_path):
    run = agent.run(
        {
            "task": PROMPT,
            "blueprint_id": "earthscope-flat",
            "case_dir": CASE_DIR,
            "run_label": label,
            # Isolated, auto-cleaned workspace root (see clio_sut.invoke).
            "workdir": str(tmp_path),
            "trace_path": str(gact_server.trace_dir / f"{label}.run.jsonl"),
            # No absolute wall clock: the SUT's no-progress watchdog bounds
            # genuine stalls; a slow but progressing run is never killed.
            "timeout_s": 0,
        }
    )

    # Runtime/harness invariants.
    assert run.error is None, run.error
    assert run.extra["blueprint_activated"], run.extra.get("active_agent_blueprint_id")

    # Ground-truth pipeline (flat-appropriate acceptance — no routing asserts):
    # the real acquisition ran end to end on structured tool evidence.
    assert ran_acquisition_to_plot_pipeline(run), run.tool_names

    # Provenance: the staged station is genuinely within the requested radius.
    assert staged_station_on_region(run), (
        f"staged station off-region or unverifiable; "
        f"station={_staged_station_id(run)}, filter={_tool_result(run, 'geo_filter_points_by_radius')}"
    )

    # Real deliverable: a non-empty PNG on disk, inside the isolated workdir.
    assert produced_nonempty_png(run), run.extra.get("artifacts")
    pngs = [p for p in run.extra.get("artifacts", []) if p.endswith(".png")]
    assert pngs, run.extra.get("artifacts")
    for p in pngs:
        assert Path(p).resolve().is_relative_to(tmp_path.resolve()), (
            f"PNG {p!r} written outside the isolated workdir {tmp_path}"
        )
