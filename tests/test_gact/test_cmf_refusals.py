"""The CMF refusal catalog is a CLOSED, typed vocabulary (shape (a), server mode).

Mirrors the ``_STREAM_FALLBACK_REASON_DEFINITIONS`` contract tests: every reason
carries its category and recovery actions, an unknown key raises rather than
inventing a degradation, and the exception type carries the same payload every
surface reports.
"""

from __future__ import annotations

import pytest

from clio_agent.gact.artifacts.provenance.cmf_reasons import (
    CMF_REFUSAL_REASON_DEFINITIONS,
    CMFRefusal,
    cmf_refusal_payload,
    cmf_refusal_reasons,
)

# The catalog the deployment-shape design fixed. Spelled out here rather than
# derived from the module so a silent addition/removal fails this test.
_EXPECTED_REASONS = {
    "cmf_no_write_target",
    "cmf_conflicting_write_targets",
    "cmf_local_runtime_unsupported_platform",
    "cmf_local_runtime_unavailable",
    "cmf_server_unreachable",
    "cmf_server_rejected_payload",
    "cmf_server_version_incompatible",
    "cmf_server_discarded_entities",
    "cmf_artifact_not_attached_to_execution",
    "cmf_artifact_reference_unresolved",
    "cmf_artifact_kind_not_representable",
    "cmf_lineage_query_unavailable",
    # Reserved for deployment shape (d); declared, never raised today.
    "cmf_worker_url_unsupported",
}


def test_cmf_refusal_catalog_is_the_closed_declared_set() -> None:
    assert set(CMF_REFUSAL_REASON_DEFINITIONS) == _EXPECTED_REASONS


@pytest.mark.parametrize("reason", sorted(_EXPECTED_REASONS))
def test_every_reason_declares_category_recovery_and_description(reason: str) -> None:
    definition = CMF_REFUSAL_REASON_DEFINITIONS[reason]
    assert definition["category"], f"{reason} has no category"
    assert definition["recovery_actions"], f"{reason} offers no recovery action"
    assert len(str(definition["description"])) > 40, f"{reason} has no real description"
    # A refusal never claims the write happened.
    assert definition["writes"] is False


@pytest.mark.parametrize("reason", sorted(_EXPECTED_REASONS))
def test_payload_carries_the_reason_and_its_definition(reason: str) -> None:
    payload = cmf_refusal_payload(reason, "concrete detail", artifact_id="artifact_1")
    assert payload["reason"] == reason
    assert payload["category"] == CMF_REFUSAL_REASON_DEFINITIONS[reason]["category"]
    assert payload["message"] == "concrete detail"
    assert payload["details"] == {"artifact_id": "artifact_1"}


def test_unknown_reason_is_rejected_not_invented() -> None:
    with pytest.raises(ValueError, match="Unknown CMF refusal reason: cmf_made_up"):
        cmf_refusal_payload("cmf_made_up")


def test_refusal_exception_carries_the_typed_payload() -> None:
    error = CMFRefusal("cmf_server_unreachable", "connect timeout", server_url="http://x")
    assert error.reason == "cmf_server_unreachable"
    assert error.payload["category"] == "downstream_unavailable"
    assert error.payload["details"] == {"server_url": "http://x"}
    assert "cmf_server_unreachable" in str(error)


def test_refusal_exception_rejects_an_unknown_reason() -> None:
    with pytest.raises(ValueError):
        CMFRefusal("cmf_not_a_reason")


def test_projection_is_a_copy_callers_cannot_mutate_the_catalog() -> None:
    projection = cmf_refusal_reasons()
    projection["cmf_no_write_target"]["recovery_actions"].append("sabotage")
    assert (
        "sabotage" not in CMF_REFUSAL_REASON_DEFINITIONS["cmf_no_write_target"]["recovery_actions"]
    )
