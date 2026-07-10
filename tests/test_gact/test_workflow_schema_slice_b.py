"""Slice-B pins for the schema-driven merge engine (#646/#648, Phase C).

The full byte-identical reproduction of the pre-Phase-C hardcode is proven by
re-running the existing golden suites through the ``schema=`` path. This pin
covers the merge behavior that only the parameterized engine exposes: an
UNDECLARED section (``station_catalog``) keeps presence-only merge (rank 0, no
precedence gate) rather than falling out of the merge entirely.

The Slice-B *scrub* pins that lived here were retired in #881: the public-prompt
and visible-transcript prose scrubbers they exercised are DELETED (the client
renders model prose verbatim; the server fixes leaks at the root). The scrub
aliases remain declarable on a pack schema for compatibility but are inert.
"""

from __future__ import annotations

from clio_agent.gact.workflow_state.merge import _merge_workflow_state_mapping
from tests.test_gact.earthscope_schema import EARTHSCOPE_WORKFLOW_STATE_SCHEMA


def test_undeclared_station_catalog_merges_presence_only() -> None:
    # station_catalog is NOT a ranked section (rank 0 for every status), so it
    # merges by presence/non-empty-overwrite with no precedence demotion.
    assert EARTHSCOPE_WORKFLOW_STATE_SCHEMA.rank("station_catalog", {"status": "cataloged"}) == 0
    target: dict[str, object] = {}
    _merge_workflow_state_mapping(
        target,
        {"station_catalog": {"status": "cataloged", "station_ids": ["P473", "P474"]}},
        schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA,
    )
    assert target["station_catalog"] == {"status": "cataloged", "station_ids": ["P473", "P474"]}

    # A later incoming with a different status still merges (rank 0 == rank 0, so
    # the "incoming_rank < current_rank" gate never drops it) and non-empty
    # fields accumulate; the prior station_ids are preserved.
    _merge_workflow_state_mapping(
        target,
        {"station_catalog": {"status": "verified", "region": "cascadia"}},
        schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA,
    )
    assert target["station_catalog"]["status"] == "verified"
    assert target["station_catalog"]["region"] == "cascadia"
    assert target["station_catalog"]["station_ids"] == ["P473", "P474"]
