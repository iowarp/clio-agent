#!/usr/bin/env python3
"""Turn time-budget waterfall analyzer for CLIO stream-audit captures (#891).

Consumes a ``CLIO_STREAM_AUDIT_LOG`` JSONL capture and decomposes one GACT
session's wall-clock into measured buckets, attributing a percentage of the
turn to each:

* **lm_ttft_wait** — per call, submit → first content token (needs the #891
  ``provider.call_started`` marker).
* **lm_streaming** — per call, first → last streamed chunk (from
  ``provider.raw_event`` timestamps, always available for the SDK path).
* **inter_call_gap** — between consecutive calls: orchestration, context
  compilation, ARC writes, tool round-trips — everything that is NOT the model
  generating tokens.

It also reports, per call: TTFT, duration, output tokens/sec, prompt chars,
cache read/creation tokens, and whether a cache-warm earlier call shares the
call's 16 KB prompt-prefix fingerprint within the TTL (the necessary condition
for a prompt-cache read); and, grouped by that fingerprint (a stand-in for
"same agent/prefix"), the same-group inter-call gap distribution against the
300 s prompt-cache TTL.

Degradation is explicit: a capture that predates the #891 usage/marker rows
still yields the streaming-vs-gap waterfall, and every column that cannot be
computed is listed with the reason. It never silently drops a column.

Join model
----------
``provider.call_started`` / ``provider.call_usage`` carry ``call_id`` (pairs a
call's two rows across processes) and ``call_index`` (process-local monotonic).
``provider.raw_event`` rows carry only ``call_index`` — so TTFT/streaming are
joined to a call by ``call_index``, which is why cross-process captures with a
reset ``call_index`` are disambiguated by the session's time window (reported as
a degradation when it happens).

Usage::

    uv run python scripts/analyze_turn_waterfall.py \\
        --audit gate1_stream_audit.jsonl --session-id sess_f81710c00d95 \\
        --json-out waterfall.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The Anthropic prompt-cache time-to-live: a cache entry survives ~5 minutes of
# inactivity. An inter-call gap longer than this means the next call cannot read
# the previous call's cache and re-pays cache-creation input tokens.
CACHE_TTL_S = 300.0

# raw_event source channels that carry a genuine model-emitted token (as opposed
# to a provider control event such as message_start / content_block_start).
_CONTENT_CHANNELS = frozenset({"text_delta", "thinking_delta"})


@dataclass
class CallRecord:
    """Per-LM-call timing and token facts joined from the audit rows.

    ``None`` on any field means "not measurable from this capture" — never zero
    as a stand-in. The rendering layer prints ``n/a`` for such fields.
    """

    call_index: int
    call_id: str | None = None
    model: str | None = None
    transport: str | None = None
    started_ts: float | None = None
    first_raw_ts: float | None = None
    first_content_ts: float | None = None
    last_raw_ts: float | None = None
    usage_ts: float | None = None
    raw_event_count: int = 0
    prompt_chars: int | None = None
    prefix_16k: str | None = None
    output_chars: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    prefix_matches_prev: bool | None = None

    @property
    def begin_ts(self) -> float | None:
        """Best wall-clock start: the submit marker, else the first chunk."""
        return self.started_ts if self.started_ts is not None else self.first_raw_ts

    @property
    def end_ts(self) -> float | None:
        """Best wall-clock end: the usage row, else the last chunk."""
        return self.usage_ts if self.usage_ts is not None else self.last_raw_ts

    @property
    def ttft_s(self) -> float | None:
        """Submit → first content token, or ``None`` without a submit marker."""
        if self.started_ts is None or self.first_content_ts is None:
            return None
        return self.first_content_ts - self.started_ts

    @property
    def stream_span_s(self) -> float | None:
        """First → last streamed chunk, or ``None`` without >=2 raw events."""
        if self.first_raw_ts is None or self.last_raw_ts is None:
            return None
        if self.last_raw_ts <= self.first_raw_ts:
            return 0.0
        return self.last_raw_ts - self.first_raw_ts

    @property
    def stream_content_span_s(self) -> float | None:
        """First *content* token → last chunk — the streaming slice used by the
        wall-clock partition. It starts at the first genuine token (not the first
        control event) so it does NOT overlap :attr:`ttft_s`, which ends there.
        """
        start = self.first_content_ts if self.first_content_ts is not None else self.first_raw_ts
        if start is None or self.last_raw_ts is None:
            return None
        if self.last_raw_ts <= start:
            return 0.0
        return self.last_raw_ts - start

    @property
    def tail_s(self) -> float | None:
        """Last streamed chunk → usage row (post-stream result wall), or ``None``
        without the usage end marker. Clamped at 0 (usage never precedes stream).
        """
        if self.usage_ts is None or self.last_raw_ts is None:
            return None
        return max(0.0, self.usage_ts - self.last_raw_ts)

    @property
    def wall_s(self) -> float | None:
        """Submit → usage duration, or the raw-event span as a fallback."""
        if self.started_ts is not None and self.usage_ts is not None:
            return self.usage_ts - self.started_ts
        return self.stream_span_s

    @property
    def tokens_per_s(self) -> float | None:
        """Output tokens / streaming span, when both are known and positive."""
        span = self.stream_span_s
        if self.output_tokens is None or span is None or span <= 0:
            return None
        return self.output_tokens / span


@dataclass
class WaterfallReport:
    """The full machine-readable result of a waterfall analysis."""

    session_id: str
    capture_shape: str
    turn_id: str | None = None
    degrade_notes: list[str] = field(default_factory=list)
    calls: list[CallRecord] = field(default_factory=list)
    attribution: dict[str, Any] = field(default_factory=dict)
    gap_distribution: dict[str, Any] = field(default_factory=dict)


def load_rows(path: Path) -> list[dict[str, Any]]:
    """Load a stream-audit JSONL file, skipping blank/malformed lines.

    Args:
        path: Path to the ``CLIO_STREAM_AUDIT_LOG`` JSONL capture.

    Returns:
        The parsed rows in file order.
    """
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def available_sessions(rows: list[dict[str, Any]]) -> list[str]:
    """Return the distinct non-empty ``session_id`` values present in ``rows``."""
    seen = {str(row["session_id"]) for row in rows if row.get("session_id") not in (None, "")}
    return sorted(seen)


def _scoped_raw_events(
    rows: list[dict[str, Any]],
    *,
    call_indexes: set[int] | None,
    window: tuple[float, float],
) -> tuple[dict[int, list[dict[str, Any]]], bool]:
    """Group SDK ``provider.raw_event`` rows by ``call_index`` for the session.

    ``provider.raw_event`` rows carry only ``call_index`` (no session/turn tag),
    so the session's time ``window`` is ALWAYS applied — otherwise a ``call_index``
    that resets across a daemon restart in a shared audit file would pull another
    process's raw events into this session (silent cross-session corruption).
    New captures additionally constrain to the session's ``call_index`` set
    (precise); old captures have no such set and lean on the window alone
    (approximate). The second return value flags the window-only path.

    Args:
        rows: All parsed audit rows.
        call_indexes: The session's call-index set, or ``None`` for old captures.
        window: ``(lo, hi)`` session timestamp bounds, always applied.

    Returns:
        A ``(by_call_index, used_window_fallback)`` pair.
    """
    lo, hi = window
    by_call: dict[int, list[dict[str, Any]]] = {}
    used_window = call_indexes is None
    for row in rows:
        if row.get("stage") != "provider.raw_event":
            continue
        if row.get("provider") != "claude_code_sdk":
            continue
        ci = row.get("call_index")
        if not isinstance(ci, int):
            continue
        if not (lo <= float(row.get("ts", 0.0)) <= hi):
            continue
        if call_indexes is not None and ci not in call_indexes:
            continue
        by_call.setdefault(ci, []).append(row)
    for events in by_call.values():
        events.sort(key=lambda r: float(r.get("ts", 0.0)))
    return by_call, used_window


def _marker_rows_by_call_id(
    session_rows: list[dict[str, Any]], stage: str
) -> tuple[dict[str, dict[str, Any]], set[int]]:
    """Index one marker stage's rows by ``call_id`` and flag reused indexes.

    ``call_id`` (not ``call_index``) is the call identity: it is unique per LM
    call, whereas ``call_index`` is a process-local counter that RESETS across a
    daemon restart appending to the same audit file. Keying by ``call_index``
    (as the first cut did) silently drops a call when the index repeats; keying
    by ``call_id`` keeps both.

    Args:
        session_rows: Rows already filtered to the session (and turn).
        stage: The marker stage to index (``provider.call_started`` or
            ``provider.call_usage``).

    Returns:
        A ``(rows_by_call_id, reused_indexes)`` pair. Rows without a ``call_id``
        are keyed by a synthetic ``idx:<call_index>``. ``reused_indexes`` holds
        every ``call_index`` seen under more than one ``call_id`` — the calls for
        which the ``call_index``-only raw_event join is ambiguous.
    """
    by_id: dict[str, dict[str, Any]] = {}
    index_owner: dict[int, str] = {}
    reused: set[int] = set()
    for row in session_rows:
        if row.get("stage") != stage or not isinstance(row.get("call_index"), int):
            continue
        ci = int(row["call_index"])
        cid = str(row.get("call_id") or f"idx:{ci}")
        by_id[cid] = row
        owner = index_owner.get(ci)
        if owner is not None and owner != cid:
            reused.add(ci)
        index_owner[ci] = cid
    return by_id, reused


def build_calls(
    rows: list[dict[str, Any]], session_id: str, turn_id: str | None = None
) -> WaterfallReport:
    """Join audit rows into per-call records for one session (optionally one turn).

    Scoping by ``turn_id`` tightens the session's time window to a single turn,
    which matters for OLD captures: raw_event rows carry no turn/session tag, so
    they are scoped by that window — a whole multi-turn session's window spans
    idle gaps and neighbouring sessions, while a single turn's window does not.

    Args:
        rows: All parsed audit rows (multiple sessions may be interleaved).
        session_id: The GACT session to analyze.
        turn_id: Optional turn to restrict to; ``None`` analyzes the whole
            session (reported with a multi-turn caveat).

    Returns:
        A :class:`WaterfallReport` with ``calls`` populated and ``capture_shape``
        / ``degrade_notes`` describing what was and was not measurable. The
        ``attribution`` and ``gap_distribution`` fields are filled by
        :func:`attribute_wall` and :func:`gap_distribution`.
    """
    session_rows = [r for r in rows if str(r.get("session_id") or "") == session_id]
    if turn_id is not None:
        session_rows = [r for r in session_rows if str(r.get("turn_id") or "") == turn_id]
    report = WaterfallReport(session_id=session_id, capture_shape="unknown")
    report.turn_id = turn_id
    if not session_rows:
        scope = f"session_id={session_id!r}" + (f", turn_id={turn_id!r}" if turn_id else "")
        report.degrade_notes.append(f"no rows carry {scope}; nothing to analyze")
        return report
    if turn_id is None:
        report.degrade_notes.append(
            "no --turn-id given: analyzing the whole session. On OLD captures a "
            "multi-turn session's time window spans idle gaps and can pull in "
            "neighbouring sessions' raw_event rows; pass --turn-id for a clean turn."
        )

    started_by_id, started_reused = _marker_rows_by_call_id(session_rows, "provider.call_started")
    usage_by_id, usage_reused = _marker_rows_by_call_id(session_rows, "provider.call_usage")

    ts_values = [float(r["ts"]) for r in session_rows if isinstance(r.get("ts"), (int, float))]
    window = (min(ts_values), max(ts_values)) if ts_values else (0.0, 0.0)

    marker_indexes = {
        int(r["call_index"]) for r in (*started_by_id.values(), *usage_by_id.values())
    }
    if started_by_id:
        report.capture_shape = "new" if usage_by_id else "partial"
    elif usage_by_id:
        report.capture_shape = "partial"
    else:
        report.capture_shape = "old"
    call_indexes: set[int] | None = marker_indexes or None

    raw_by_call, used_window = _scoped_raw_events(rows, call_indexes=call_indexes, window=window)

    if not started_by_id:
        report.degrade_notes.append(
            "provider.call_started rows absent: TTFT, prompt_chars, and prefix-cache "
            "columns unavailable; per-call start derived from the first raw_event."
        )
    if not usage_by_id:
        report.degrade_notes.append(
            "provider.call_usage rows absent: token counts, cache_read/creation, and "
            "tokens/sec columns unavailable (capture predates #891 instrumentation)."
        )
    if used_window:
        report.degrade_notes.append(
            "raw_event rows carry no session_id and no call_started marker exists, so "
            "they were scoped to this session by TIME WINDOW (approximate): a "
            "concurrent session overlapping in time could contaminate the per-call set."
        )

    # A call_index owned by >1 call_id (a daemon restart inside the session) makes
    # the call_index-only raw_event join ambiguous — leave those calls' streaming
    # columns unmeasured rather than merge foreign chunks in silently.
    ambiguous_indexes: set[int] = set(started_reused) | set(usage_reused)
    index_owner: dict[int, str] = {}
    for cid, row in {**started_by_id, **usage_by_id}.items():
        ci = int(row["call_index"])
        if index_owner.get(ci, cid) != cid:
            ambiguous_indexes.add(ci)
        index_owner.setdefault(ci, cid)
    if ambiguous_indexes:
        report.degrade_notes.append(
            f"call_index {sorted(ambiguous_indexes)} reused by >1 call_id within the "
            "session (daemon restart): raw_event streaming/TTFT for those calls is "
            "ambiguous and was left unmeasured rather than joined to the wrong call."
        )

    calls: list[CallRecord] = []
    if started_by_id or usage_by_id:
        for cid in dict.fromkeys((*started_by_id, *usage_by_id)):
            s = started_by_id.get(cid)
            u = usage_by_id.get(cid)
            base = s if s is not None else u
            ci = int(base["call_index"])  # type: ignore[index]
            rec = CallRecord(call_index=ci, call_id=None if cid.startswith("idx:") else cid)
            if s is not None:
                rec.model = s.get("model")
                rec.transport = s.get("transport")
                rec.started_ts = _as_float(s.get("ts"))
                rec.prompt_chars = _as_int(s.get("prompt_chars"))
                rec.prefix_16k = s.get("prefix_16k_sha256")
            if u is not None:
                rec.model = rec.model or u.get("model")
                rec.transport = rec.transport or u.get("transport")
                rec.usage_ts = _as_float(u.get("ts"))
                rec.output_chars = _as_int(u.get("output_chars"))
                rec.input_tokens = _as_int(u.get("usage_input_tokens"))
                rec.output_tokens = _as_int(u.get("usage_output_tokens"))
                rec.cache_read_tokens = _as_int(u.get("usage_cache_read_input_tokens"))
                rec.cache_creation_tokens = _as_int(u.get("usage_cache_creation_input_tokens"))
            if ci not in ambiguous_indexes:
                _attach_raw(rec, raw_by_call.get(ci, []))
            calls.append(rec)
    else:
        for ci in sorted(raw_by_call):
            rec = CallRecord(call_index=ci)
            _attach_raw(rec, raw_by_call[ci])
            calls.append(rec)

    # Cross-check: one provider.batch_response row is written per completed LM call
    # (and it carries session_id), so a joined-call count that disagrees is a loud
    # signal the raw_event/marker join is contaminated or missing calls (#891).
    batch_count = sum(1 for r in session_rows if r.get("stage") == "provider.batch_response")
    if batch_count and batch_count != len(calls):
        report.degrade_notes.append(
            f"joined {len(calls)} call(s) but the session carries {batch_count} "
            "provider.batch_response row(s) (one per LM call): the raw_event/marker "
            "join is contaminated or incomplete — pass --turn-id to bound the window; "
            "treat the attribution percentages as SUSPECT."
        )

    calls.sort(key=lambda c: (c.begin_ts if c.begin_ts is not None else 0.0, c.call_index))
    _mark_prefix_matches(calls)
    report.calls = calls
    return report


def _attach_raw(rec: CallRecord, raw: list[dict[str, Any]]) -> None:
    """Populate a call's raw-event timing fields from its scoped chunk rows."""
    if not raw:
        return
    rec.raw_event_count = len(raw)
    rec.first_raw_ts = _as_float(raw[0].get("ts"))
    rec.last_raw_ts = _as_float(raw[-1].get("ts"))
    rec.first_content_ts = _first_content_ts(raw)


