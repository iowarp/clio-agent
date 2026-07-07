"""Slice-B pins for the schema-driven merge/scrub engine (#646/#648, Phase C).

The full byte-identical reproduction of the pre-Phase-C hardcode is proven by
re-running the existing golden suites through the ``schema=`` path. These extra
pins cover the three behaviors that only the parameterized engine exposes:

* an UNDECLARED section (``station_catalog``) keeps presence-only merge (rank 0,
  no precedence gate) rather than falling out of the merge entirely;
* the ``dataset`` / ``datasets`` alias members are both scrubbed — the schema
  stores plain names and the engine builds a longest-first alternation so the
  ``\\.field`` continuation still matches ``datasets.x``;
* the ``region:`` fence label drives the fenced-block scrub, and an empty-alias
  (generic) schema strips nothing (the never-match sentinel).
"""

from __future__ import annotations

from clio_agent.gact.delegation import (
    _clean_public_delegation_prompt,
    _clean_public_transcript_text,
)
from clio_agent.gact.workflow_state.merge import _merge_workflow_state_mapping
from clio_agent.gact.workflow_state.schema import GENERIC_WORKFLOW_STATE_SCHEMA
from tests.test_gact.earthscope_schema import EARTHSCOPE_WORKFLOW_STATE_SCHEMA


def test_undeclared_station_catalog_merges_presence_only() -> None:
    # station_catalog is NOT a ranked section (rank 0 for every status), so it
    # merges by presence/non-empty-overwrite with no precedence demotion.
    assert (
        EARTHSCOPE_WORKFLOW_STATE_SCHEMA.rank("station_catalog", {"status": "cataloged"}) == 0
    )
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


def test_dataset_and_datasets_alias_members_both_scrub() -> None:
    # Longest-first alternation: both the singular and plural section alias must
    # scrub their `.field` sentence (a `dataset|datasets` order would let the
    # singular pre-empt the plural's `\.field` continuation).
    singular = _clean_public_delegation_prompt(
        "Run the pipeline. Update dataset.local_path next. Then stop.",
        schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA,
    )
    assert "dataset.local_path" not in singular
    assert "Run the pipeline." in singular
    assert "Then stop." in singular

    plural = _clean_public_delegation_prompt(
        "Run the pipeline. Update datasets.local_path next. Then stop.",
        schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA,
    )
    assert "datasets.local_path" not in plural
    assert "Run the pipeline." in plural
    assert "Then stop." in plural


def test_region_fence_label_drives_fenced_block_scrub() -> None:
    # A ``<words> region:`` intro followed by a fenced block that mentions
    # ``workflow_state`` is stripped whole — intro line included. The fenced body
    # deliberately avoids the literal ``{"workflow_state"`` prefix so the generic
    # JSON-carrier truncation does not pre-empt the fence-label pattern under test.
    text = (
        "Here is the plan.\n\n"
        "Study region:\n"
        '```json\n{"acquisition": {"status": "staged"}, "workflow_state": "recorded"}\n```\n\n'
        "Done."
    )
    cleaned = _clean_public_transcript_text(text, schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA)
    assert "workflow_state" not in cleaned
    assert "region:" not in cleaned
    assert "Here is the plan." in cleaned
    assert "Done." in cleaned


def test_generic_schema_scrubs_no_domain_paths() -> None:
    # The generic (empty-alias) schema declares no scrub vocabulary, so the
    # never-match sentinel leaves an arbitrary `word.field` sentence untouched —
    # only the CLIO-carrier tokens (workflow_state / structured state) still go.
    text = "Run the pipeline. Update config.value next. Then stop."
    assert (
        _clean_public_delegation_prompt(text, schema=GENERIC_WORKFLOW_STATE_SCHEMA) == text
    )
