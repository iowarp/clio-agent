"""De-domaining proof for the pack-declared workflow_state engine (#646, Phase C).

Slice B extracted the EarthScope vocabulary (section names, status precedence,
readiness/blocker prose, artifact fields+extensions, scrub aliases, failure
rules) out of the core engine (``workflow_state/merge.py``, ``evidence.py``,
``delegation.py``) and into a pack-declared :class:`WorkflowStateSchema`. The
core must now be a *generic* engine: give it a DIFFERENT vocabulary and it must
merge, ground, and stamp that vocabulary just as faithfully — with zero
EarthScope leakage. (The prose-scrub half of Slice B was DELETED in #881; the
``aliases`` block stays declarable for pack compatibility but is inert, so the
widget schema below still declares it to prove a non-EarthScope pack validates.)

This module proves that two ways:

1. A synthetic ``WIDGET_FACTORY_SCHEMA`` (a fictional widget-factory domain that
   shares no tokens with EarthScope) is driven through the real engine
   functions; each behavior the EarthScope schema exercises is re-exercised with
   the widget vocabulary, and EarthScope-only tokens are asserted inert.
2. A permanent, case-insensitive grep-guard over the three engine files asserts
   that not one EarthScope domain literal survives in core (comments and
   docstrings count — the vocabulary must live only in the pack declaration).
"""

from __future__ import annotations

from pathlib import Path

from clio_agent.gact.app import (
    _merge_workflow_state_mapping,
)
from clio_agent.gact.workflow_state.schema import (
    WorkflowFailureRule,
    WorkflowScrubAliases,
    WorkflowSectionReadiness,
    WorkflowSectionRule,
    WorkflowStateSchema,
)

# --------------------------------------------------------------------------- #
# The synthetic domain: a widget factory. Nothing here is EarthScope.          #
# --------------------------------------------------------------------------- #

WIDGET_FACTORY_SCHEMA = WorkflowStateSchema(
    sections={
        "molding": WorkflowSectionRule(
            status_ranks={"cast": 4, "prototyped": 3, "jammed": 2},
            readiness=WorkflowSectionReadiness(
                field="ship_ready",
                ready_status="cast",
                ready_rank=5,
                requires_ondisk=False,
                not_on_disk_rank=1,
                path_fields=("mold_path",),
                demote_keep_statuses=("jammed",),
                demote_status_reused_metadata="prototyped",
                demote_status_default="prototyped",
                blocker_field="blocker",
                blocker_reused_metadata="ship-ready molding requires a fresh production mold",
                blocker_default="ship-ready molding requires a cast production mold",
            ),
        ),
        "artwork": WorkflowSectionRule(
            status_ranks={"drawn": 4, "sketched": 2},
            sticky_true_fields=("vector_true",),
        ),
    },
    artifact_paths=(("artwork", "path"),),
    artifact_extensions=("svg",),
    aliases=WorkflowScrubAliases(
        sections=("molding", "artwork", "assembly"),
        orphan_sections=("molding", "assembly"),
        fields=("pour_temp", "ship_ready"),
        fence_labels=("factory",),
    ),
    resume_example_fields=("molding.pour_temp", "artwork.status"),
    failure_rules=(
        WorkflowFailureRule(
            section="molding",
            when="always",
            set_status="jammed",
            set_fields={
                "blocker": "child expert {child!r} failed before completing molding: {error}",
            },
        ),
    ),
)


# --------------------------------------------------------------------------- #
# 1. Behavioral proof — the engine speaks the widget vocabulary.               #
# --------------------------------------------------------------------------- #


def test_widget_rank_precedence_merges_correctly() -> None:
    """Higher-rank widget status wins; lower-rank incoming is dropped — driven
    entirely by ``WIDGET_FACTORY_SCHEMA.status_ranks`` (cast 4 > prototyped 3 >
    jammed 2), with no EarthScope status names anywhere."""

    target: dict = {"molding": {"status": "prototyped"}}
    # cast (4) beats the current prototyped (3): status progresses.
    _merge_workflow_state_mapping(
        target, {"molding": {"status": "cast"}}, schema=WIDGET_FACTORY_SCHEMA
    )
    assert target["molding"]["status"] == "cast"
    # jammed (2) loses to the current cast (4): the merge is skipped.
    _merge_workflow_state_mapping(
        target, {"molding": {"status": "jammed"}}, schema=WIDGET_FACTORY_SCHEMA
    )
    assert target["molding"]["status"] == "cast"