def _first_content_ts(raw_events: list[dict[str, Any]]) -> float | None:
    """Return the timestamp of the first genuine model token in ``raw_events``."""
    for row in raw_events:
        channel = row.get("source_channel")
        text_len = _as_int(row.get("text_len")) or 0
        thinking_len = _as_int(row.get("thinking_len")) or 0
        if channel in _CONTENT_CHANNELS or text_len > 0 or thinking_len > 0:
            return _as_float(row.get("ts"))
    return None


def _mark_prefix_matches(calls: list[CallRecord]) -> None:
    """Set ``prefix_matches_prev``: does a cache-warm identical prefix precede it?

    The Anthropic prompt cache is content-keyed, not adjacency-keyed: a call can
    read a cached prefix laid down by ANY earlier call sharing that 16 KB
    fingerprint, provided the most recent such call was within the cache TTL.
    Comparing only against the immediately previous call (as the first cut did)
    reads ``False`` at every delegation boundary where agents interleave, wrongly
    exonerating the cache. So we track the most recent call per fingerprint and
    test it against the TTL.

    Sets ``True`` when a same-fingerprint predecessor is within
    :data:`CACHE_TTL_S`; ``False`` when the newest same-fingerprint predecessor is
    beyond the TTL (cache cold) or no earlier call shares the fingerprint; ``None``
    when the fingerprint is unknown, this is the first fingerprinted call, or the
    timestamps needed for the TTL test are missing.
    """
    last_seen: dict[str, CallRecord] = {}
    seen_any_fingerprint = False
    for call in calls:
        fp = call.prefix_16k
        if fp is None or not seen_any_fingerprint:
            call.prefix_matches_prev = None
        else:
            prior = last_seen.get(fp)
            if prior is None:
                call.prefix_matches_prev = False
            elif call.begin_ts is None or prior.end_ts is None:
                call.prefix_matches_prev = None
            else:
                call.prefix_matches_prev = (call.begin_ts - prior.end_ts) <= CACHE_TTL_S
        if fp is not None:
            last_seen[fp] = call
            seen_any_fingerprint = True


