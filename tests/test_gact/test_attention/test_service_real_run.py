"""End-to-end attention answer on a real recorded run (fixture from attention job 3237185)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from clio_agent.gact.attention.lm_calls import LmCall
from clio_agent.gact.attention.ranges import declare_ranges
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.service import SelectionRequest, explain_selection
from clio_agent.gact.attention.store import AttentionStore
from tests.test_gact.test_attention._support import (
    SID,
    FakeFlowcept,
    FixtureRenderer,
    descriptor_doc,
    fixture,
    fixture_flowcept,
    fixture_thought,
    fixture_transcript,
)

FIRST_SENTENCE = "We have registered the MTA1 time series CSV as an artifact."
PROMPT_TOKENS = 15514


def _call(**over: Any) -> LmCall:
    c = fixture()["lm_call"]
    fields = {
        "event_id": c["event_id"],
        "session_id": SID,
        "turn_id": c["turn_id"],
        "occurred_at": "2026-09-27T05:30:00Z",
        "model": c["model"],
        "response_id": c["response_id"],
        "messages": c["messages"],
        "content": c["content"],
        "declaration": None,
        "source": "flowcept",
    }
    fields.update(over)
    return LmCall(**fields)


def _explain(
    *,
    calls: list[LmCall] | None = None,
    flowcept: FakeFlowcept | None = None,
    messages: list[Any] | None = None,
    request: SelectionRequest | None = None,
) -> dict[str, Any]:
    renderer = FixtureRenderer()
    return explain_selection(
        messages=messages or fixture_transcript(),
        calls=calls if calls is not None else [_call()],
        store=AttentionStore(flowcept or fixture_flowcept()),
        renderer_for=lambda identity: renderer,
        request=request
        or SelectionRequest(
            message_id="msg_asst_1",
            part_id="call_sel",
            field="thought",
            start=0,
            end=len(FIRST_SENTENCE),
        ),
    )


def test_fixture_is_the_real_capture() -> None:
    """The template render reproduces the capture's prompt (length and offsets)."""
    data = fixture()
    renderer = FixtureRenderer()
    encoded = renderer.render_encoded(data["lm_call"]["messages"])
    assert len(encoded.ids) == data["descriptor"]["used"]["num_prompt_tokens"] == PROMPT_TOKENS
    assert encoded.offsets[-1][1] == len(encoded.text)
    assert fixture_thought().startswith(FIRST_SENTENCE)


def test_profiles_recompute_the_same_capture_without_changing_mean_mass() -> None:
    from clio_schemas.attention import DECAYED_MAX

    uniform = _explain()
    decayed = _explain(
        request=SelectionRequest(
            message_id="msg_asst_1",
            part_id="call_sel",
            field="thought",
            start=0,
            end=len(FIRST_SENTENCE),
            profile=DECAYED_MAX,
        )
    )
    assert decayed["profile_revision"] == DECAYED_MAX.revision
    assert decayed["request_id"] == uniform["request_id"]
    assert decayed["sources"] == uniform["sources"]
    assert decayed["residual"] == uniform["residual"]
    assert decayed["profile_weights"][0] > decayed["profile_weights"][1]
    assert decayed["blocks"]
    assert any(
        a["score"] != b["score"] for a, b in zip(uniform["blocks"], decayed["blocks"], strict=True)
    )
    assert all(0 <= block["intensity"] <= 1 for block in decayed["blocks"])
    assert all(len(block["content_revision"]) == 64 for block in decayed["blocks"])


def test_selection_maps_to_its_decode_steps_and_mass_is_bounded() -> None:
    result = _explain()
    sel = result["selection"]
    assert result["available"] is True
    assert sel["text"] == FIRST_SENTENCE
    # Output token t is decode row t (the step that produced it); 328 output
    # tokens + the stop row = the capture's 329 decode steps.
    assert sel["token_range"] == [133, 147]
    assert sel["steps"] == [133, 146] and sel["step_count"] == 14
    assert sel["output_tokens"] == 328 and sel["token_index_base"] == "produced"
    assert result["sections_source"] == "derived"
    # The fixture keeps only each row's top 64 entries, so the retained shares
    # plus the recorded residual fall short of 1 (the full file sums to 1).
    total = sum(s["share"] for s in result["sections"]) + result["residual"]
    assert 0.5 < total <= 1.0 + 1e-6
    assert result["residual"] == pytest.approx(0.7586, abs=1e-3)


def test_sources_breakdown_covers_the_owner_domains() -> None:
    result = _explain()
    domains = {s["domain"] for s in result["sources"]}
    assert {"system", "user", "tool_definitions", "thinking", "tool_call", "tool_result"} <= domains
    labels = {s["label"] for s in result["sections"] if s["domain"] == "tool_result"}
    assert labels == {"load_skill", "create_artifact"}


def test_transcript_blocks_are_located_with_heat_runs_inside_their_text() -> None:
    result = _explain()
    transcript = {(m.id, p.id): p for m in fixture_transcript() for p in m.parts}
    kinds = {b["kind"] for b in result["blocks"]}
    assert {"user_text", "assistant_text", "tool_result", "thought", "tool_input"} <= kinds
    for block in result["blocks"]:
        part = transcript[(block["message_id"], block["part_id"])]
        text = {
            "text": part.text,
            "thought": part.thought,
            "result": "\n".join(c.text for c in part.content),
            "input": json.dumps(part.input, ensure_ascii=False),
        }[block["field"]]
        for lo, hi, value in block["runs"]:
            assert 0 <= lo < hi <= len(text)
            assert value > 0
    # the selected output itself is never an anchor
    assert not any(b["part_id"] == "call_sel" and b["field"] == "thought" for b in result["blocks"])


