"""Tests for the #891 turn time-budget waterfall analyzer.

Exercises the analyzer on small synthetic stream-audit fixtures in BOTH shapes:

* **new** — with the #891 ``provider.call_started`` / ``provider.call_usage``
  rows, so TTFT, tokens, cache metrics, and prefix-cache matching are all
  measurable and the wall-clock attribution is exact.
* **old** — a pre-#891 capture with only ``provider.raw_event`` + session-tagged
  rows, so the analyzer must degrade explicitly (naming every unavailable
  column) yet still produce the streaming-vs-gap waterfall.

The percentage attribution and gap distribution are asserted against
hand-computed values, so a regression in the join or arithmetic fails loudly.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.analyze_turn_waterfall import (
    analyze,
    available_sessions,
    build_calls,
    load_rows,
    main,
    report_to_dict,
)

_SID = "sess_test"
_TID = "turn_test"


def _new_shape_rows() -> list[dict]:
    """Two SDK calls with full #891 markers; call 2 reuses call 1's prefix."""
    return [
        # Call 1 --------------------------------------------------------------
        {
            "stage": "provider.call_started",
            "ts": 100.0,
            "session_id": _SID,
            "turn_id": _TID,
            "call_id": "c1",
            "call_index": 1,
            "model": "haiku",
            "transport": "sdk",
            "prompt_chars": 5000,
            "prefix_16k_sha256": "AAA",
        },
        {
            "stage": "provider.raw_event",
            "provider": "claude_code_sdk",
            "ts": 100.5,
            "call_index": 1,
            "source_channel": "provider_event",
            "text_len": 0,
            "thinking_len": 0,
        },
        {
            "stage": "provider.raw_event",
            "provider": "claude_code_sdk",
            "ts": 101.0,
            "call_index": 1,
            "source_channel": "text_delta",
            "text_len": 3,
            "thinking_len": 0,
        },
        {
            "stage": "provider.raw_event",
            "provider": "claude_code_sdk",
            "ts": 103.0,
            "call_index": 1,
            "source_channel": "text_delta",
            "text_len": 5,
            "thinking_len": 0,
        },
        {
            "stage": "provider.call_usage",
            "ts": 104.0,
            "session_id": _SID,
            "turn_id": _TID,
            "call_id": "c1",
            "call_index": 1,
            "model": "haiku",
            "transport": "sdk",
            "output_chars": 8,
            "usage_input_tokens": 200,
            "usage_output_tokens": 10,
            "usage_cache_read_input_tokens": 1000,
            "usage_cache_creation_input_tokens": 0,
        },
        # Call 2 --------------------------------------------------------------
        {
            "stage": "provider.call_started",
            "ts": 110.0,
            "session_id": _SID,
            "turn_id": _TID,
            "call_id": "c2",
            "call_index": 2,
            "model": "haiku",
            "transport": "sdk",
            "prompt_chars": 6000,
            "prefix_16k_sha256": "AAA",
        },
        {
            "stage": "provider.raw_event",
            "provider": "claude_code_sdk",
            "ts": 110.5,
            "call_index": 2,
            "source_channel": "provider_event",
            "text_len": 0,
            "thinking_len": 0,
        },
        {
            "stage": "provider.raw_event",
            "provider": "claude_code_sdk",
            "ts": 111.0,
            "call_index": 2,
            "source_channel": "text_delta",
            "text_len": 2,
            "thinking_len": 0,
        },
        {
            "stage": "provider.raw_event",
            "provider": "claude_code_sdk",
            "ts": 112.0,
            "call_index": 2,
            "source_channel": "text_delta",
            "text_len": 4,
            "thinking_len": 0,
        },
        {
            "stage": "provider.call_usage",
            "ts": 113.0,
            "session_id": _SID,
            "turn_id": _TID,
            "call_id": "c2",
            "call_index": 2,
            "model": "haiku",
            "transport": "sdk",
            "output_chars": 6,
            "usage_input_tokens": 240,
            "usage_output_tokens": 6,
            "usage_cache_read_input_tokens": 1200,
            "usage_cache_creation_input_tokens": 0,
        },
    ]


