"""The durable trace is a proven DERIVATION of the ``_events`` log (#737 S1).

Pins the S1 invariants that make ``_events`` the canonical floor and the durable-trace
JSONL a projection of it, never an independent second history:

* **Derivation (scope 1).** The trace JSONL line for an event equals the projection of
  the matching ``_events`` segment content — proven on a synthetic exotic-type matrix
  AND on the REAL ``ARCMemory.record_semantic_event`` → sink → ``FileSemanticTraceBackend``
  path (one emit, two derivations, byte-equal on every carried field).
* **Order (scope 3a).** Every event lands in ``_events`` BEFORE any sink sees it.
* **Recursion (scope 3b, §2.9).** The ``arc.op`` raw lane derives DIRECTLY to the sink;
  it never re-enters ``record`` — pinned by an entry counter that would climb if it did.
* **#762 backfill (scope 3c).** With ``trace.backend=file``, ``release_session`` erases
  ``_events``; the JSONL is then a LOSSLESS backfill source (round-trip byte-equal).

Sabotages (restored after): (a) a projection that skips a carried field → derivation
diff RED; (b) sink-before-log → order pin RED; (c) a dropped JSONL line → backfill
reports a typed gap.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc.live import build_event_content
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.events import EventBus
from clio_agent.gact.semantic_events import (
    EVENTS_CONTENT_CARRIED_FIELDS,
    EVENTS_CONTENT_UNCARRIED_TRACE_FIELDS,
    NoopSemanticTraceBackend,
    SemanticEvent,
    SemanticEventSink,
    trace_line_from_events_content,
)
from clio_agent.gact.trace_backfill import (
    BackfillResult,
    TraceUnavailable,
    backfill_events_from_trace,
    verify_events_roundtrip,
)
from tests._config_layer import set_config
from tests.equivalence.normalizers import MASKED, first_divergence

_MASK = EVENTS_CONTENT_UNCARRIED_TRACE_FIELDS


def _mask_uncarried(line: dict[str, Any]) -> dict[str, Any]:
    """Mask the trace-line fields ``_events`` content does not carry (§2.3), so the diff
    asserts only the DERIVABLE fields."""
    return {k: (MASKED if k in _MASK else v) for k, v in line.items()}


def _sample_events() -> list[SemanticEvent]:
    """A spread of real event types WITH exotic bodies (the caveat-a payloads)."""
    return [
        SemanticEvent(
            event_type="turn.started",
            session_id="s1",
            trace_id="tr",
            turn_id="t1",
            occurred_at="2026-06-14T00:00:00+00:00",
            payload={"input": "stations near San Diego"},
        ),
        SemanticEvent(
            event_type="react.step.completed",
            session_id="s1",
            trace_id="tr",
            turn_id="t1",
            occurred_at="2026-06-14T00:00:01+00:00",
            actor={"agent_id": "data", "tags": frozenset({"io"})},
            payload={"observation": ("x", 1, 2.5), "flag": frozenset({"only"})},
        ),
        SemanticEvent(
            event_type="expert.response.completed",
            session_id="s1",
            trace_id="tr",
            turn_id="t1",
            occurred_at="2026-06-14T00:00:02+00:00",
            provider={"model_id": "m", "opt": None},  # None kept (unified encoder)
            payload={"answer": "Found 71 stations.", "reasoning": "because"},
        ),
    ]


# --------------------------------------------------------------------------- #
# Derivation — synthetic (build_event_content vs to_dict "full")
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("event", _sample_events(), ids=lambda e: e.event_type)
def test_trace_line_is_projection_of_events_content(event: SemanticEvent) -> None:
    """Scope 1: the trace line == the projection of the ``_events`` content, on every
    carried field (uncarried envelope masked)."""
    content = build_event_content(event)
    assert content is not None
    actual = event.to_dict("full")
    derived = trace_line_from_events_content(
        content, session_id=event.session_id, turn_id=event.turn_id
    )
    div = first_divergence(_mask_uncarried(actual), _mask_uncarried(derived))
    assert div is None, div.pretty() if div else ""
    # And every carried field is reproduced VERBATIM (not merely masked away).
    for f in EVENTS_CONTENT_CARRIED_FIELDS:
        assert derived[f] == actual[f], f


def test_sabotage_projection_skipping_a_field_goes_red() -> None:
    """Sabotage (a): a projection that DROPS a carried field diverges with a precise
    field path — proving the derivation diff is a real gate, not a tautology."""
    event = _sample_events()[1]
    content = build_event_content(event)
    assert content is not None
    actual = event.to_dict("full")
    sabotaged = trace_line_from_events_content(
        content, session_id=event.session_id, turn_id=event.turn_id
    )
    sabotaged.pop("payload")  # skip a carried field
    div = first_divergence(_mask_uncarried(actual), _mask_uncarried(sabotaged))
    assert div is not None and div.reason == "keys"


# --------------------------------------------------------------------------- #
# Derivation on the REAL record path (one emit, two derivations)
# --------------------------------------------------------------------------- #


def _read_events_contents(arc: ARCMemory, sid: str) -> list[dict[str, Any]]:
    """All of a session's ``_events`` segment contents, in log order (chunk family)."""
    contents: list[dict[str, Any]] = []
    for scope in arc._live.events_scopes(sid):
        contents.extend(seg.content for seg in arc.render_segments(sid, scope))
    return contents


