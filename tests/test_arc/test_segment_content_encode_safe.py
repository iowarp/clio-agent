"""Every GENERIC segment write must be encode-safe at the ONE chokepoint, and a
persist-failure must be NON-POISONING.

Background: ARC's durable record (``Segment`` content) is encoded with strict
msgspec/msgpack. The semantic-event ``_events`` log was already guarded inside
``build_event_content``, but the GENERIC working-set / live-context-plane writes that
the ReAct loop reads its prompt FROM (thought / tool_call / observation segments, via
``SegmentStore.append`` / ``insert`` / ``summarize`` / ``replace``) were NOT: a nested
non-msgpack value in ``content`` would throw at ``_persist``, AND — because the bad
Segment was already appended to the in-memory scope list before the encode — would
DURABLY POISON the scope (every subsequent persist of that ``(session, scope)``
re-encodes it and re-throws).

The fix runs every op's incoming ``content`` through ``_encode_safe`` at a single
chokepoint (:func:`clio_agent.arc.segments._coerce_content`) BEFORE constructing the
Segment, so the strict encode can never throw on content; and makes ``_persist``
defensively non-poisoning (a segment that still fails to encode is removed from the
in-memory list + logged via ``runtime.trace`` 'SEGMENT-DROP', never silently). These
tests are fully offline + deterministic: they construct content with exotic objects
directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import msgspec
import pytest

from clio_agent.arc.live import EVENTS_SCOPE, _encode_safe
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import Segment, decode_segments, encode_segments
from clio_agent.arc.segments import SegmentStore
from clio_agent.arc.storage import LocalFSStore


# ---------------------------------------------------------------------------
# Exotic, NON-msgpack-native object shapes (no third-party dep): msgspec's strict
# encode cannot natively serialize either, mirroring litellm usage objects.
# ---------------------------------------------------------------------------
@dataclass
class _Detail:
    reasoning_tokens: int = 7
    accepted: int = 0


class _Usage:
    """Plain object with ``__dict__`` + a NESTED dataclass + set/tuple."""

    def __init__(self) -> None:
        self.prompt_tokens = 11
        self.total_tokens = 34
        self.detail = _Detail()
        self.flags = {"cached", "streamed"}
        self.span = (1, 2, 3)


class _Unencodable:
    """An object whose ``__dict__`` is EMPTY and which is not dict-like, so it always
    falls through to ``str(value)`` in ``_encode_safe`` — used to prove the chokepoint
    coerces even the worst case (and that a raw, un-coerced one would poison)."""

    __slots__ = ()


def _store(tmp_path) -> SegmentStore:
    return SegmentStore(LocalFSStore(str(tmp_path / "arc")))


# ---------------------------------------------------------------------------
# (a) append with content carrying a nested NON-msgpack object persists + round-trips.
# ---------------------------------------------------------------------------
def test_append_nested_exotic_content_persists_and_round_trips(tmp_path):
    store = _store(tmp_path)
    sid, scope = "sess_a", "agentA"

    # An observation-shaped content whose value is a nested exotic object.
    store.append(sid, scope, "observation", {"text": "ok", "usage": _Usage()})

    live = store.render(sid, scope)
    assert len(live) == 1
    usage = live[0].content["usage"]
    assert isinstance(usage, dict)
    assert usage["prompt_tokens"] == 11
    assert usage["detail"]["reasoning_tokens"] == 7
    assert isinstance(usage["flags"], list)
    assert usage["span"] == [1, 2, 3]

    # Round-trips through the SAME strict msgpack path the store persists with.
    raw = encode_segments(live)
    decoded = decode_segments(raw)
    assert decoded[0].content["usage"]["total_tokens"] == 34

    # And a fresh store re-loading the persisted record sees it (durable, no throw).
    store2 = _store(tmp_path)
    reloaded = store2.render(sid, scope)
    assert reloaded[0].content["usage"]["total_tokens"] == 34


# ---------------------------------------------------------------------------
# (c) tool_call-shaped content with EXOTIC args persists (the gact app's
#     {'name': .., 'args': pred.next_tool_args} write path).
# ---------------------------------------------------------------------------
def test_tool_call_content_with_exotic_args_persists(tmp_path):
    store = _store(tmp_path)
    sid, scope = "sess_tc", "agentB"

    store.append(
        sid,
        scope,
        "tool_call",
        {"name": "hdf5_read", "args": {"path": "/x.h5", "opts": _Usage(), "tags": {"a", "b"}}},
    )

    live = store.render(sid, scope)
    assert live[0].content["name"] == "hdf5_read"
    args = live[0].content["args"]
    assert args["path"] == "/x.h5"
    assert args["opts"]["total_tokens"] == 34
    assert isinstance(args["tags"], list)

    # Strict encode round-trip (the persist path) succeeds.
    decoded = decode_segments(encode_segments(live))
    assert decoded[0].content["args"]["opts"]["detail"]["reasoning_tokens"] == 7


# ---------------------------------------------------------------------------
# insert / replace / summarize all coerce content at the same chokepoint.
# ---------------------------------------------------------------------------
def test_insert_replace_summarize_coerce_exotic_content(tmp_path):
    store = _store(tmp_path)
    sid, scope = "sess_ops", "agentC"

    base = store.append(sid, scope, "thought", {"text": "t0"})
    store.insert(sid, scope, 0, "observation", {"text": "obs", "u": _Usage()})
    repl = store.replace(sid, scope, base.id, {"text": "t0'", "u": _Usage()})
    assert repl is not None
    store.summarize(sid, scope, [repl.id], {"text": "summary", "u": _Usage()})

    # Everything persisted; the whole scope encodes cleanly.
    all_segs = store.list_segments(sid, scope, include_tombstoned=True)
    decode_segments(encode_segments(all_segs))  # would raise if any content un-coerced
    for s in all_segs:
        if "u" in s.content:
            assert s.content["u"]["total_tokens"] == 34


# ---------------------------------------------------------------------------
# (b) NON-POISONING: a segment that STILL fails to encode (e.g. a raw Segment forced
#     into the in-memory list) is dropped (logged, never silent) and subsequent
#     appends+persists to the SAME scope still succeed — no durable wedge.
# ---------------------------------------------------------------------------
def test_poison_segment_does_not_durably_wedge_scope(tmp_path, monkeypatch):
    import clio_agent.arc.segments as seg_mod

    store = _store(tmp_path)
    sid, scope = "sess_poison", "agentD"

    # First, a clean append establishes the scope + loads it.
    store.append(sid, scope, "thought", {"text": "good-1"})

    # Forge a POISON segment whose content holds a raw un-encodable object and splice
    # it directly into the in-memory list, bypassing the _coerce_content chokepoint —
    # simulating a hypothetical write that slipped an un-encodable value through.
    poison = Segment(
        scope=scope,
        kind="observation",
        content={"raw": _Unencodable()},  # strict msgpack cannot encode this
        session_id=sid,
        step=0,
        order=99.0,
        logical_time=10_000,
    )
    # Sanity: this segment genuinely does NOT encode (so the test exercises the guard).
    with pytest.raises((TypeError, msgspec.MsgspecError)):
        encode_segments([poison])

    segs = store._segs(sid, scope)  # loaded in-memory list for the scope
    segs.append(poison)
    store._index.add(sid, scope, poison)

    drops: list[tuple[Any, ...]] = []
    orig_event = seg_mod.trace.event

    def _spy(tag: str, fmt: str, *args: Any) -> None:
        if tag == "SEGMENT-DROP":
            drops.append((fmt, *args))
        orig_event(tag, fmt, *args)

    monkeypatch.setattr(seg_mod.trace, "event", _spy)

    # The NEXT append triggers a persist of the whole scope. Without the guard this
    # would throw on the poison segment AND keep re-throwing forever (durable wedge).
    store.append(sid, scope, "thought", {"text": "good-2"})

    # The poison was dropped (logged, not silent) and the good segments survive.
    assert drops, "poison segment was not logged via SEGMENT-DROP"
    live_texts = [s.content.get("text") for s in store.render(sid, scope)]
    assert "good-1" in live_texts
    assert "good-2" in live_texts
    assert all("raw" not in s.content for s in store.render(sid, scope))

    # The scope is NOT wedged: another append+persist still succeeds, and a fresh
    # store reloads the persisted (clean) record.
    store.append(sid, scope, "thought", {"text": "good-3"})
    store2 = _store(tmp_path)
    reloaded = [s.content.get("text") for s in store2.render(sid, scope)]
    assert "good-3" in reloaded

    # Index stays consistent with the scan after the drop (no orphan locator entry).
    assert store._index_matches_scan(sid, scope)


# ---------------------------------------------------------------------------
# The _events log path is ALSO routed through the same chokepoint (belt-and-suspenders
# with build_event_content's own _encode_safe), via the observer/ARC writer.
# ---------------------------------------------------------------------------
def test_events_scope_segment_also_coerced(tmp_path):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    sid = "sess_ev"
    # Append a semantic_event-shaped segment whose content carries an exotic object
    # directly through the generic append surface (the chokepoint).
    arc.append_segment(sid, EVENTS_SCOPE, "semantic_event", {"event_type": "x", "u": _Usage()})
    segs = arc.render_segments(sid, EVENTS_SCOPE)
    assert segs[0].content["u"]["total_tokens"] == 34
    decode_segments(encode_segments(list(segs)))  # encodes cleanly


# ---------------------------------------------------------------------------
# _encode_safe is the SAME object whether imported from arc.live or arc.segments
# (re-export, one implementation — no drift).
# ---------------------------------------------------------------------------
def test_encode_safe_single_implementation():
    from clio_agent.arc.live import _encode_safe as from_live
    from clio_agent.arc.segments import _encode_safe as from_segments

    assert from_live is from_segments
    assert _encode_safe is from_segments