def _old_shape_rows() -> list[dict]:
    """Two SDK calls with only raw_event + session-tagged bracket/batch rows."""
    return [
        # Session-tagged rows bracket the turn (sse rows always do in reality),
        # so the window covers the untagged raw_events that precede each batch.
        {"stage": "sse.write", "ts": 199.9, "session_id": _SID, "turn_id": _TID},
        {
            "stage": "provider.raw_event",
            "provider": "claude_code_sdk",
            "ts": 200.0,
            "call_index": 1,
            "source_channel": "provider_event",
            "text_len": 0,
            "thinking_len": 0,
        },
        {
            "stage": "provider.raw_event",
            "provider": "claude_code_sdk",
            "ts": 200.2,
            "call_index": 1,
            "source_channel": "text_delta",
            "text_len": 3,
            "thinking_len": 0,
        },
        {
            "stage": "provider.raw_event",
            "provider": "claude_code_sdk",
            "ts": 200.4,
            "call_index": 1,
            "source_channel": "text_delta",
            "text_len": 5,
            "thinking_len": 0,
        },
        {
            "stage": "provider.batch_response",
            "provider": "dspy_lm",
            "ts": 200.5,
            "session_id": _SID,
            "turn_id": _TID,
            "model": "claude_code/cc-haiku",
        },
        {
            "stage": "provider.raw_event",
            "provider": "claude_code_sdk",
            "ts": 203.0,
            "call_index": 2,
            "source_channel": "provider_event",
            "text_len": 0,
            "thinking_len": 0,
        },
        {
            "stage": "provider.raw_event",
            "provider": "claude_code_sdk",
            "ts": 203.2,
            "call_index": 2,
            "source_channel": "text_delta",
            "text_len": 2,
            "thinking_len": 0,
        },
        {
            "stage": "provider.raw_event",
            "provider": "claude_code_sdk",
            "ts": 203.4,
            "call_index": 2,
            "source_channel": "text_delta",
            "text_len": 4,
            "thinking_len": 0,
        },
        {
            "stage": "provider.batch_response",
            "provider": "dspy_lm",
            "ts": 203.5,
            "session_id": _SID,
            "turn_id": _TID,
            "model": "claude_code/cc-haiku",
        },
        {"stage": "sse.write", "ts": 206.0, "session_id": _SID, "turn_id": _TID},
    ]


def test_new_shape_per_call_metrics_exact() -> None:
    report = analyze(_new_shape_rows(), _SID, _TID)
    assert report.capture_shape == "new"
    assert report.degrade_notes == []
    assert len(report.calls) == 2

    c1, c2 = report.calls
    assert c1.ttft_s == 1.0  # 101.0 - 100.0 submit->first content token
    assert c1.stream_span_s == 2.5  # 103.0 - 100.5
    assert c1.wall_s == 4.0  # 104.0 - 100.0 usage - started
    assert c1.tokens_per_s == 4.0  # 10 output tokens / 2.5 s span
    assert c1.output_tokens == 10
    assert c1.cache_read_tokens == 1000
    assert c1.prompt_chars == 5000
    assert c1.prefix_matches_prev is None  # first call has no predecessor

    # Call 2 reuses the identical 16 KB prefix fingerprint -> cache-warm prefix.
    assert c2.prefix_matches_prev is True
    assert c2.cache_read_tokens == 1200


def test_new_shape_wall_attribution_percentages() -> None:
    report = analyze(_new_shape_rows(), _SID, _TID)
    attr = report.attribution
    assert attr["turn_wall_s"] == 13.0  # 113.0 - 100.0
    buckets = attr["buckets_s"]
    # The three per-call slices PARTITION the wall (no first_raw->first_content
    # double count): ttft = begin->first content, streaming = first content->last
    # chunk, other = last chunk->usage. They must sum with the gap to the wall.
    assert buckets["lm_ttft_wait_s"] == 2.0  # (101-100) + (111-110)
    assert buckets["lm_streaming_s"] == 3.0  # (103-101) + (112-111)
    assert buckets["lm_other_s"] == 2.0  # (104-103) + (113-112)
    assert buckets["inter_call_gap_s"] == 6.0  # 110.0 - 104.0
    assert round(attr["residual_s"], 6) == 0.0  # exact partition, no residual
    assert round(attr["percent"]["inter_call_gap_s"], 2) == 46.15
    # Per call, ttft + streaming never exceeds the wall (finding 4 regression).
    for call in report.calls:
        assert call.ttft_s is not None and call.stream_content_span_s is not None
        assert call.ttft_s + call.stream_content_span_s <= call.wall_s + 1e-9


def test_new_shape_gap_distribution_vs_ttl() -> None:
    report = analyze(_new_shape_rows(), _SID, _TID)
    gaps = report.gap_distribution
    assert gaps["cache_ttl_s"] == 300.0
    assert gaps["grouping"] == "prefix_16k"
    assert gaps["overall"]["count"] == 1
    assert gaps["overall"]["max_s"] == 6.0
    assert gaps["overall"]["over_cache_ttl"] == 0
    # Both calls share the "AAA" 16 KB fingerprint -> one same-group gap.
    assert gaps["per_group"]["AAA"]["count"] == 1