def _read_trace_lines(trace_dir: Path, sid: str) -> list[dict[str, Any]]:
    path = trace_dir / f"{sid}.semantic.jsonl"
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _wire_real_arc(tmp_path: Path, backend: Any) -> tuple[ARCMemory, SemanticEventSink]:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    sink = SemanticEventSink(bus=EventBus(), trace_backend=backend, live_consumers=None)
    arc.set_highway_sink(sink.emit)
    return arc, sink


def test_real_record_path_trace_equals_events_projection(tmp_path: Path) -> None:
    """The production flow: ``record_semantic_event`` persists ``_events`` AND (via the
    sink) writes the trace JSONL. Each JSONL line equals the projection of its matching
    ``_events`` segment — the trace is a derivation of the log, proven end to end."""
    from clio_agent.gact.semantic_events import FileSemanticTraceBackend

    trace_dir = tmp_path / "traces"
    backend = FileSemanticTraceBackend(trace_dir)
    arc, _sink = _wire_real_arc(tmp_path, backend)

    events = _sample_events()
    for ev in events:
        arc.record_semantic_event(ev)
    backend.flush()

    contents = _read_events_contents(arc, "s1")
    lines = _read_trace_lines(trace_dir, "s1")
    assert len(contents) == len(lines) == len(events)

    for seg_content, actual_line in zip(contents, lines, strict=True):
        # Recover session_id/turn_id from the segment envelope via the trace line's own
        # (they are equal; the fold takes them as envelope inputs).
        derived = trace_line_from_events_content(
            seg_content, session_id=actual_line["session_id"], turn_id=actual_line["turn_id"]
        )
        div = first_divergence(_mask_uncarried(actual_line), _mask_uncarried(derived))
        assert div is None, div.pretty() if div else ""


# --------------------------------------------------------------------------- #
# Order pin (scope 3a) — event in _events BEFORE the sink sees it
# --------------------------------------------------------------------------- #


def test_event_lands_in_events_before_sink_sees_it(tmp_path: Path) -> None:
    """Scope 3a: at the moment the highway sink runs, the event is ALREADY queryable in
    ``_events`` — the log is the floor, the sink a downstream projection."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    present_at_emit: list[bool] = []

    def sink(event: SemanticEvent) -> dict[str, Any]:
        types_now = [c["event_type"] for c in _read_events_contents(arc, event.session_id)]
        present_at_emit.append(event.event_type in types_now)
        return {}

    arc.set_highway_sink(sink)
    arc.record_semantic_event(_sample_events()[0])
    assert present_at_emit == [True]


def test_sabotage_sink_before_log_would_fail_the_order_pin(tmp_path: Path) -> None:
    """Sabotage (b): if the sink ran BEFORE the log write, the same probe sees the event
    ABSENT from ``_events`` — demonstrating the order pin has teeth. We reproduce the
    reversed order explicitly (never mutating production) and assert the probe flips."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    event = _sample_events()[0]

    def probe() -> bool:
        types_now = [c["event_type"] for c in _read_events_contents(arc, event.session_id)]
        return event.event_type in types_now

    # Reversed order (the sabotage): sink observes FIRST, log write SECOND.
    saw_before_log = probe()  # sink's view, before on_semantic_event ran
    arc.on_semantic_event(event)  # the log write that production does FIRST
    saw_after_log = probe()

    assert saw_before_log is False  # the pin WOULD go red under sink-before-log
    assert saw_after_log is True  # and green once the log write lands


# --------------------------------------------------------------------------- #
# Recursion pin (scope 3b, §2.9) — arc.op never re-enters record
# --------------------------------------------------------------------------- #


def _arc_op_event(session_id: str, op: str) -> SemanticEvent:
    return SemanticEvent(
        event_type="arc.op",
        session_id=session_id,
        trace_id="tr",
        turn_id="t1",
        status=op,
        occurred_at="2026-06-14T00:00:00+00:00",
        payload={"op": op},
    )