def test_drilldown_lists_each_selected_token_with_its_top_positions() -> None:
    result = _explain()
    tokens = result["tokens"]
    assert [t["token_index"] for t in tokens] == list(range(133, 147))
    assert "".join(t["text"] for t in tokens) == FIRST_SENTENCE
    for token in tokens:
        assert 0 < len(token["top"]) <= 5
        assert all(0 < t["pos"] < PROMPT_TOKENS for t in token["top"])
        means = [t["mean"] for t in token["top"]]
        assert means == sorted(means, reverse=True)


def test_non_vllm_call_is_typed_provider_not_vllm() -> None:
    with pytest.raises(AttentionUnavailable) as exc:
        _explain(calls=[_call(model="claude_code/sonnet")])
    assert exc.value.reason == "provider_not_vllm"


def test_user_message_is_not_selectable() -> None:
    turn = fixture()["lm_call"]["turn_id"]
    with pytest.raises(AttentionUnavailable) as exc:
        _explain(request=SelectionRequest(message_id=turn))
    assert exc.value.reason == "message_not_generated"


def test_text_not_in_any_output_is_selection_not_located() -> None:
    messages = fixture_transcript()
    messages[-1].parts[-1].thought = "A thought the model never produced."
    with pytest.raises(AttentionUnavailable) as exc:
        _explain(messages=messages)
    assert exc.value.reason == "selection_not_located"


def test_turn_without_lm_calls_is_lm_call_not_found() -> None:
    with pytest.raises(AttentionUnavailable) as exc:
        _explain(calls=[_call(turn_id="msg_user_other")])
    assert exc.value.reason == "lm_call_not_found"


def test_capture_length_mismatch_is_range_alignment_mismatch() -> None:
    """A prompt the capture did not score (here: one message changed) is refused."""
    call = fixture()["lm_call"]
    messages = [dict(m) for m in call["messages"]]
    renderer = FixtureRenderer()
    encoded = renderer.render_encoded(call["messages"])

    class ShortRenderer(FixtureRenderer):
        def render_encoded(self, msgs, template_kwargs=None):  # type: ignore[no-untyped-def]
            return type(encoded)(
                text=encoded.text, ids=encoded.ids[:-1], offsets=encoded.offsets[:-1]
            )

    with pytest.raises(AttentionUnavailable) as exc:
        explain_selection(
            messages=fixture_transcript(),
            calls=[_call(messages=messages)],
            store=AttentionStore(fixture_flowcept()),
            renderer_for=lambda identity: ShortRenderer(),
            request=SelectionRequest(
                message_id="msg_asst_1", part_id="call_sel", field="thought", start=0, end=10
            ),
        )
    assert exc.value.reason == "range_alignment_mismatch"


def test_skipped_decode_steps_are_attention_capture_partial() -> None:
    doc = descriptor_doc(attention_stats={"decode_steps_unscored": 3})
    with pytest.raises(AttentionUnavailable) as exc:
        _explain(flowcept=FakeFlowcept([doc], [fixture()["workflow"]]))
    assert exc.value.reason == "attention_capture_partial"


def test_output_that_does_not_retokenize_to_the_step_count_is_refused() -> None:
    doc = descriptor_doc()
    doc["used"] = {**doc["used"], "num_decode_tokens": 331}
    with pytest.raises(AttentionUnavailable) as exc:
        _explain(flowcept=FakeFlowcept([doc], [fixture()["workflow"]]))
    # the record/file cross-check fires first: the file has 329 rows
    assert exc.value.reason == "attention_record_malformed"


def _declared_setup() -> dict[str, Any]:
    """A declaration whose ranges are the capture's segments (as vLLM would echo them)."""
    data = fixture()
    renderer = FixtureRenderer()
    encoded = renderer.render_encoded(data["lm_call"]["messages"])
    ranges = declare_ranges(data["lm_call"]["messages"], encoded).ranges
    return {
        "schema": "clio.attention.declaration.v1",
        "status": "declared",
        "tokenizer": "ibm-granite/granite-4.2-30b",
        "prompt_token_count": len(encoded.ids),
        "ranges": [r.to_record() for r in ranges],
    }


def test_declared_ranges_not_in_a_fixed_capture_are_range_alignment_mismatch() -> None:
    """The fixture was captured in fixed 128-token mode, so declared ranges were not used."""
    with pytest.raises(AttentionUnavailable) as exc:
        _explain(calls=[_call(declaration=_declared_setup())])
    assert exc.value.reason == "range_alignment_mismatch"


def test_rendered_selection_text_is_located_in_the_source() -> None:
    """The UI sends only the rendered text; the server finds its part and span."""
    result = _explain(
        request=SelectionRequest(
            message_id="msg_asst_1", text="the confirmed column names (time, east, north, up)"
        )
    )
    assert result["selection"]["part_id"] == "call_sel"
    assert result["selection"]["field"] == "thought"
    assert result["selection"]["text"] == "the confirmed column names (time, east, north, up)"


def test_rendered_selection_not_in_the_message_is_typed() -> None:
    with pytest.raises(AttentionUnavailable) as exc:
        _explain(request=SelectionRequest(message_id="msg_asst_1", text="never generated"))
    assert exc.value.reason == "selection_not_located"