def test_old_shape_degrades_but_still_attributes() -> None:
    report = analyze(_old_shape_rows(), _SID, _TID)
    assert report.capture_shape == "old"
    joined = " ".join(report.degrade_notes)
    assert "provider.call_usage rows absent" in joined
    assert "provider.call_started rows absent" in joined

    assert len(report.calls) == 2
    c1, c2 = report.calls
    # Token / TTFT columns are unavailable and reported as None, never 0.
    assert c1.ttft_s is None
    assert c1.output_tokens is None
    assert c1.tokens_per_s is None
    assert c1.prompt_chars is None
    # Streaming span is still measurable from raw_event timestamps.
    assert round(c1.stream_span_s, 6) == 0.4  # 200.4 - 200.0
    assert round(c1.wall_s, 6) == 0.4  # falls back to the raw-event span

    attr = report.attribution
    # Streaming partitions from the first CONTENT token (200.2), not first_raw.
    assert round(attr["buckets_s"]["lm_streaming_s"], 6) == 0.4  # (200.4-200.2)*2
    assert round(attr["buckets_s"]["inter_call_gap_s"], 6) == 2.6  # 203.0 - 200.4
    # TTFT and post-stream tail are unmeasurable here -> None (n/a), never a hard
    # 0 that reads as "measured zero wait" (finding 7).
    assert attr["buckets_s"]["lm_ttft_wait_s"] is None
    assert attr["buckets_s"]["lm_other_s"] is None
    assert attr["percent"]["lm_ttft_wait_s"] is None
    joined_notes = " ".join(report.degrade_notes)
    assert "lm_ttft_wait unavailable" in joined_notes
    assert "folds into" in joined_notes


def test_old_shape_no_bracket_uses_window_and_warns() -> None:
    # Whole-session (no --turn-id) on old shape must warn about window scoping.
    report = build_calls(_old_shape_rows(), _SID, turn_id=None)
    joined = " ".join(report.degrade_notes)
    assert "TIME WINDOW" in joined
    assert "no --turn-id given" in joined


def _marker_pair(call_id: str, call_index: int, start: float, fp: str) -> list[dict]:
    """A (call_started, call_usage) marker pair for a 1 s call with fingerprint ``fp``."""
    return [
        {
            "stage": "provider.call_started",
            "ts": start,
            "session_id": _SID,
            "turn_id": _TID,
            "call_id": call_id,
            "call_index": call_index,
            "model": "haiku",
            "transport": "sdk",
            "prompt_chars": 10,
            "prefix_16k_sha256": fp,
        },
        {
            "stage": "provider.call_usage",
            "ts": start + 1.0,
            "session_id": _SID,
            "turn_id": _TID,
            "call_id": call_id,
            "call_index": call_index,
            "model": "haiku",
            "transport": "sdk",
            "output_chars": 6,
            "usage_output_tokens": 3,
        },
    ]


def test_foreign_raw_event_with_colliding_call_index_is_windowed_out() -> None:
    # A daemon restart reuses call_index=1 in a shared file: a FOREIGN raw_event
    # (no session tag) collides on call_index but sits far outside this call's
    # time window. The window filter must exclude it, not corrupt TTFT/streaming.
    rows = [
        {
            "stage": "provider.raw_event",
            "provider": "claude_code_sdk",
            "ts": 100.0,  # foreign process, ~900 s before this session's call
            "call_index": 1,
            "source_channel": "text_delta",
            "text_len": 3,
            "thinking_len": 0,
        },
        {
            "stage": "provider.call_started",
            "ts": 1000.0,
            "session_id": _SID,
            "turn_id": _TID,
            "call_id": "cA",
            "call_index": 1,
            "model": "haiku",
            "transport": "sdk",
            "prompt_chars": 10,
            "prefix_16k_sha256": "AAA",
        },
        {
            "stage": "provider.raw_event",
            "provider": "claude_code_sdk",
            "ts": 1001.0,
            "call_index": 1,
            "source_channel": "text_delta",
            "text_len": 3,
            "thinking_len": 0,
        },
        {
            "stage": "provider.raw_event",
            "provider": "claude_code_sdk",
            "ts": 1002.0,
            "call_index": 1,
            "source_channel": "text_delta",
            "text_len": 3,
            "thinking_len": 0,
        },
        {
            "stage": "provider.call_usage",
            "ts": 1003.0,
            "session_id": _SID,
            "turn_id": _TID,
            "call_id": "cA",
            "call_index": 1,
            "model": "haiku",
            "transport": "sdk",
            "output_chars": 6,
            "usage_output_tokens": 3,
        },
        {
            "stage": "provider.batch_response",
            "provider": "dspy_lm",
            "ts": 1003.5,
            "session_id": _SID,
            "turn_id": _TID,
            "model": "claude_code/cc-haiku",
        },
    ]
    report = analyze(rows, _SID, _TID)
    assert len(report.calls) == 1
    call = report.calls[0]
    # Foreign ts=100 is gone; the call's own first chunk is 1001, never negative.
    assert call.first_raw_ts == 1001.0
    assert call.ttft_s == 1.0  # 1001 - 1000, NOT 100 - 1000 = -900
    assert round(call.stream_span_s, 6) == 1.0
    assert report.attribution["residual_s"] >= -1e-6
    # One batch_response, one joined call -> no contamination note.
    assert not any("SUSPECT" in n for n in report.degrade_notes)


