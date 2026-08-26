"""Recover the ARC ``_events`` log from the durable semantic-trace JSONL (#737 S1).

The ARC ``_events`` semantic-event log is the canonical floor (design §2.8.a): every
event lands there before the durable-trace sink writes its JSONL line. But #762 gives
that floor a documented hole — when ``trace.backend=file``, ``ARCMemory.release_session``
ERASES the ``_events`` chunk family on release *because the durable trace then holds the
full history* (``arc/memory.py`` #762 branch). This module is the INVERSE seam that
makes that erase safe: it reconstructs the erased ``_events`` content from the trace
JSONL, so the trace is a proven LOSSLESS backfill source, not a lossy sidecar.

The round-trip the harness proves (``tests/test_equivalence/test_trace_derivation.py``):

    _events content  --(sink: to_dict "full")-->  trace JSONL
    trace JSONL      --(backfill_events_from_trace)-->  _events content'
    assert content == content'      (byte-for-byte, after encoder unification)

Byte-equality holds because the trace body and the ``_events`` body share ONE coercion
(``_encode_safe``; see ``semantic_events._json_safe``), so re-encoding a round-tripped
line is idempotent. Every failure is TYPED (no silent skip): a missing file raises
:class:`TraceUnavailable`; an unreadable or malformed line becomes a typed
:class:`BackfillReason`; a dropped/mismatched event surfaces as a :class:`RoundTripGap`.
This is the §4.4(d) discipline — a version/format problem is loud, never
data-disappearance via ``except: continue``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clio_agent.arc.live import build_event_content
from clio_agent.gact.semantic_events import SemanticEvent

# --------------------------------------------------------------------------- #
# Typed failures / results (no silent fallback — design §3.3, §4.4d)
# --------------------------------------------------------------------------- #


class TraceUnavailable(Exception):
    """The durable-trace JSONL source needed for backfill does not exist.

    Raised (never swallowed) by :func:`backfill_events_from_trace` when the configured
    trace path is absent — the #762 recovery is impossible and the caller must know,
    rather than silently recovering zero events.
    """


@dataclass(frozen=True)
class BackfillReason:
    """A typed reason ONE trace line could not be reconstructed (no silent skip).

    ``kind`` is a machine tag (``unreadable_json`` / ``not_an_object`` /
    ``missing_event_type``); ``line_no`` is 1-based; ``detail`` is a short human note.
    """

    kind: str
    line_no: int
    detail: str


@dataclass(frozen=True)
class BackfilledEvent:
    """One recovered ``_events`` record: the content dict plus its Segment envelope.

    ``content`` is byte-identical to what :func:`clio_agent.arc.live.build_event_content`
    originally stored; ``session_id`` / ``turn_id`` are the envelope fields the append
    lane stamps onto the ``semantic_event`` segment (``_events`` content itself never
    carries them — §2.3).
    """

    content: dict[str, Any]
    session_id: str
    turn_id: str


@dataclass
class BackfillResult:
    """The outcome of reading a trace JSONL: recovered events + per-line typed reasons."""

    source: str
    events: list[BackfilledEvent] = field(default_factory=list)
    reasons: list[BackfillReason] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when EVERY line reconstructed (no typed failure reasons)."""
        return not self.reasons

    def pretty(self) -> str:
        head = f"backfill[{self.source}] recovered={len(self.events)}"
        if self.ok:
            return head + " reasons=(none)"
        lines = [f"  - line {r.line_no} {r.kind}: {r.detail}" for r in self.reasons]
        return head + f" reasons={len(self.reasons)}:\n" + "\n".join(lines)


@dataclass(frozen=True)
class RoundTripGap:
    """One place the backfilled ``_events`` diverges from the original (a typed gap).

    ``index`` is the position in the original event order; ``expected_event_type`` /
    ``recovered_event_type`` name the two sides (``recovered_event_type`` is ``None``
    when a line was DROPPED and nothing was recovered at that slot); ``detail`` is the
    machine reason (``dropped`` / ``content_mismatch`` / ``extra``).
    """

    index: int
    expected_event_type: str
    recovered_event_type: str | None
    detail: str


@dataclass
class RoundTripReport:
    """Whether the trace→``_events`` backfill reproduced the original log losslessly."""

    matched: int
    expected: int
    recovered: int
    gaps: list[RoundTripGap] = field(default_factory=list)
    reasons: list[BackfillReason] = field(default_factory=list)

    @property
    def lossless(self) -> bool:
        """True when every original event was recovered byte-for-byte, in order, with
        no unreadable lines."""
        return not self.gaps and not self.reasons and self.matched == self.expected

    def pretty(self) -> str:
        head = (
            f"roundtrip matched={self.matched}/{self.expected} "
            f"recovered={self.recovered} lossless={self.lossless}"
        )
        parts = [head]
        for g in self.gaps:
            parts.append(
                f"  GAP @{g.index} {g.detail}: expected={g.expected_event_type!r} "
                f"recovered={g.recovered_event_type!r}"
            )
        for r in self.reasons:
            parts.append(f"  UNREADABLE line {r.line_no} {r.kind}: {r.detail}")
        return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Reconstruction
# --------------------------------------------------------------------------- #