def test_arc_op_derives_directly_never_reentering_record(tmp_path: Path) -> None:
    """§2.9: the production op-logger derives ``arc.op`` DIRECTLY to the sink, so
    recording ONE event enters ``record`` exactly once — the append's ``arc.op`` does
    not re-enter it. An entry counter would climb past 1 if the raw lane were violated."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    sink = SemanticEventSink(
        bus=EventBus(), trace_backend=NoopSemanticTraceBackend(), live_consumers=None
    )
    arc.set_highway_sink(sink.emit)
    # Production-shape op-logger: arc.op -> sink.emit (direct), NEVER arc.record.
    arc.set_segment_op_logger(
        lambda op, session_id, scope, **kw: sink.emit(_arc_op_event(session_id, op))
    )

    entries = {"n": 0}
    real_record = arc.record_semantic_event

    def counting(event: Any) -> Any:
        entries["n"] += 1
        return real_record(event)

    arc.record_semantic_event = counting  # type: ignore[method-assign]
    arc.record_semantic_event(_sample_events()[0])

    assert entries["n"] == 1  # one event -> one record; arc.op did NOT re-enter
    # exactly one persisted semantic_event (arc.op derived to the sink, not the log)
    assert [c["event_type"] for c in _read_events_contents(arc, "s1")] == ["turn.started"]


def test_routing_arc_op_through_record_forms_the_loop(tmp_path: Path) -> None:
    """The §2.9 violation, made visible: routing ``arc.op`` back through ``record`` re-
    enters the op-logger (record -> append -> op-logger -> record -> …). A depth bound
    stops a real stack overflow; the climb past 1 IS the loop-forming signal that the
    direct raw lane prevents."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    sink = SemanticEventSink(
        bus=EventBus(), trace_backend=NoopSemanticTraceBackend(), live_consumers=None
    )
    arc.set_highway_sink(sink.emit)
    depth = {"n": 0}

    def wrong_op_logger(op: str, session_id: str, scope: str, **kw: Any) -> Any:
        depth["n"] += 1
        if depth["n"] < 5:  # bound: the loop would otherwise RecursionError
            arc.record_semantic_event(_arc_op_event(session_id, op))
        return {}

    arc.set_segment_op_logger(wrong_op_logger)
    arc.record_semantic_event(_sample_events()[0])

    assert depth["n"] >= 5  # the loop FORMED — the raw-lane discipline is load-bearing


# --------------------------------------------------------------------------- #
# #762 backfill round-trip (scope 3c)
# --------------------------------------------------------------------------- #


def _record_with_file_trace(tmp_path: Path, monkeypatch: Any) -> tuple[ARCMemory, Path]:
    """Record the sample events with the FILE trace backend wired (so #762 erase is
    armed) and return (arc, trace_dir)."""
    from clio_agent.gact.semantic_events import FileSemanticTraceBackend

    set_config("trace.backend", "file")  # file-layer (file > env); #985 config-first
    trace_dir = tmp_path / "traces"
    backend = FileSemanticTraceBackend(trace_dir)
    arc, _sink = _wire_real_arc(tmp_path, backend)
    for ev in _sample_events():
        arc.record_semantic_event(ev)
    backend.flush()
    return arc, trace_dir


def test_backfill_roundtrip_is_lossless(tmp_path: Path, monkeypatch: Any) -> None:
    """Scope 3c: ``_events`` -> [#762 erase] -> backfill-from-JSONL -> byte-equal
    ``_events`` content. The trace is a lossless recovery source."""
    arc, trace_dir = _record_with_file_trace(tmp_path, monkeypatch)
    original = _read_events_contents(arc, "s1")
    assert original  # holds the recorded events

    arc.release_session("s1")  # #762: file backend => _events erased
    assert _read_events_contents(arc, "s1") == []

    result = backfill_events_from_trace(trace_dir / "s1.semantic.jsonl")
    report = verify_events_roundtrip(original, result)
    assert report.lossless, report.pretty()
    assert report.matched == len(original)


def test_backfill_reports_typed_gap_on_dropped_line(tmp_path: Path, monkeypatch: Any) -> None:
    """Sabotage (c): dropping ONE JSONL line makes the round-trip report a typed gap
    (``dropped``) at the missing event's index — not a silent short recovery."""
    arc, trace_dir = _record_with_file_trace(tmp_path, monkeypatch)
    original = _read_events_contents(arc, "s1")

    path = trace_dir / "s1.semantic.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    # Drop the MIDDLE line.
    kept = [ln for i, ln in enumerate(lines) if i != 1]
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    result = backfill_events_from_trace(path)
    report = verify_events_roundtrip(original, result)
    assert not report.lossless
    assert report.recovered == len(original) - 1
    assert any(g.detail in {"dropped", "content_mismatch"} for g in report.gaps)


def test_backfill_missing_trace_raises_typed(tmp_path: Path) -> None:
    """Wrong-input: an absent trace file raises the TYPED ``TraceUnavailable`` (the #762
    recovery source is gone) — never a silent empty recovery."""
    with pytest.raises(TraceUnavailable):
        backfill_events_from_trace(tmp_path / "nope" / "s1.semantic.jsonl")


def test_backfill_reports_typed_reason_on_malformed_line(tmp_path: Path) -> None:
    """Error-path: a malformed JSONL line becomes a typed ``BackfillReason`` (no
    ``except: continue`` data-disappearance, §4.4d) while good lines still recover."""
    path = tmp_path / "s1.semantic.jsonl"
    good = _sample_events()[0].to_dict("full")
    path.write_text(
        json.dumps(good, sort_keys=True) + "\n" + "{not valid json\n",
        encoding="utf-8",
    )
    result: BackfillResult = backfill_events_from_trace(path)
    assert len(result.events) == 1  # the good line recovered
    assert not result.ok
    assert result.reasons[0].kind == "unreadable_json" and result.reasons[0].line_no == 2
