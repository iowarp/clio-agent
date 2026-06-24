"""ARC must persist a semantic event whose payload carries a NON-msgpack-native object.

Live root cause (qwopus, confirmed via the now-unswallowed exception): ``lm.call`` /
``react.step.completed`` / ``tool.call.*`` events carry litellm's usage object
(``Usage`` / ``CompletionTokensDetailsWrapper`` / ``PromptTokensDetailsWrapper`` —
pydantic-ish objects with ``__dict__`` / ``model_dump``). ARC's durable record encodes
``semantic_event`` segments with msgspec/msgpack, which is STRICT: it raised
``TypeError('Encoding objects of type CompletionTokensDetailsWrapper is unsupported')``.
``record_semantic_event`` swallowed it (``except Exception: pass``), so those events
reached the tolerant durable trace (JSON) but NEVER ARC (strict msgpack) — the
"trace ⊋ ARC" bypass.

The fix coerces any non-native value to a plain serializable form in
:func:`build_event_content` (the ONE place that builds the persisted content dict), so
ARC NEVER throws on an exotic payload from ANY emit site — generally, not a one-off for
one litellm type. These tests construct events with the offending object SHAPES directly
(a dataclass + a ``__dict__`` object + nested sets/tuples), so they are fully offline +
deterministic, and assert:

  (a) the event PERSISTS to ``_events`` (``render_segments`` returns it) AND round-trips
      through ``encode_segments`` / ``decode_segments``;
  (b) it persists WITHOUT the ``ARC-EVENTS`` ``FAILED`` trace.event firing (no silent or
      logged drop);
  (c) ``build_event_content``'s output is fully msgpack-encodable for a payload with
      nested exotic objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from clio_agent.arc.live import EVENTS_SCOPE, _encode_safe, build_event_content
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import Segment, decode_segments, encode_segments
from clio_agent.gact.semantic_events import SemanticEvent


# ---------------------------------------------------------------------------
# Stand-ins for litellm/openai usage objects (the offending payload shapes), with
# NO litellm dependency — exercising the same coercion strategies the production
# objects hit (msgspec cannot natively encode either of these).
# ---------------------------------------------------------------------------
@dataclass
class _CompletionTokensDetailsWrapper:
    """Mimics litellm's dataclass-ish usage detail object (coerced via ``asdict``)."""

    reasoning_tokens: int = 7
    accepted_prediction_tokens: int = 0


class _UsageLike:
    """Mimics litellm's ``Usage`` — a plain object with ``__dict__`` and a NESTED exotic
    object (coerced via ``__dict__`` + recursion). msgspec cannot natively encode it."""

    def __init__(self) -> None:
        self.prompt_tokens = 11
        self.completion_tokens = 23
        self.total_tokens = 34
        self.completion_tokens_details = _CompletionTokensDetailsWrapper()
        # A set + tuple to prove non-dict containers are coerced too.
        self.flags = {"cached", "streamed"}
        self.span = (1, 2, 3)


def _exotic_payload() -> dict[str, Any]:
    """An lm.call-shaped payload whose ``usage`` is the non-native object that broke
    the strict msgpack encode in production."""
    return {
        "model": "qwopus",
        "usage": _UsageLike(),
        "history": [{"role": "user", "content": "hi"}],
    }


def _events_types(arc: ARCMemory, sid: str) -> list[str]:
    return [s.content["event_type"] for s in arc.render_segments(sid, EVENTS_SCOPE)]


# ---------------------------------------------------------------------------
# (a) record_semantic_event with an exotic payload PERSISTS to _events and the
#     persisted segments round-trip through encode/decode.
# ---------------------------------------------------------------------------
def test_exotic_payload_persists_and_round_trips(tmp_path):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    sid = "sess_exotic"
    event = SemanticEvent(
        event_type="lm.call",
        session_id=sid,
        trace_id="tr1",
        turn_id="t1",
        payload=_exotic_payload(),
    )

    arc.record_semantic_event(event)

    # Persisted to the _events log.
    segs = arc.render_segments(sid, EVENTS_SCOPE)
    assert _events_types(arc, sid) == ["lm.call"]

    # The persisted content is lean + native: usage coerced to a plain dict, the
    # nested wrapper to a dict, the set/tuple to lists.
    usage = segs[0].content["payload"]["usage"]
    assert isinstance(usage, dict)
    assert usage["prompt_tokens"] == 11
    assert usage["completion_tokens_details"]["reasoning_tokens"] == 7
    assert isinstance(usage["flags"], list)
    assert sorted(usage["flags"]) == ["cached", "streamed"]
    assert usage["span"] == [1, 2, 3]

    # Round-trips through the SAME strict msgpack path ARC persists with.
    raw = encode_segments(list(segs))
    decoded = decode_segments(raw)
    assert [s.content["event_type"] for s in decoded] == ["lm.call"]
    assert decoded[0].content["payload"]["usage"]["total_tokens"] == 34