def attribute_wall(report: WaterfallReport) -> None:
    """Fill ``report.attribution`` with the wall-clock percentage breakdown.

    The turn wall clock ``[first begin, last end]`` is partitioned per call into
    three non-overlapping slices — ``lm_ttft_wait`` (begin → first *content*
    token; on the SDK path this includes the per-call spawn/connect), then
    ``lm_streaming`` (first content → last chunk), then ``lm_other`` (last chunk →
    usage row) — plus ``inter_call_gap`` between calls. Streaming deliberately
    starts at the first content token, not the first raw control event, so it does
    not double-count the pre-token control window with ``lm_ttft_wait``.

    A bucket whose defining marker is absent is reported as ``None`` (n/a), NEVER
    as a hard ``0`` that would read as a measured zero: without
    ``provider.call_started`` the submit→first-token wait is unmeasured and folds
    into ``inter_call_gap``; without ``provider.call_usage`` the post-stream tail
    is unmeasured. ``residual`` absorbs any unattributed remainder, and a negative
    residual (segments exceeding the wall) is flagged as a degradation.
    """
    calls = [c for c in report.calls if c.begin_ts is not None and c.end_ts is not None]
    if not calls:
        report.attribution = {"turn_wall_s": None, "note": "no calls with usable timestamps"}
        return
    turn_lo = min(c.begin_ts for c in calls)  # type: ignore[type-var]
    turn_hi = max(c.end_ts for c in calls)  # type: ignore[type-var]
    turn_wall = turn_hi - turn_lo

    ttft_sum = 0.0
    stream_sum = 0.0
    tail_sum = 0.0
    ttft_seen = False
    tail_seen = False
    for call in calls:
        if call.started_ts is not None and call.first_content_ts is not None:
            ttft_sum += max(0.0, call.first_content_ts - call.started_ts)
            ttft_seen = True
        content_stream = call.stream_content_span_s
        if content_stream is not None:
            stream_sum += content_stream
        tail = call.tail_s
        if tail is not None:
            tail_sum += tail
            tail_seen = True

    gap_sum = 0.0
    for prev, cur in zip(calls, calls[1:], strict=False):
        gap = (cur.begin_ts or 0.0) - (prev.end_ts or 0.0)  # type: ignore[operator]
        if gap > 0:
            gap_sum += gap

    buckets: dict[str, float | None] = {
        "lm_ttft_wait_s": ttft_sum if ttft_seen else None,
        "lm_streaming_s": stream_sum,
        "lm_other_s": tail_sum if tail_seen else None,
        "inter_call_gap_s": gap_sum,
    }
    if not ttft_seen:
        report.degrade_notes.append(
            "lm_ttft_wait unavailable (no provider.call_started markers): each call's "
            "submit->first-token wait (incl. SDK spawn) is unmeasured and folds into "
            "inter_call_gap, making that bucket an UPPER BOUND on orchestration."
        )
    if not tail_seen:
        report.degrade_notes.append(
            "lm_other (post-stream tail) unavailable (no provider.call_usage markers): "
            "the last-token->result wall is unmeasured and lands in residual."
        )
    attributed = sum(v for v in buckets.values() if v is not None)
    residual = turn_wall - attributed
    if residual < -1e-6:
        report.degrade_notes.append(
            f"wall-attribution residual is negative ({residual:.3f}s): summed call "
            "segments exceed the turn wall — overlapping or inconsistent timestamps."
        )
    report.attribution = {
        "turn_wall_s": turn_wall,
        "call_count": len(calls),
        "buckets_s": buckets,
        "residual_s": residual,
        "percent": {
            name: (value / turn_wall * 100.0 if (turn_wall > 0 and value is not None) else None)
            for name, value in {**buckets, "residual": residual}.items()
        },
    }