def test_batch_response_count_mismatch_is_flagged() -> None:
    # Old-shape whole-session window pulls a foreign call_index into this session
    # (finding 6). The session's single batch_response row must expose the join
    # as contaminated instead of silently reporting two calls.
    rows = [
        {"stage": "sse.write", "ts": 10.0, "session_id": _SID, "turn_id": _TID},
        {
            "stage": "provider.raw_event",
            "provider": "claude_code_sdk",
            "ts": 11.0,
            "call_index": 1,
            "source_channel": "text_delta",
            "text_len": 3,
            "thinking_len": 0,
        },
        {
            "stage": "provider.raw_event",
            "provider": "claude_code_sdk",
            "ts": 12.0,
            "call_index": 1,
            "source_channel": "text_delta",
            "text_len": 3,
            "thinking_len": 0,
        },
        {
            "stage": "provider.batch_response",
            "provider": "dspy_lm",
            "ts": 12.5,
            "session_id": _SID,
            "turn_id": _TID,
            "model": "m",
        },
        {  # foreign call pulled in by the wide window (different call_index)
            "stage": "provider.raw_event",
            "provider": "claude_code_sdk",
            "ts": 20.0,
            "call_index": 2,
            "source_channel": "text_delta",
            "text_len": 3,
            "thinking_len": 0,
        },
        {
            "stage": "provider.raw_event",
            "provider": "claude_code_sdk",
            "ts": 21.0,
            "call_index": 2,
            "source_channel": "text_delta",
            "text_len": 3,
            "thinking_len": 0,
        },
        {"stage": "sse.write", "ts": 30.0, "session_id": _SID, "turn_id": _TID},
    ]
    report = analyze(rows, _SID, _TID)
    assert len(report.calls) == 2  # the join over-collected
    joined = " ".join(report.degrade_notes)
    assert "provider.batch_response" in joined
    assert "SUSPECT" in joined


def test_reused_call_index_keeps_both_calls() -> None:
    # A daemon restart mid-session reuses call_index=1 for two DIFFERENT call_ids.
    # Keying by call_id keeps both; keying by call_index (the first cut) dropped
    # one and mis-sized the turn wall.
    rows = [*_marker_pair("cA", 1, 100.0, "AAA"), *_marker_pair("cB", 1, 500.0, "BBB")]
    report = analyze(rows, _SID, _TID)
    assert len(report.calls) == 2
    assert {c.call_id for c in report.calls} == {"cA", "cB"}
    # cA=[100,101], cB=[500,501]; wall spans both (501-100), not one call's 1 s.
    assert report.attribution["turn_wall_s"] == 401.0
    assert any("reused" in n for n in report.degrade_notes)


def test_prefix_match_spans_interleaved_delegation() -> None:
    # main(M) -> child(C) -> main(M): the second main call reuses main's prefix,
    # cache-warm (gap < TTL), even though the IMMEDIATELY previous call is the
    # child with a different fingerprint. Adjacency-only comparison reads False.
    rows = [
        *_marker_pair("m1", 1, 0.0, "M"),
        *_marker_pair("c1", 2, 10.0, "C"),
        *_marker_pair("m2", 3, 20.0, "M"),
    ]
    report = analyze(rows, _SID, _TID)
    by_id = {c.call_id: c for c in report.calls}
    assert by_id["m1"].prefix_matches_prev is None  # first fingerprinted call
    assert by_id["c1"].prefix_matches_prev is False  # no earlier C prefix
    assert by_id["m2"].prefix_matches_prev is True  # warm reuse of main's prefix