def test_widget_sticky_true_field_survives_incoming_false() -> None:
    """A schema-declared sticky field (``artwork.vector_true``) that is currently
    True is not clobbered by an equal-rank incoming False."""

    target: dict = {"artwork": {"status": "drawn", "vector_true": True}}
    _merge_workflow_state_mapping(
        target,
        {"artwork": {"status": "drawn", "vector_true": False}},
        schema=WIDGET_FACTORY_SCHEMA,
    )
    assert target["artwork"]["vector_true"] is True


# The grounding de-domaining proof moved to the registry-sourced grounding-parity
# suite (S7 #973, ``test_artifacts_s7_grounding.py``): answer grounding no longer
# disk-scans ``workflow_state.artifact_paths`` — it rewrites against the session's
# REGISTERED artifacts. The widget-schema grounding case is re-exercised there
# (``test_parity_widget_schema_grounds_svg_and_leaves_csv_png_untouched``).


def test_widget_failure_rule_stamps_molding_not_earthscope_sections() -> None:
    """The widget failure rule stamps only its declared section (``molding``);
    the EarthScope sections ``acquisition`` / ``resource_discovery`` — which this
    schema knows nothing about — are left untouched."""

    state: dict = {
        "molding": {"status": "cast"},
        "acquisition": {"status": "staged"},
        "resource_discovery": {"status": "resource_found"},
    }
    WIDGET_FACTORY_SCHEMA.apply_failure_rules(
        state,
        child_agent_id="press-bot",
        error="press overheated",
        message="molding aborted",
    )

    assert state["molding"]["status"] == "jammed"
    assert "press-bot" in state["molding"]["blocker"]
    # EarthScope sections are not in the widget schema — never stamped.
    assert state["acquisition"] == {"status": "staged"}
    assert state["resource_discovery"] == {"status": "resource_found"}


# --------------------------------------------------------------------------- #
# 2. Permanent grep-guard — no EarthScope literal survives in the engine.      #
# --------------------------------------------------------------------------- #

# Case-insensitive whole-file scan (comments + docstrings included). These are
# the EarthScope-domain tokens that lived in the pre-Phase-C hardcode and must
# now exist ONLY in the pack declaration, never in the generic engine. Add a
# token here whenever a new domain literal is de-domained; never remove one to
# paper over a regression.
_FORBIDDEN_DOMAIN_TOKENS = (
    "acquisition",
    "station_catalog",
    "resource_candidate",
    "resource_discovery",
    "network_analysis",
    "geographically_grounded",
    "analysis_ready",
    "metadata_path",
    "candidate_found",
    "geospatial",
    "csv",
    "png",
)

_ENGINE_FILES = (
    "workflow_state/merge.py",
    "evidence.py",
    "delegation.py",
    # S7 (#973): answer grounding re-sourced from the registry — must stay generic.
    "artifacts/grounding.py",
)

_GACT_DIR = Path(__import__("clio_agent.gact.app", fromlist=["__file__"]).__file__).resolve().parent


def test_engine_files_carry_no_earthscope_domain_literals() -> None:
    """PERMANENT DE-DOMAINING GUARD.

    The workflow_state engine is generic: its domain vocabulary is declared per
    pack, not baked into core. Any EarthScope literal reappearing in these three
    files (even in a comment) means a fix bolted the domain back onto the engine
    — fail loudly here so it never rots the extraction."""

    violations: list[str] = []
    for rel in _ENGINE_FILES:
        path = _GACT_DIR / rel
        lowered = path.read_text(encoding="utf-8").lower()
        for token in _FORBIDDEN_DOMAIN_TOKENS:
            if token in lowered:
                violations.append(f"{rel}: '{token}'")

    assert not violations, (
        "EarthScope domain literals leaked back into the generic workflow_state "
        f"engine: {violations}"
    )
