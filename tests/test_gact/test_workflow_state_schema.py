"""Exhaustive engine tests for the pack-declared WorkflowStateSchema (#646, Phase C slice A).

Each rank / normalize / failure-rule branch of the pre-Phase-C hardcode
(workflow_state/merge.py:74-171, delegation.py:769-785) is pinned here against
the EARTHSCOPE fixture (tests/test_gact/earthscope_schema.py), plus small
custom schemas for the branches EarthScope's values do not exercise, plus the
domain-free GENERIC default.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from clio_agent.gact.workflow_state.schema import (
    GENERIC_WORKFLOW_STATE_SCHEMA,
    WorkflowFailureRule,
    WorkflowSectionReadiness,
    WorkflowSectionRule,
    WorkflowStateSchema,
)
from tests.test_gact.earthscope_schema import EARTHSCOPE_WORKFLOW_STATE_SCHEMA

SCHEMA = EARTHSCOPE_WORKFLOW_STATE_SCHEMA


# --------------------------------------------------------------------------- #
# rank() -- every branch of merge.py:74-136
# --------------------------------------------------------------------------- #
class TestRankAcquisitionReadiness:
    def test_staged_ready_no_path_is_ready_rank(self) -> None:
        # local_path absent -> "".startswith(('/','~')) is False -> ready_rank
        assert SCHEMA.rank("acquisition", {"status": "staged", "analysis_ready": True}) == 5

    def test_staged_ready_relative_path_is_ready_rank(self) -> None:
        # relative path never triggers the on-disk demotion (guard only for /,~)
        assert (
            SCHEMA.rank(
                "acquisition",
                {"status": "staged", "analysis_ready": True, "local_path": "rel/data.csv"},
            )
            == 5
        )

    def test_staged_ready_existing_absolute_path_is_ready_rank(self, tmp_path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("x")
        # POSIX: hits the is_file()==True route; Windows: non-'/' path skips the guard.
        assert (
            SCHEMA.rank(
                "acquisition",
                {"status": "staged", "analysis_ready": True, "local_path": str(f)},
            )
            == 5
        )

    def test_staged_ready_missing_absolute_path_demotes_to_not_on_disk_rank(self) -> None:
        assert (
            SCHEMA.rank(
                "acquisition",
                {
                    "status": "staged",
                    "analysis_ready": True,
                    "local_path": "/nonexistent/does_not_exist.csv",
                },
            )
            == 1
        )

    def test_path_field_or_chain_prefers_local_path_then_path(self) -> None:
        # local_path missing-on-disk wins over a (would-be) relative `path`
        assert (
            SCHEMA.rank(
                "acquisition",
                {
                    "status": "staged",
                    "analysis_ready": True,
                    "local_path": "/nope/x.csv",
                    "path": "rel/ok.csv",
                },
            )
            == 1
        )

    def test_ready_check_requires_field_true(self) -> None:
        # analysis_ready not True -> falls through the readiness block to bare status_ranks
        assert SCHEMA.rank("acquisition", {"status": "staged", "analysis_ready": False}) == 4


class TestRankAcquisitionStatusTable:
    def test_staged_bare(self) -> None:
        assert SCHEMA.rank("acquisition", {"status": "staged"}) == 4

    def test_metadata_only(self) -> None:
        assert SCHEMA.rank("acquisition", {"status": "metadata_only"}) == 3

    @pytest.mark.parametrize("status", ["blocked", "missing"])
    def test_blocked_missing(self, status: str) -> None:
        assert SCHEMA.rank("acquisition", {"status": status}) == 2

    def test_unlisted_truthy_status_floor(self) -> None:
        assert SCHEMA.rank("acquisition", {"status": "surprising"}) == 1

    def test_empty_status(self) -> None:
        assert SCHEMA.rank("acquisition", {"status": ""}) == 0
        assert SCHEMA.rank("acquisition", {}) == 0

    def test_status_is_stripped_and_lowercased(self) -> None:
        assert SCHEMA.rank("acquisition", {"status": "  STAGED  "}) == 4


class TestRankResourceCandidate:
    @pytest.mark.parametrize(
        "status,expected",
        [
            ("selected", 4),
            ("metadata_only", 3),
            ("missing", 2),
            ("blocked", 2),
            ("weird", 1),
            ("", 0),
        ],
    )
    def test_table(self, status: str, expected: int) -> None:
        assert SCHEMA.rank("resource_candidate", {"status": status}) == expected


class TestRankProducedFamily:
    @pytest.mark.parametrize("section", ["profile", "visualization", "artifact", "network_analysis"])
    @pytest.mark.parametrize(
        "status,expected",
        [
            ("complete", 4),
            ("completed", 4),
            ("created", 4),
            ("plotted", 4),
            ("blocked", 2),
            ("missing", 2),
            ("weird", 1),
            ("", 0),
        ],
    )
    def test_table(self, section: str, status: str, expected: int) -> None:
        assert SCHEMA.rank(section, {"status": status}) == expected


class TestRankCatalog:
    @pytest.mark.parametrize(
        "status,expected",
        [
            ("candidates_found", 3),
            ("metadata_found", 3),
            ("search_incomplete", 2),
            ("no_candidates", 2),
            ("blocked", 2),
            ("weird", 1),
            ("", 0),
        ],
    )
    def test_table(self, status: str, expected: int) -> None:
        # search_incomplete and no_candidates/blocked all tie at 2 -- losing the tie changes merges
        assert SCHEMA.rank("catalog", {"status": status}) == expected


class TestRankResourceDiscovery:
    @pytest.mark.parametrize(
        "status,expected",
        [
            ("resource_found", 4),
            ("candidate_found", 4),
            ("search_required", 3),
            ("search_exhausted", 2),
            ("blocked", 2),
            ("weird", 1),
            ("", 0),
        ],
    )
    def test_table(self, status: str, expected: int) -> None:
        assert SCHEMA.rank("resource_discovery", {"status": status}) == expected


class TestRankUndeclaredSection:
    def test_undeclared_section_is_presence_only_even_with_status(self) -> None:
        # station_catalog / geospatial / delegation are NOT ranked -> always 0
        assert SCHEMA.rank("station_catalog", {"status": "selected"}) == 0
        assert SCHEMA.rank("geospatial", {"status": "complete"}) == 0
        assert SCHEMA.rank("delegation", {"status": "failed"}) == 0


class TestRankReadinessRequiresOndiskFalse:
    def test_requires_ondisk_false_skips_demotion(self) -> None:
        schema = WorkflowStateSchema(
            sections={
                "acquisition": WorkflowSectionRule(
                    status_ranks={"staged": 4},
                    readiness=WorkflowSectionReadiness(
                        field="analysis_ready",
                        ready_status="staged",
                        ready_rank=5,
                        requires_ondisk=False,
                        path_fields=("local_path",),
                        demote_keep_statuses=(),
                        demote_status_reused_metadata="metadata_only",
                        demote_status_default="candidate_found",
                        blocker_reused_metadata="reused",
                        blocker_default="default",
                    ),
                )
            }
        )
        # absolute missing path but requires_ondisk False -> no demotion -> ready_rank
        assert (
            schema.rank(
                "acquisition",
                {"status": "staged", "analysis_ready": True, "local_path": "/nope/x.csv"},
            )
            == 5
        )


# --------------------------------------------------------------------------- #
# normalize_section() -- every branch of merge.py:139-171
# --------------------------------------------------------------------------- #
class TestNormalizeNoReadiness:
    def test_section_without_readiness_is_scalar_normalize_only(self) -> None:
        out = SCHEMA.normalize_section("profile", {"status": "complete", "path": "a/b‐c.csv"})
        # unicode hyphen in a path-like field is normalized; nothing else touched
        assert out == {"status": "complete", "path": "a/b-c.csv"}

    def test_undeclared_section_is_scalar_normalize_only(self) -> None:
        out = SCHEMA.normalize_section("station_catalog", {"status": "selected", "note": "x"})
        assert out == {"status": "selected", "note": "x"}

    def test_scalar_normalize_leaves_non_path_fields(self) -> None:
        out = SCHEMA.normalize_section("profile", {"status": "complete", "label": "a—b"})
        assert out["label"] == "a—b"  # label is not path-like -> untouched


class TestNormalizeAcquisitionDemote:
    def test_demote_keep_status_blocked(self) -> None:
        out = SCHEMA.normalize_section(
            "acquisition", {"status": "blocked", "analysis_ready": True}
        )
        assert out["analysis_ready"] is False
        assert out["status"] == "blocked"  # kept
        assert out["blocker"] == "analysis-ready acquisition requires a staged local CSV path"

    def test_demote_keep_status_metadata_only(self) -> None:
        out = SCHEMA.normalize_section(
            "acquisition", {"status": "metadata_only", "analysis_ready": True}
        )
        assert out["status"] == "metadata_only"
        assert out["analysis_ready"] is False

    def test_demote_reused_metadata_becomes_metadata_only(self) -> None:
        out = SCHEMA.normalize_section(
            "acquisition",
            {
                "status": "staged",
                "analysis_ready": True,
                "local_path": "/data/catalog.csv",
                "metadata_path": "/data/catalog.csv",
            },
        )
        assert out["status"] == "metadata_only"
        assert out["analysis_ready"] is False
        assert out["blocker"] == (
            "analysis-ready acquisition requires a staged data resource distinct "
            "from the discovery metadata catalog"
        )

    def test_demote_default_candidate_found(self) -> None:
        # analysis_ready True but no local_path and status staged -> default demotion
        out = SCHEMA.normalize_section(
            "acquisition", {"status": "staged", "analysis_ready": True}
        )
        assert out["status"] == "candidate_found"
        assert out["analysis_ready"] is False
        assert out["blocker"] == "analysis-ready acquisition requires a staged local CSV path"

    def test_setdefault_does_not_clobber_existing_blocker(self) -> None:
        out = SCHEMA.normalize_section(
            "acquisition",
            {"status": "staged", "analysis_ready": True, "blocker": "custom blocker"},
        )
        assert out["blocker"] == "custom blocker"

    def test_genuinely_ready_pops_blocker(self) -> None:
        out = SCHEMA.normalize_section(
            "acquisition",
            {
                "status": "staged",
                "analysis_ready": True,
                "local_path": "rel/real.csv",
                "blocker": "stale",
            },
        )
        assert out["analysis_ready"] is True
        assert out["status"] == "staged"
        assert "blocker" not in out

    def test_not_ready_is_untouched(self) -> None:
        out = SCHEMA.normalize_section(
            "acquisition", {"status": "staged", "analysis_ready": False}
        )
        assert out == {"status": "staged", "analysis_ready": False}


class TestNormalizeMetadataPathFieldDisabled:
    def test_empty_metadata_path_field_disables_reuse_check(self) -> None:
        schema = WorkflowStateSchema(
            sections={
                "acquisition": WorkflowSectionRule(
                    status_ranks={"staged": 4},
                    readiness=WorkflowSectionReadiness(
                        field="analysis_ready",
                        ready_status="staged",
                        ready_rank=5,
                        path_fields=("local_path",),
                        metadata_path_field="",  # disables reuse detection
                        demote_keep_statuses=(),
                        demote_status_reused_metadata="metadata_only",
                        demote_status_default="candidate_found",
                        blocker_reused_metadata="reused",
                        blocker_default="default",
                    ),
                )
            }
        )
        # local_path == metadata_path, but reuse check disabled -> stays genuinely ready
        out = schema.normalize_section(
            "acquisition",
            {
                "status": "staged",
                "analysis_ready": True,
                "local_path": "rel/x.csv",
                "metadata_path": "rel/x.csv",
            },
        )
        assert out["analysis_ready"] is True
        assert out["status"] == "staged"


# --------------------------------------------------------------------------- #
# sticky_true_fields_for()
# --------------------------------------------------------------------------- #
class TestStickyFields:
    def test_resource_candidate_sticky(self) -> None:
        assert SCHEMA.sticky_true_fields_for("resource_candidate") == ("geographically_grounded",)

    def test_section_without_sticky(self) -> None:
        assert SCHEMA.sticky_true_fields_for("acquisition") == ()

    def test_undeclared_section(self) -> None:
        assert SCHEMA.sticky_true_fields_for("station_catalog") == ()


# --------------------------------------------------------------------------- #
# apply_failure_rules() -- delegation.py:769-785
# --------------------------------------------------------------------------- #
class TestApplyFailureRules:
    def test_both_rules_stamp(self) -> None:
        state = {
            "acquisition": {"status": "staged", "analysis_ready": False},
            "resource_discovery": {"status": "search_required"},
        }
        SCHEMA.apply_failure_rules(state, child_agent_id="ndp", error="boom", message="msg")
        assert state["acquisition"]["status"] == "blocked"
        assert state["acquisition"]["analysis_ready"] is False
        assert state["acquisition"]["blocker"] == (
            "child expert 'ndp' failed before completing acquisition: boom"
        )
        assert state["resource_discovery"]["status"] == "child_failed"
        assert state["resource_discovery"]["blocker"] == (
            "child expert 'ndp' failed before completing resource discovery"
        )
        assert state["resource_discovery"]["next_action"] == (
            "retry the child expert after provider availability is restored"
        )

    def test_acquisition_ready_true_short_circuits(self) -> None:
        # readiness_not_true rule: analysis_ready True -> acquisition untouched
        state = {"acquisition": {"status": "staged", "analysis_ready": True}}
        SCHEMA.apply_failure_rules(state, child_agent_id="ndp", error="boom", message="m")
        assert state["acquisition"] == {"status": "staged", "analysis_ready": True}

    def test_missing_readiness_field_treated_as_not_true(self) -> None:
        state = {"acquisition": {"status": "staged"}}
        SCHEMA.apply_failure_rules(state, child_agent_id="ndp", error="boom", message="m")
        assert state["acquisition"]["status"] == "blocked"
        assert state["acquisition"]["analysis_ready"] is False

    def test_section_absent_is_noop(self) -> None:
        state: dict = {"profile": {"status": "complete"}}
        SCHEMA.apply_failure_rules(state, child_agent_id="ndp", error="boom", message="m")
        assert state == {"profile": {"status": "complete"}}

    def test_section_present_but_not_dict_is_skipped(self) -> None:
        state = {"acquisition": "not-a-dict", "resource_discovery": {"status": "x"}}
        SCHEMA.apply_failure_rules(state, child_agent_id="ndp", error="boom", message="m")
        assert state["acquisition"] == "not-a-dict"
        assert state["resource_discovery"]["status"] == "child_failed"

    def test_always_rule_fires_regardless_of_readiness(self) -> None:
        state = {"resource_discovery": {"status": "resource_found", "analysis_ready": True}}
        SCHEMA.apply_failure_rules(state, child_agent_id="c", error="e", message="m")
        assert state["resource_discovery"]["status"] == "child_failed"


# --------------------------------------------------------------------------- #
# GENERIC -- domain-free default
# --------------------------------------------------------------------------- #
class TestGeneric:
    @pytest.mark.parametrize(
        "section,value",
        [
            ("acquisition", {"status": "staged", "analysis_ready": True}),
            ("profile", {"status": "complete"}),
            ("catalog", {"status": "candidates_found"}),
            ("anything", {"status": "whatever"}),
        ],
    )
    def test_all_ranks_zero(self, section: str, value: dict) -> None:
        assert GENERIC_WORKFLOW_STATE_SCHEMA.rank(section, value) == 0

    def test_normalize_is_scalar_only(self) -> None:
        out = GENERIC_WORKFLOW_STATE_SCHEMA.normalize_section(
            "acquisition",
            {"status": "staged", "analysis_ready": True, "local_path": "a‐b.csv"},
        )
        # no demotion, no blocker -- only the path scalar-normalize applies
        assert out == {"status": "staged", "analysis_ready": True, "local_path": "a-b.csv"}

    def test_no_sticky_fields(self) -> None:
        assert GENERIC_WORKFLOW_STATE_SCHEMA.sticky_true_fields_for("resource_candidate") == ()

    def test_no_failure_rules(self) -> None:
        state = {"acquisition": {"status": "staged", "analysis_ready": False}}
        GENERIC_WORKFLOW_STATE_SCHEMA.apply_failure_rules(
            state, child_agent_id="c", error="e", message="m"
        )
        assert state == {"acquisition": {"status": "staged", "analysis_ready": False}}


# --------------------------------------------------------------------------- #
# model invariants
# --------------------------------------------------------------------------- #
class TestModelInvariants:
    def test_frozen(self) -> None:
        with pytest.raises(ValidationError):
            SCHEMA.artifact_extensions = ("svg",)  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            WorkflowSectionRule(status_ranks={"a": 1}, bogus=True)  # type: ignore[call-arg]

    def test_failure_rule_when_literal_rejects_unknown(self) -> None:
        with pytest.raises(ValidationError):
            WorkflowFailureRule(section="a", when="sometimes", set_status="x")  # type: ignore[arg-type]

    def test_generic_is_empty(self) -> None:
        assert GENERIC_WORKFLOW_STATE_SCHEMA.sections == {}
        assert GENERIC_WORKFLOW_STATE_SCHEMA.artifact_extensions == ()
        assert GENERIC_WORKFLOW_STATE_SCHEMA.failure_rules == ()