def gap_distribution(report: WaterfallReport) -> None:
    """Fill ``report.gap_distribution`` with cache-relevant inter-call gap stats.

    The cache-TTL question (#891 factor 1) is: how long did a given agent's prompt
    prefix sit idle between ITS OWN consecutive calls, across an interleaved
    delegation? Adjacent-call gaps keyed by the next call's model answer the wrong
    question — in the LA pipeline every agent runs the same model, collapsing to
    one adjacency group whose ``over_cache_ttl`` reads ~0 while a parent agent's
    cache is stone cold across a 10-minute child delegation.

    So ``per_group`` groups gaps by 16 KB prompt-prefix fingerprint (the stand-in
    for "same agent/prefix") and measures the delta between consecutive
    SAME-fingerprint calls — the interval the prompt cache actually had to
    survive. ``overall`` keeps the adjacent-call gaps (the orchestration gaps the
    attribution's ``inter_call_gap`` sums). When no fingerprints exist (old
    captures) it falls back to adjacent-model grouping and says so.
    """
    calls = [c for c in report.calls if c.begin_ts is not None and c.end_ts is not None]
    overall: list[float] = []
    for prev, cur in zip(calls, calls[1:], strict=False):
        gap = (cur.begin_ts or 0.0) - (prev.end_ts or 0.0)  # type: ignore[operator]
        if gap >= 0:
            overall.append(gap)

    groups: dict[str, list[float]] = {}
    have_fingerprints = any(c.prefix_16k is not None for c in calls)
    if have_fingerprints:
        by_fp: dict[str, list[CallRecord]] = {}
        for call in calls:
            if call.prefix_16k is not None:
                by_fp.setdefault(call.prefix_16k, []).append(call)
        for fp, group_calls in by_fp.items():
            for prev, cur in zip(group_calls, group_calls[1:], strict=False):
                gap = (cur.begin_ts or 0.0) - (prev.end_ts or 0.0)  # type: ignore[operator]
                if gap >= 0:
                    groups.setdefault(fp, []).append(gap)
    else:
        report.degrade_notes.append(
            "gap distribution grouped by ADJACENT-call model, not same-agent prefix "
            "(no prompt-prefix fingerprints in this capture): interleaved delegations "
            "are not isolated, so per-group over_cache_ttl understates cache-cold gaps."
        )
        for prev, cur in zip(calls, calls[1:], strict=False):
            gap = (cur.begin_ts or 0.0) - (prev.end_ts or 0.0)  # type: ignore[operator]
            if gap >= 0:
                groups.setdefault(str(cur.model or "unknown"), []).append(gap)

    def _stats(values: list[float]) -> dict[str, Any]:
        if not values:
            return {"count": 0}
        return {
            "count": len(values),
            "min_s": min(values),
            "median_s": statistics.median(values),
            "mean_s": statistics.fmean(values),
            "max_s": max(values),
            "over_cache_ttl": sum(1 for v in values if v > CACHE_TTL_S),
            "cache_ttl_s": CACHE_TTL_S,
        }

    report.gap_distribution = {
        "cache_ttl_s": CACHE_TTL_S,
        "grouping": "prefix_16k" if have_fingerprints else "adjacent_model",
        "overall": _stats(overall),
        "per_group": {key: _stats(vals) for key, vals in sorted(groups.items())},
    }