# ---------------------------------------------------------------------------
# (b) the persist does NOT fire the ARC-EVENTS 'FAILED' trace.event (no drop).
# ---------------------------------------------------------------------------
def test_exotic_payload_no_failed_trace_event(tmp_path, monkeypatch):
    import clio_agent.arc.memory as memory_mod

    failed: list[tuple[Any, ...]] = []
    orig_event = memory_mod.trace.event

    def _spy(tag: str, fmt: str, *args: Any) -> None:
        if tag == "ARC-EVENTS" and "FAILED" in fmt:
            failed.append((fmt, *args))
        orig_event(tag, fmt, *args)

    monkeypatch.setattr(memory_mod.trace, "event", _spy)

    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    sid = "sess_nofail"
    event = SemanticEvent(
        event_type="react.step.completed",
        session_id=sid,
        trace_id="tr1",
        turn_id="t1",
        payload=_exotic_payload(),
    )

    arc.record_semantic_event(event)

    assert _events_types(arc, sid) == ["react.step.completed"]
    assert failed == [], f"persist logged an ARC-EVENTS FAILED drop: {failed}"


# ---------------------------------------------------------------------------
# (c) build_event_content's output is fully msgpack-encodable for a payload with
#     nested exotic objects (the central guarantee, independent of ARC wiring).
# ---------------------------------------------------------------------------
def test_build_event_content_is_encodable_for_nested_exotic():
    event = SemanticEvent(
        event_type="tool.call.completed",
        session_id="sess_c",
        trace_id="tr1",
        turn_id="t1",
        actor={"tool": "hdf5", "usage": _UsageLike()},
        provider={"name": "lmstudio", "raw": _CompletionTokensDetailsWrapper()},
        payload=_exotic_payload(),
    )

    content = build_event_content(event)
    assert content is not None

    # The decisive guarantee: a Segment carrying this content encodes with the strict
    # msgpack path (would raise TypeError for an un-coerced exotic object) and round-trips.
    seg = Segment(
        session_id="sess_c",
        scope=EVENTS_SCOPE,
        kind="semantic_event",
        content=content,
        step=-1,
        order=0.0,
        logical_time=0,
    )
    raw = encode_segments([seg])
    decoded = decode_segments(raw)
    assert decoded[0].content["actor"]["usage"]["total_tokens"] == 34
    assert decoded[0].content["provider"]["raw"]["reasoning_tokens"] == 7


# ---------------------------------------------------------------------------
# _encode_safe unit coverage: every coercion strategy + native pass-through.
# ---------------------------------------------------------------------------
def test_encode_safe_coercion_strategies():
    # Native scalars pass through unchanged.
    assert _encode_safe("s") == "s"
    assert _encode_safe(3) == 3
    assert _encode_safe(1.5) == 1.5
    assert _encode_safe(True) is True
    assert _encode_safe(None) is None

    # Containers recurse; non-str keys -> str; set/tuple -> list.
    assert _encode_safe({1: "a"}) == {"1": "a"}
    assert _encode_safe((1, 2)) == [1, 2]
    assert sorted(_encode_safe({"x", "y"})) == ["x", "y"]

    # dataclass -> asdict; __dict__ object -> dict (recursively coerced).
    assert _encode_safe(_CompletionTokensDetailsWrapper())["reasoning_tokens"] == 7
    safe_usage = _encode_safe(_UsageLike())
    assert safe_usage["completion_tokens_details"]["reasoning_tokens"] == 7
    assert isinstance(safe_usage["flags"], list)

    # A truly foreign object with no dict-like surface falls back to str().
    obj = object()
    assert _encode_safe(obj) == str(obj)