def semantic_event_from_trace_dict(line: dict[str, Any]) -> SemanticEvent:
    """Rebuild a SemanticEvent from ONE ``to_dict("full")`` durable-trace line.

    The trace line is a SUPERSET of the ``_events`` content (it carries the full
    envelope: ``span_id`` / ``parent_span_id`` / ``workspace_id`` / ``blueprint`` /
    ``live_observed`` / ``detail_level`` / ``schema_version``), so — unlike the
    ``_events``-content fold — this reconstruction is lossless for every SemanticEvent
    field. Feeding the result back through
    :func:`clio_agent.arc.live.build_event_content` reproduces the original ``_events``
    content byte-for-byte.

    Args:
        line: A parsed durable-trace JSONL object (``SemanticEvent.to_dict("full")``).

    Returns:
        The reconstructed SemanticEvent. ``span_id`` is taken from the line (never
        re-minted), so the reconstruction is pure.
    """
    return SemanticEvent(
        event_type=str(line.get("event_type", "") or ""),
        session_id=str(line.get("session_id", "") or ""),
        trace_id=str(line.get("trace_id", "") or ""),
        turn_id=str(line.get("turn_id", "") or ""),
        workspace_id=str(line.get("workspace_id", "") or ""),
        span_id=str(line.get("span_id", "") or ""),
        parent_span_id=str(line.get("parent_span_id", "") or ""),
        status=str(line.get("status", "") or ""),
        summary=str(line.get("summary", "") or ""),
        actor=dict(line.get("actor") or {}),
        subject=dict(line.get("subject") or {}),
        blueprint=dict(line.get("blueprint") or {}),
        provider=dict(line.get("provider") or {}),
        payload=dict(line.get("payload") or {}),
        live_observed=bool(line.get("live_observed", True)),
        detail_level=str(line.get("detail_level", "") or "") or "semantic",
        occurred_at=str(line.get("occurred_at", "") or ""),
    )


def backfill_events_from_trace(trace_path: Path) -> BackfillResult:
    """Reconstruct the ``_events`` records for a session from its durable-trace JSONL.

    Reads ``trace_path`` line by line, rebuilds a SemanticEvent per line
    (:func:`semantic_event_from_trace_dict`), and re-derives the ``_events`` content via
    :func:`clio_agent.arc.live.build_event_content`. Every unreadable/malformed line
    yields a TYPED :class:`BackfillReason` (never a silent ``except: continue``); a blank
    line is not an event and is ignored without a reason.

    Args:
        trace_path: The session's ``*.semantic.jsonl`` durable-trace file.

    Returns:
        A :class:`BackfillResult` carrying the recovered events (in file order) and any
        per-line reasons.

    Raises:
        TraceUnavailable: ``trace_path`` does not exist — the #762 recovery source is
            gone and the caller must handle the loss explicitly.
    """
    if not trace_path.exists():
        raise TraceUnavailable(f"durable trace not found: {trace_path}")

    result = BackfillResult(source=str(trace_path))
    text = trace_path.read_text(encoding="utf-8")
    for idx, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue  # blank separator line — not an event, not a failure
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            result.reasons.append(BackfillReason("unreadable_json", idx, str(exc)))
            continue
        if not isinstance(obj, dict):
            result.reasons.append(
                BackfillReason(
                    "not_an_object", idx, f"line is a {type(obj).__name__}, not an object"
                )
            )
            continue
        event = semantic_event_from_trace_dict(obj)
        content = build_event_content(event)
        if content is None:
            result.reasons.append(
                BackfillReason("missing_event_type", idx, "line has no event_type; cannot rebuild")
            )
            continue
        result.events.append(
            BackfilledEvent(content=content, session_id=event.session_id, turn_id=event.turn_id)
        )
    return result


def verify_events_roundtrip(
    original: list[dict[str, Any]], result: BackfillResult
) -> RoundTripReport:
    """Compare backfilled ``_events`` content against the ORIGINAL log, in order.

    ``original`` is the list of ``_events`` content dicts captured BEFORE the #762
    erase; ``result`` is what :func:`backfill_events_from_trace` recovered from the
    JSONL. The comparison is positional and byte-for-byte: a dropped JSONL line makes
    the recovered stream short and misaligned, surfaced as a :class:`RoundTripGap` at
    the first divergence (``detail="dropped"``) plus the count delta — this is the typed
    gap the sabotage test expects. Unreadable-line reasons from the backfill are carried
    through so a malformed line is never mistaken for a clean recovery.

    Args:
        original: The ``_events`` content dicts, in log order, before erase.
        result: The backfill outcome to verify.

    Returns:
        A :class:`RoundTripReport`; ``lossless`` iff every original event was recovered
        byte-for-byte with no unreadable lines.
    """
    recovered = [ev.content for ev in result.events]
    gaps: list[RoundTripGap] = []
    matched = 0
    for i, expected in enumerate(original):
        exp_type = str(expected.get("event_type", "") or "")
        if i >= len(recovered):
            gaps.append(RoundTripGap(i, exp_type, None, "dropped"))
            continue
        got = recovered[i]
        if got == expected:
            matched += 1
        else:
            gaps.append(
                RoundTripGap(i, exp_type, str(got.get("event_type", "") or ""), "content_mismatch")
            )
    for j in range(len(original), len(recovered)):
        got_type = str(recovered[j].get("event_type", "") or "")
        gaps.append(RoundTripGap(j, "", got_type, "extra"))
    return RoundTripReport(
        matched=matched,
        expected=len(original),
        recovered=len(recovered),
        gaps=gaps,
        reasons=list(result.reasons),
    )