def analyze(
    rows: list[dict[str, Any]], session_id: str, turn_id: str | None = None
) -> WaterfallReport:
    """Run the full pipeline (build → attribute → gaps) for one session/turn."""
    report = build_calls(rows, session_id, turn_id)
    attribute_wall(report)
    gap_distribution(report)
    return report


def _as_float(value: Any) -> float | None:
    """Coerce to float, or ``None`` when absent/uncoercible."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    """Coerce to int, or ``None`` when absent/uncoercible."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Any, spec: str = "") -> str:
    """Format a value for the human table, rendering ``None`` as ``n/a``."""
    if value is None:
        return "n/a"
    if spec:
        return format(value, spec)
    return str(value)


def render_table(report: WaterfallReport) -> str:
    """Render the human-readable per-call table + attribution as text."""
    lines: list[str] = []
    scope = report.session_id + (
        f" turn {report.turn_id}" if report.turn_id else " (whole session)"
    )
    lines.append(f"# Turn waterfall — {scope} (capture: {report.capture_shape})")
    if report.degrade_notes:
        lines.append("")
        lines.append("Degradations (columns unavailable and why):")
        for note in report.degrade_notes:
            lines.append(f"  - {note}")
    lines.append("")
    header = (
        f"{'call#':>5} {'model':<10} {'start_ts':>15} {'ttft_s':>7} {'wall_s':>7} "
        f"{'out_tok':>7} {'tok/s':>7} {'prompt_ch':>9} {'c_read':>8} {'c_crt':>7} {'pfx=prev':>8}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for call in report.calls:
        lines.append(
            f"{call.call_index:>5} "
            f"{_fmt(call.model):<10.10} "
            f"{_fmt(call.begin_ts, '15.3f'):>15} "
            f"{_fmt(call.ttft_s, '7.2f'):>7} "
            f"{_fmt(call.wall_s, '7.2f'):>7} "
            f"{_fmt(call.output_tokens):>7} "
            f"{_fmt(call.tokens_per_s, '7.1f'):>7} "
            f"{_fmt(call.prompt_chars):>9} "
            f"{_fmt(call.cache_read_tokens):>8} "
            f"{_fmt(call.cache_creation_tokens):>7} "
            f"{_fmt(call.prefix_matches_prev):>8}"
        )
    attr = report.attribution
    lines.append("")
    lines.append("## Wall-clock attribution")
    if attr.get("turn_wall_s") is None:
        lines.append(f"  {attr.get('note', 'unavailable')}")
    else:
        lines.append(f"  turn_wall_s = {attr['turn_wall_s']:.2f} over {attr['call_count']} calls")
        for name, pct in attr["percent"].items():
            secs = (
                attr["buckets_s"].get(name, attr.get("residual_s"))
                if name != "residual"
                else attr["residual_s"]
            )
            lines.append(f"    {name:<18} {_fmt(secs, '9.2f')} s  {_fmt(pct, '6.1f')} %")
    gaps = report.gap_distribution
    if gaps:
        lines.append("")
        lines.append(
            f"## Inter-call gap distribution (cache TTL = {gaps['cache_ttl_s']:.0f}s, "
            f"per-group by {gaps.get('grouping', 'prefix_16k')})"
        )
        ov = gaps["overall"]
        if ov.get("count"):
            lines.append(
                f"  overall (adjacent): n={ov['count']} median={ov['median_s']:.2f}s "
                f"mean={ov['mean_s']:.2f}s max={ov['max_s']:.2f}s "
                f"over_ttl={ov['over_cache_ttl']}"
            )
        for group, st in gaps["per_group"].items():
            if st.get("count"):
                lines.append(
                    f"  {group[:16]}: n={st['count']} median={st['median_s']:.2f}s "
                    f"max={st['max_s']:.2f}s over_ttl={st['over_cache_ttl']}"
                )
    return "\n".join(lines)


def report_to_dict(report: WaterfallReport) -> dict[str, Any]:
    """Convert a :class:`WaterfallReport` to a JSON-serializable dict."""
    return {
        "session_id": report.session_id,
        "turn_id": report.turn_id,
        "capture_shape": report.capture_shape,
        "degrade_notes": report.degrade_notes,
        "calls": [
            {
                "call_index": c.call_index,
                "call_id": c.call_id,
                "model": c.model,
                "transport": c.transport,
                "begin_ts": c.begin_ts,
                "end_ts": c.end_ts,
                "ttft_s": c.ttft_s,
                "stream_span_s": c.stream_span_s,
                "wall_s": c.wall_s,
                "raw_event_count": c.raw_event_count,
                "prompt_chars": c.prompt_chars,
                "output_chars": c.output_chars,
                "input_tokens": c.input_tokens,
                "output_tokens": c.output_tokens,
                "tokens_per_s": c.tokens_per_s,
                "cache_read_tokens": c.cache_read_tokens,
                "cache_creation_tokens": c.cache_creation_tokens,
                "prefix_matches_prev": c.prefix_matches_prev,
            }
            for c in report.calls
        ],
        "attribution": report.attribution,
        "gap_distribution": report.gap_distribution,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    # The report prints on any platform (this analysis is run on Windows), so
    # force UTF-8 on stdout rather than let a non-ASCII char crash under cp1252.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", required=True, type=Path, help="stream-audit JSONL path")
    parser.add_argument("--session-id", required=True, help="GACT session_id to analyze")
    parser.add_argument(
        "--turn-id",
        default=None,
        help="optional turn_id to restrict to (recommended: clean per-turn window)",
    )
    parser.add_argument(
        "--messages",
        type=Path,
        default=None,
        help="optional session messages.json (reserved for delegation boundaries)",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="write the machine-readable JSON report to this path",
    )
    args = parser.parse_args(argv)

    if not args.audit.exists():
        print(f"error: audit file not found: {args.audit}", file=sys.stderr)
        return 2
    rows = load_rows(args.audit)
    session_rows = [r for r in rows if str(r.get("session_id") or "") == args.session_id]
    if not session_rows:
        print(
            f"error: no rows for session_id={args.session_id!r}. "
            f"available: {available_sessions(rows)}",
            file=sys.stderr,
        )
        return 2

    report = analyze(rows, args.session_id, args.turn_id)
    print(render_table(report))
    payload = report_to_dict(report)
    if args.json_out is not None:
        args.json_out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"\nJSON report written to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
