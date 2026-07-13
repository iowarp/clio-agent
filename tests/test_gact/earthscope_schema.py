"""The EarthScope workflow_state schema, transcribed 1:1 from the pre-Phase-C
hardcode (workflow_state/merge.py:74-171, evidence.py:62-92, delegation.py:
169-172/554-688/746-785 @ develop 496641c). Slice F asserts the installed
pack's AGENT.md declaration parses to EXACTLY this object."""

from clio_agent.gact.workflow_state.schema import (
    WorkflowFailureRule,
    WorkflowScrubAliases,
    WorkflowSectionReadiness,
    WorkflowSectionRule,
    WorkflowStateSchema,
)

_PRODUCED = {"complete": 4, "completed": 4, "created": 4, "plotted": 4, "blocked": 2, "missing": 2}
# ^ merge.py:108-115 (profile / visualization / artifact / network_analysis)

EARTHSCOPE_WORKFLOW_STATE_SCHEMA = WorkflowStateSchema(
    sections={
        # merge.py:78-97 + 139-171
        "acquisition": WorkflowSectionRule(
            status_ranks={"staged": 4, "metadata_only": 3, "blocked": 2, "missing": 2},
            readiness=WorkflowSectionReadiness(
                field="analysis_ready",
                ready_status="staged",
                ready_rank=5,  # merge.py:87-88
                requires_ondisk=True,  # merge.py:79-86
                not_on_disk_rank=1,
                path_fields=("local_path", "path"),  # merge.py:79,144
                metadata_path_field="metadata_path",  # merge.py:145
                demote_keep_statuses=("blocked", "missing", "metadata_only"),  # merge.py:156
                demote_status_reused_metadata="metadata_only",  # merge.py:159
                demote_status_default="candidate_found",  # merge.py:161
                blocker_field="blocker",
                blocker_reused_metadata=(
                    "analysis-ready acquisition requires a staged data resource distinct "
                    "from the discovery metadata catalog"
                ),  # merge.py:164-166
                blocker_default="analysis-ready acquisition requires a staged local CSV path",
                # merge.py:167 (pinned by test_agent_blueprints.py:997/1026)
            ),
        ),
        # merge.py:98-107 + 195-202
        "resource_candidate": WorkflowSectionRule(
            status_ranks={"selected": 4, "metadata_only": 3, "missing": 2, "blocked": 2},
            sticky_true_fields=("geographically_grounded",),  # merge.py:196-202
        ),
        "profile": WorkflowSectionRule(status_ranks=dict(_PRODUCED)),
        "visualization": WorkflowSectionRule(status_ranks=dict(_PRODUCED)),
        "artifact": WorkflowSectionRule(status_ranks=dict(_PRODUCED)),
        "network_analysis": WorkflowSectionRule(status_ranks=dict(_PRODUCED)),
        # merge.py:116-125
        "catalog": WorkflowSectionRule(
            status_ranks={
                "candidates_found": 3,
                "metadata_found": 3,
                "search_incomplete": 2,
                "no_candidates": 2,
                "blocked": 2,
            },
        ),
        # merge.py:126-135
        "resource_discovery": WorkflowSectionRule(
            status_ranks={
                "resource_found": 4,
                "candidate_found": 4,
                "search_required": 3,
                "search_exhausted": 2,
                "blocked": 2,
            },
        ),
    },
    # evidence.py:81-92 (order preserved)
    artifact_paths=(
        ("acquisition", "local_path"),
        ("artifact", "path"),
        ("visualization", "path"),
        ("visualization", "plot_path"),
        ("visualization", "staged_plot_png"),
        ("profile", "path"),
    ),
    artifact_extensions=("csv", "png"),  # evidence.py:62,109,121
    aliases=WorkflowScrubAliases(
        # delegation.py:571-573 (`datasets?` -> dataset + datasets; engine sorts longest-first)
        sections=(
            "acquisition",
            "analysis",
            "artifacts",
            "dataset",
            "datasets",
            "evidence",
            "geospatial",
            "region",
            "station_catalog",
        ),
        # delegation.py:626/631/636
        orphan_sections=(
            "acquisition",
            "analysis",
            "artifacts",
            "dataset",
            "datasets",
            "evidence",
            "region",
        ),
        # delegation.py:575 (domain half; core keeps workflow[_ ]state / structured state)
        # + delegation.py:672 (metadata_path | analysis_ready)
        fields=("metadata_path", "analysis_ready"),
        fence_labels=("region",),  # delegation.py:661-665 `[A-Za-z ]*region:`
    ),
    # delegation.py:169-171 -- the parent-resume example keys, in prose order
    resume_example_fields=(
        "acquisition.metadata_path",
        "station_catalog.station_ids",
        "acquisition.status",
        "profile.status",
    ),
    # delegation.py:769-785
    failure_rules=(
        WorkflowFailureRule(
            section="acquisition",
            when="readiness_not_true",
            set_status="blocked",
            set_readiness_false=True,
            set_fields={
                "blocker": "child expert {child!r} failed before completing acquisition: {error}",
            },
        ),
        WorkflowFailureRule(
            section="resource_discovery",
            when="always",
            set_status="child_failed",
            set_fields={
                "blocker": "child expert {child!r} failed before completing resource discovery",
                "next_action": (
                    "retry the child expert after provider availability is restored"
                ),
            },
        ),
    ),
)
