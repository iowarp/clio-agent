"""Both lookup directions use real capture rows and never double-count selection unions."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from clio_schemas.attention import DECAYED_MAX, UNIFORM_MEAN, AttentionProfile
from clio_schemas.connected_resources import ContentSelection

from clio_agent.gact.attention.lookup import lookup_attention
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.store import AttentionStore
from clio_agent.gact.attention.transcript_map import transcript_texts
from tests.test_gact.test_attention._support import (
    SID,
    FixtureRenderer,
    fixture_flowcept,
    fixture_transcript,
)
from tests.test_gact.test_attention.test_service_real_run import _call


def _ref(part_id: str, field: str, start: int = 0, end: int | None = None) -> ContentSelection:
    text = next(
        t
        for t in transcript_texts(fixture_transcript())
        if t.part_id == part_id and t.field == field
    )
    return ContentSelection.model_validate(
        {
            "session_id": SID,
            "message_id": text.message_id,
            "part_id": text.part_id,
            "field": text.field,
            "content_revision": text.content_revision,
            "selection": {"kind": "text", "start": start, "end": end or len(text.text)},
        }
    )


def _lookup(
    refs: list[ContentSelection],
    *,
    reverse: bool = False,
    profile: AttentionProfile = UNIFORM_MEAN,
    **kwargs: Any,
) -> dict[str, Any]:
    return lookup_attention(
        session_id=SID,
        messages=kwargs.pop("messages", fixture_transcript()),
        calls=kwargs.pop("calls", [_call()]),
        store=AttentionStore(fixture_flowcept()),
        renderer_for=lambda identity: FixtureRenderer(),
        selections=refs,
        direction="source_to_generation" if reverse else "generated_to_source",
        profile=profile,
        **kwargs,
    )


@pytest.mark.parametrize("profile", [UNIFORM_MEAN, DECAYED_MAX])
def test_overlapping_generated_selections_reduce_unique_steps(profile: AttentionProfile) -> None:
    whole = _ref("call_sel", "thought", 0, 59)
    overlap = _ref("call_sel", "thought", 8, 40)
    one = _lookup([whole], profile=profile)["views"][0]
    many = _lookup([whole, overlap, whole], profile=profile)
    assert many["selection_count"] == 2
    view = many["views"][0]
    assert view["selected_steps"] == one["selected_steps"]
    assert view["sources"] == one["sources"]
    assert view["blocks"] == one["blocks"]
    assert view["profile_weights"] == one["profile_weights"]
    assert view["selected_references"] == [whole.model_dump(), overlap.model_dump()]


def test_reverse_lookup_deduplicates_source_positions_and_keeps_profiles_inspectable() -> None:
    # u0 is present exactly once. The earlier user text is transformed in the
    # captured history and intentionally has no inferred coordinate mapping.
    source = next(t for t in transcript_texts(fixture_transcript()) if t.part_id == "u0")
    whole = _ref(source.part_id, source.field)
    overlap = _ref(source.part_id, source.field, 4, 20)
    first_result = _lookup([whole], reverse=True)
    assert first_result["views"], first_result
    one = first_result["views"][0]
    many = _lookup([whole, whole, overlap], reverse=True)["views"][0]
    assert one["prompt_positions"] == many["prompt_positions"]
    assert one["mass"] == many["mass"]
    assert one["score"] == many["score"]
    assert one["step_count"] == 328
    assert one["omitted_tokens"] == 200
    assert all(0 <= t["intensity"] <= 1 for t in one["tokens"])
    decayed = _lookup([whole], reverse=True, profile=DECAYED_MAX)["views"][0]
    assert decayed["mass"] == one["mass"]
    assert decayed["profile_revision"] == DECAYED_MAX.revision
    assert decayed["capture_sha256"] == one["capture_sha256"]
    assert one["heat"]["blocks"]
    for block in one["heat"]["blocks"]:
        source_text = block["source_text"]
        assert all(0 <= lo < hi <= len(source_text) for lo, hi, _ in block["display_runs"])
        assert all(0 <= intensity <= 1 for _, _, intensity in block["display_runs"])
    assert one["heat"]["profile_revision"] == UNIFORM_MEAN.revision


def test_unavailable_coordinates_stale_revision_and_cross_session_are_explicit() -> None:
    ref = _ref("call_sel", "thought")
    invalid = [
        ref.model_copy(update={"content_revision": "stale"}),
        ref.model_copy(update={"session_id": "another-session"}),
        ContentSelection.model_validate(
            {
                **ref.model_dump(),
                "selection": {
                    "kind": "image_region",
                    "x": 0.0,
                    "y": 0.0,
                    "width": 0.5,
                    "height": 0.5,
                },
            }
        ),
    ]
    result = _lookup(invalid)
    assert result["views"] == []
    assert {item["reason"] for item in result["unavailable"]} == {
        "content_revision_changed",
        "message_not_found",
        "content_coordinates_unavailable",
    }


def test_reverse_lookup_pages_calls_and_excludes_content_created_later() -> None:
    ref = _ref("call_sel", "thought")
    result = _lookup(
        [ref], reverse=True, calls=[_call(), replace(_call(), event_id="later")], limit=1
    )
    assert result["views"] == []  # the output cannot be its own prompt source
    assert result["next_cursor"] == 1
    assert _lookup([ref], reverse=True, cursor=1)["next_cursor"] is None


def test_evidence_lookup_is_bound_to_the_exact_model_call() -> None:
    source = _ref("u0", "text")
    result = _lookup(
        [source],
        reverse=True,
        calls=[_call(), replace(_call(), event_id="later")],
        lm_call_id="later",
    )
    assert [view["lm_call_id"] for view in result["views"]] == ["later"]
    with pytest.raises(AttentionUnavailable, match="referenced model call"):
        _lookup([source], reverse=True, lm_call_id="another-session-call")
