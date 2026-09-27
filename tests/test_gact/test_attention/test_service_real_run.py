"""End-to-end attention answer on a real recorded run (fixture from Flowcept job 3188205)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from clio_agent.gact.attention.contract import parse_summary
from clio_agent.gact.attention.lm_calls import LmCall
from clio_agent.gact.attention.ranges import declare_ranges
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.service import SelectionRequest, explain_selection
from clio_agent.gact.attention.store import AttentionStore
from tests.test_gact.test_attention._support import (
    SID,
    FakeFlowcept,
    FixtureRenderer,
    fixture,
    fixture_flowcept,
    fixture_thought,
    fixture_transcript,
    step_docs,
    summary_doc,
)

FIRST_SENTENCE = "We have the EarthScope stations dataset URL."


def _call(**over: Any) -> LmCall:
    c = fixture()["lm_call"]
    fields = {
        "event_id": c["event_id"],
        "session_id": SID,
        "turn_id": c["turn_id"],
        "occurred_at": "2026-09-21T18:40:00Z",
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
    assert len(encoded.ids) == data["summary"]["num_prompt_tokens"] == 13296
    assert encoded.offsets[-1][1] == len(encoded.text)
    assert fixture_thought().startswith(FIRST_SENTENCE)


def test_selection_maps_to_its_decode_steps_and_mass_is_conserved() -> None:
    result = _explain()
    sel = result["selection"]
    assert result["available"] is True
    assert sel["text"] == FIRST_SENTENCE
    assert sel["token_range"] == [262, 271]
    assert sel["steps"] == [262, 270] and sel["step_count"] == 9
    assert sel["aligned_fraction"] == 1.0
    assert result["sections_source"] == "derived"
    total = sum(s["share"] for s in result["sections"]) + result["residual"]
    assert total == pytest.approx(1.0, abs=2e-3)
    rows = fixture()["steps"]
    expected_residual = sum(rows[str(s)]["residual"] for s in range(262, 271)) / 9
    assert result["residual"] == pytest.approx(expected_residual, rel=1e-6)


def test_sources_breakdown_covers_the_owner_domains() -> None:
    result = _explain()
    domains = {s["domain"] for s in result["sources"]}
    assert {"system", "user", "tool_definitions", "thinking", "tool_call", "tool_result"} <= domains
    labels = {s["label"] for s in result["sections"] if s["domain"] == "tool_result"}
    assert labels == {"load_skill", "ndp_search_datasets"}


def test_transcript_blocks_are_located_with_heat_runs_inside_their_text() -> None:
    result = _explain()
    transcript = {(m.id, p.id): p for m in fixture_transcript() for p in m.parts}
    kinds = {b["kind"] for b in result["blocks"]}
    assert {"user_text", "tool_result", "thought"} <= kinds
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
    assert [t["token_index"] for t in tokens] == list(range(262, 271))
    assert "".join(t["text"] for t in tokens).strip().startswith("We have the EarthScope")
    for token in tokens:
        assert 0 < len(token["top"]) <= 5
        assert all(0 < t["pos"] < 13296 for t in token["top"])


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
    messages[1].parts[-1].thought = "A thought the model never produced."
    with pytest.raises(AttentionUnavailable) as exc:
        _explain(messages=messages)
    assert exc.value.reason == "selection_not_located"


def test_turn_without_lm_calls_is_lm_call_not_found() -> None:
    with pytest.raises(AttentionUnavailable) as exc:
        _explain(calls=[_call(turn_id="msg_user_other")])
    assert exc.value.reason == "lm_call_not_found"


def test_capture_length_mismatch_is_range_alignment_mismatch() -> None:
    doc = summary_doc()
    doc["used"]["num_prompt_tokens"] = 13000
    doc["generated"]["attention_summary"]["segments"][-1][1] = 13000
    doc["generated"]["attention_summary"].pop("prompt_token_ids")
    doc["generated"]["attention_summary"].pop("attn_sum")
    store = FakeFlowcept([doc, *step_docs()], [fixture()["workflow"]])
    segs = doc["generated"]["attention_summary"]["segments"]
    while segs and segs[-1][0] >= 13000:
        segs.pop()
        doc["generated"]["attention_summary"]["segment_mean_mass"].pop()
    segs[-1][1] = 13000
    with pytest.raises(AttentionUnavailable) as exc:
        _explain(flowcept=store)
    assert exc.value.reason == "range_alignment_mismatch"


def _declared_setup() -> tuple[dict[str, Any], FakeFlowcept]:
    """A capture whose segments are CLIO's declared ranges (+ gaps), as vLLM would emit."""
    data = fixture()
    renderer = FixtureRenderer()
    encoded = renderer.render_encoded(data["lm_call"]["messages"])
    ranges = declare_ranges(data["lm_call"]["messages"], encoded).ranges
    segments: list[list[int]] = []
    cursor = 0
    for r in ranges:
        if r.lo > cursor:
            segments.append([cursor, r.lo])
        segments.append([r.lo, r.hi])
        cursor = r.hi
    if cursor < len(encoded.ids):
        segments.append([cursor, len(encoded.ids)])
    attn = data["summary"]["attn_sum"]
    g = data["summary"]["num_decode_tokens"]
    doc = summary_doc(
        summary={
            "segments": segments,
            "segment_mean_mass": [sum(attn[lo:hi]) / g for lo, hi in segments],
            "segment_mode": "variable",
        }
    )
    declaration = {
        "schema": "clio.attention.declaration.v1",
        "status": "declared",
        "tokenizer": "ibm-granite/granite-4.2-30b",
        "prompt_token_count": len(encoded.ids),
        "ranges": [r.to_record() for r in ranges],
    }
    return declaration, FakeFlowcept([doc, *step_docs()], [data["workflow"]])


def test_declared_ranges_are_used_verbatim() -> None:
    declaration, store = _declared_setup()
    result = _explain(calls=[_call(declaration=declaration)], flowcept=store)
    assert result["sections_source"] == "declared"
    declared = {(r["lo"], r["hi"]) for r in declaration["ranges"]}
    assert declared <= {(s["lo"], s["hi"]) for s in result["sections"]}


def test_declared_range_missing_from_capture_is_range_alignment_mismatch() -> None:
    declaration, store = _declared_setup()
    declaration["ranges"][2]["hi"] -= 1
    with pytest.raises(AttentionUnavailable) as exc:
        _explain(calls=[_call(declaration=declaration)], flowcept=store)
    assert exc.value.reason == "range_alignment_mismatch"


def test_summary_parses_real_values() -> None:
    summary = parse_summary(summary_doc())
    assert summary.prompt_tokens == 13296 and summary.decode_steps == 435
    assert summary.segments[0] == (0, 128) and summary.top_pct == 10.0
    assert summary.attn_sum is not None and summary.attn_sum.shape == (13296,)
