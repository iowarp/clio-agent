"""Per-surface normalizer + differ acceptance (design §4.1.A).

Proves each normalizer's masking table and the differ's first-divergence report,
including the two decisive negative controls the S0 brief demands:

* **drop-detection** — a deliberately suppressed SSE event type turns the diff RED
  (not set-equality: the DROP is named).
* **one-byte mutation** — a single content byte changed in a projection is caught
  with a precise field-path report.
"""

from __future__ import annotations

import copy
from typing import Any

from tests.equivalence import normalizers as N


class _Ev:
    """Minimal bus-event stand-in (``.type`` / ``.payload``)."""

    def __init__(self, type: str, payload: dict[str, Any]) -> None:
        self.type = type
        self.payload = payload
        self.session_id = "s"


class _Seg:
    """Minimal segment stand-in (``.kind`` / ``.content`` / ``.order`` / ...)."""

    def __init__(self, kind: str, content: dict[str, Any], order: float = 0.0) -> None:
        self.kind = kind
        self.content = content
        self.order = order
        self.status = "live"
        self.id = "seg"
        self.logical_time = 1


# --------------------------------------------------------------------------- #
# first_divergence — the differ
# --------------------------------------------------------------------------- #


def test_first_divergence_reports_precise_nested_path() -> None:
    left = {"messages": [{"parts": [{"text": "hello"}]}]}
    right = {"messages": [{"parts": [{"text": "hellX"}]}]}
    div = N.first_divergence(left, right)
    assert div is not None
    assert div.path == ".messages[0].parts[0].text"
    assert div.reason == "value"
    assert div.left == "hello" and div.right == "hellX"


def test_first_divergence_reports_length_and_keys() -> None:
    assert N.first_divergence([1, 2], [1, 2, 3]).reason == "length"
    assert N.first_divergence({"a": 1}, {"b": 1}).reason == "keys"


def test_first_divergence_none_when_equal() -> None:
    obj = {"a": [1, {"b": "x"}], "c": 2.0}
    assert N.first_divergence(obj, copy.deepcopy(obj)) is None


def test_int_float_compared_numerically() -> None:
    # JSON round-trips 0 as 0 or 0.0; numeric equality must not be a type divergence.
    assert N.first_divergence({"x": 0}, {"x": 0.0}) is None
    assert N.first_divergence({"x": 0}, {"x": 1.0}).reason == "value"


# --------------------------------------------------------------------------- #
# SSE surface
# --------------------------------------------------------------------------- #


def _sse_ref() -> list[_Ev]:
    return [
        _Ev("session.status_changed", {"status": "running"}),  # excluded
        _Ev("message.created", {"id": "m1", "created_at": "T0", "role": "assistant"}),
        _Ev("message.part.added", {"id": "p1", "part": {"type": "text"}}),
        _Ev("message.part.delta", {"part_id": "p1", "delta": {"text_append": "he"}}),  # excluded
        _Ev("arc.op", {"id": "o1", "kind": "summarize", "token_count": 5}),
        _Ev("message.part.completed", {"part_id": "p1", "final_text": "hello"}),
        _Ev("message.completed", {"id": "m1", "duration_ms": 12.0, "cost_usd": 0.01}),
    ]


def test_sse_excludes_transport_and_timing_rows() -> None:
    norm = N.normalize_sse(_sse_ref())
    types = [e["type"] for e in norm]
    assert "session.status_changed" not in types
    assert "message.part.delta" not in types  # coalesced away (completed is authoritative)
    assert "arc.op" in types and "message.completed" in types


def test_sse_masks_nonnormative_fields() -> None:
    norm = N.normalize_sse(_sse_ref())
    completed = [e for e in norm if e["type"] == "message.completed"][0]
    assert completed["payload"]["duration_ms"] == N.MASKED
    assert completed["payload"]["cost_usd"] == N.MASKED
    created = [e for e in norm if e["type"] == "message.created"][0]
    assert created["payload"]["id"] == N.MASKED
    assert created["payload"]["created_at"] == N.MASKED
    assert created["payload"]["role"] == "assistant"  # normative content survives


def test_sse_equivalent_when_only_ids_and_clock_differ() -> None:
    ref = _sse_ref()
    cand = copy.deepcopy(_sse_ref())
    for ev in cand:  # different ids/clock, same types + normative payload
        for k in ("id", "created_at", "duration_ms", "cost_usd"):
            if k in ev.payload:
                ev.payload[k] = "DIFFERENT"
    assert N.diff_sse(ref, cand).empty


def test_sse_drop_detection_fires_on_suppressed_type() -> None:
    """THE drop-detection control: suppressing ``arc.op`` turns the diff RED and NAMES
    the dropped type — presence, not set-equality."""
    ref = _sse_ref()
    cand = [ev for ev in copy.deepcopy(_sse_ref()) if ev.type != "arc.op"]
    report = N.diff_sse(ref, cand)
    assert not report.empty
    assert report.divergence.reason == "drop_detection"
    assert "arc.op" in str(report.divergence.right)  # the dropped type is named


def test_sse_payload_mutation_caught_when_types_agree() -> None:
    ref = _sse_ref()
    cand = copy.deepcopy(_sse_ref())
    [e for e in cand if e.type == "arc.op"][0].payload["kind"] = "delete"  # summarize -> delete
    report = N.diff_sse(ref, cand)
    assert not report.empty
    assert report.divergence.path.endswith(".kind")


# --------------------------------------------------------------------------- #
# context + trace surfaces
# --------------------------------------------------------------------------- #


def test_context_is_byte_identical_and_unmasked() -> None:
    a = [_Seg("thought", {"text": "A"}), _Seg("observation", {"text": "B"})]
    b = [_Seg("thought", {"text": "A"}), _Seg("observation", {"text": "B"})]
    assert N.diff_context(a, b).empty
    assert not N.diff_context(a, [_Seg("thought", {"text": "A"})]).empty  # length


def test_context_mutation_caught_with_path() -> None:
    a = [_Seg("thought", {"text": "A"}), _Seg("observation", {"text": "B"})]
    b = [_Seg("thought", {"text": "A"}), _Seg("observation", {"text": "B_MUT"})]
    report = N.diff_context(a, b)
    assert not report.empty
    assert report.divergence.path == "[1].content.text"


def test_trace_masks_clock_but_compares_content_and_order() -> None:
    a = [_Seg("thought", {"text": "A"}, order=1.0), _Seg("observation", {"text": "B"}, order=2.0)]
    b = [_Seg("thought", {"text": "A"}, order=1.0), _Seg("observation", {"text": "B"}, order=2.0)]
    assert N.diff_trace(a, b).empty
    # reordering IS caught (order is part of the trace content)
    b_reordered = [
        _Seg("observation", {"text": "B"}, order=2.0),
        _Seg("thought", {"text": "A"}, order=1.0),
    ]
    assert not N.diff_trace(a, b_reordered).empty
