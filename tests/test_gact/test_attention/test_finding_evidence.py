"""Reviewer findings preserve actual capture rows and parent ownership."""

from dataclasses import replace
from pathlib import Path
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


@pytest.mark.parametrize("valid", [True, False])
def test_native_alert_emits_verified_inspection_or_disabled_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, valid: bool
) -> None:
    from fastapi.testclient import TestClient

    from clio_agent.gact.action_cards import build_raise_alert_card_tool
    from clio_agent.gact.app import build_app
    from clio_agent.gact.attention import routes
    from clio_agent.gact.types import AgentDef
    from tests.test_gact.test_action_cards import _call_tool_as, _register_fake_child_task

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        task = _register_fake_child_task(app, parent_sid=parent, blueprint_id="spotter-ai")
        messages = fixture_transcript()
        for message in messages:
            message.session_id = parent
        app.state.messages[parent] = messages
        monkeypatch.setattr(
            routes, "session_lm_calls", lambda app, sid: [replace(_call(), session_id=parent)]
        )
        monkeypatch.setattr(
            routes, "attention_store", lambda app: AttentionStore(fixture_flowcept())
        )
        monkeypatch.setattr(routes, "_renderer", lambda identity: FixtureRenderer())
        receipt = _receipt().model_dump()
        if not valid:
            receipt["capture_sha256"] = "0" * 64
        result = _call_tool_as(
            app,
            task.child_session_id,
            build_raise_alert_card_tool(AgentDef(id="spotter_watcher", title="SPOTTER reviewer")),
            title="Review this result",
            body="Observed evidence, not a poisoning verdict.",
            attention_evidence=receipt,
        )
        assert result["emitted"] is True
        assert result["attention_evidence"]["available"] is valid
        wire = client.get(f"/v1/sessions/{parent}/messages").json()["messages"]
        card = next(
            part for message in wire for part in message["parts"] if part["type"] == "action_card"
        )
        assert "Uncertainty:" in card["body"]
        assert card["actions"][0]["enabled"] is valid
        assert card["actions"][1]["behavior"]["handle_id"] == task.task_id
        if valid:
            from clio_agent.gact.protocol.v3.message import _action_cards

            projected = _action_cards(card)
            assert (
                projected[0]["behavior"]["inspection"]
                == card["actions"][0]["behavior"]["inspection"]
            )
            assert all(
                ref["session_id"] == parent
                for ref in card["actions"][0]["behavior"]["inspection"]["selections"]
            )
        else:
            assert card["actions"][0]["behavior"]["reason"]
