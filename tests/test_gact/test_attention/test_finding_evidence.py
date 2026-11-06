"""Reviewer findings preserve actual capture rows and parent ownership."""

from dataclasses import replace
from typing import Any

import pytest
from clio_schemas.attention import DECAYED_MAX

from clio_agent.gact.attention.finding_evidence import (
    FindingAttentionRequest,
    bind_finding_evidence,
)
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.store import AttentionStore
from tests.test_gact.test_attention._support import (
    SID,
    FixtureRenderer,
    fixture_flowcept,
    fixture_transcript,
)
from tests.test_gact.test_attention.test_lookup import _lookup, _ref
from tests.test_gact.test_attention.test_service_real_run import _call


def _receipt() -> FindingAttentionRequest:
    view = _lookup([_ref("call_sel", "thought", 0, 59)], profile=DECAYED_MAX)["views"][0]
    return FindingAttentionRequest(
        response_id=_call().response_id,
        capture_sha256=view["capture_sha256"],
        steps=view["selected_steps"],
        profile=DECAYED_MAX,
        profile_revision=DECAYED_MAX.revision,
        uncertainty="Attention strength alone does not establish poisoning.",
    )


def _bind(request: FindingAttentionRequest, **overrides: Any) -> Any:
    args = {
        "session_id": SID,
        "messages": fixture_transcript(),
        "calls": [_call()],
        "store": AttentionStore(fixture_flowcept()),
        "renderer_for": lambda _: FixtureRenderer(),
        "request": request,
    }
    args.update(overrides)
    return bind_finding_evidence(**args)


def test_finding_roundtrips_exact_profile_capture_and_unique_steps() -> None:
    request = _receipt()
    # Duplicate selection rows do not give the reviewer extra weight.
    inspection = _bind(request.model_copy(update={"steps": request.steps * 2}))
    assert inspection.profile == DECAYED_MAX
    assert inspection.capture_sha256 == request.capture_sha256
    assert inspection.lm_call_id == _call().event_id
    replay = _lookup(inspection.selections, profile=inspection.profile)["views"][0]
    assert replay["selected_steps"] == request.steps
    assert all(ref.session_id == SID for ref in inspection.selections)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"capture_sha256": "0" * 64}, "capture_changed"),
        ({"profile_revision": "0" * 64}, "profile_revision_changed"),
        ({"steps": [328]}, "content_coordinates_unavailable"),
        ({"response_id": "another-response"}, "lm_call_ambiguous"),
    ],
)
def test_changed_or_unmapped_receipts_are_never_substituted(
    change: dict[str, Any], reason: str
) -> None:
    with pytest.raises(AttentionUnavailable) as exc:
        _bind(_receipt().model_copy(update=change))
    assert exc.value.reason == reason


def test_other_session_call_and_ambiguous_response_are_refused() -> None:
    for calls in [[replace(_call(), session_id="other")], [_call(), _call()]]:
        with pytest.raises(AttentionUnavailable, match="no unique parent-session response"):
            _bind(_receipt(), calls=calls)


def test_absent_transcript_or_changed_content_has_no_invented_coordinates() -> None:
    with pytest.raises(AttentionUnavailable) as exc:
        _bind(_receipt(), messages=[])
    assert exc.value.reason == "content_coordinates_unavailable"