def test_gap_distribution_isolates_same_agent_cold_cache() -> None:
    # main(M) at t=0, a dense child(C) delegation fills t=10..590 with small
    # adjacent gaps, then main(M) returns at t=610. Main's OWN cache sat idle
    # ~609 s (cold), but every adjacent-call gap is tiny. Fingerprint grouping
    # must count main's cold gap; the adjacency 'overall' must not (finding 5).
    rows = [*_marker_pair("m1", 1, 0.0, "M")]
    ci = 2
    t = 10.0
    while t <= 590.0:
        rows += _marker_pair(f"c{ci}", ci, t, "C")
        ci += 1
        t += 10.0
    rows += _marker_pair("m2", ci, 610.0, "M")

    report = analyze(rows, _SID, _TID)
    gaps = report.gap_distribution
    assert gaps["grouping"] == "prefix_16k"
    assert gaps["per_group"]["M"]["over_cache_ttl"] == 1  # main's cache went cold
    assert gaps["overall"]["over_cache_ttl"] == 0  # adjacency alone exonerates it


def test_negative_residual_is_flagged() -> None:
    # Overlapping timestamps (usage before the last streamed chunk on one call)
    # push segments past the wall; the residual goes negative and must be noted.
    rows = [
        {
            "stage": "provider.call_started",
            "ts": 0.0,
            "session_id": _SID,
            "turn_id": _TID,
            "call_id": "x1",
            "call_index": 1,
            "model": "haiku",
            "transport": "sdk",
            "prompt_chars": 10,
            "prefix_16k_sha256": "AAA",
        },
        {
            "stage": "provider.raw_event",
            "provider": "claude_code_sdk",
            "ts": 1.0,
            "call_index": 1,
            "source_channel": "text_delta",
            "text_len": 3,
            "thinking_len": 0,
        },
        {
            "stage": "provider.raw_event",
            "provider": "claude_code_sdk",
            "ts": 100.0,  # last chunk far after the (earlier) usage row
            "call_index": 1,
            "source_channel": "text_delta",
            "text_len": 3,
            "thinking_len": 0,
        },
        {
            "stage": "provider.call_usage",
            "ts": 5.0,  # usage BEFORE the last raw chunk -> inconsistent
            "session_id": _SID,
            "turn_id": _TID,
            "call_id": "x1",
            "call_index": 1,
            "model": "haiku",
            "transport": "sdk",
            "output_chars": 6,
            "usage_output_tokens": 3,
        },
        {  # widens the window past the usage row so the ts=100 chunk is in scope
            "stage": "sse.write",
            "ts": 200.0,
            "session_id": _SID,
            "turn_id": _TID,
        },
    ]
    report = analyze(rows, _SID, _TID)
    assert report.attribution["residual_s"] < 0
    assert any("residual is negative" in n for n in report.degrade_notes)


def test_load_rows_skips_blank_and_malformed(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text(
        '{"stage": "a", "session_id": "s"}\n\n'  # valid + blank
        "not json at all\n"
        '{"stage": "b", "session_id": "s"}\n',
        encoding="utf-8",
    )
    rows = load_rows(path)
    assert [r["stage"] for r in rows] == ["a", "b"]
    assert available_sessions(rows) == ["s"]


def test_missing_session_reports_error(tmp_path: Path, capsys) -> None:
    path = tmp_path / "audit.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for row in _new_shape_rows():
            handle.write(json.dumps(row) + "\n")

    code = main(["--audit", str(path), "--session-id", "sess_absent"])
    assert code == 2
    err = capsys.readouterr().err
    assert "no rows for session_id" in err
    assert _SID in err  # lists the available session


def test_main_writes_json_report(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    with audit.open("w", encoding="utf-8") as handle:
        for row in _new_shape_rows():
            handle.write(json.dumps(row) + "\n")
    out = tmp_path / "report.json"

    code = main(
        ["--audit", str(audit), "--session-id", _SID, "--turn-id", _TID, "--json-out", str(out)]
    )
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["capture_shape"] == "new"
    assert payload["turn_id"] == _TID
    assert len(payload["calls"]) == 2
    assert payload["attribution"]["turn_wall_s"] == 13.0
    # round-trips through the public serializer
    assert report_to_dict(analyze(_new_shape_rows(), _SID, _TID)) == payload
