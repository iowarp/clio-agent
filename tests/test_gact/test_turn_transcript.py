"""Unit tests for the TurnTranscript single-writer part ledger (#767 PR1).

Covers the design's §8.1 unit surface: open/delta/close lifecycle,
``(agent, field)`` part splits, whitespace-only drops, atomic-append
boundaries, once-key idempotency, 1-based arrival-order sequence,
verbatim whole-buffer storage (#881 — no visible-text cleaner), first-producer
message minting, the FieldStream truth table, late-op auditing, thread
interleaving, the non-blocking ``EventBus.publish`` precondition — plus the
§8.2(a) transcript-level live==reload fold property over randomized op
interleavings.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from types import SimpleNamespace
from typing import Any

from clio_agent.gact.events import Event, EventBus
from clio_agent.gact.transcript import (
    EventBusTranscriptPublisher,
    TurnTranscript,
    TurnTranscriptRegistry,
)
from clio_agent.gact.types import Part

# A marker the MODEL might write in its own prose. Since #881 the transcript
# stores text verbatim, so this survives to the wire unedited — the tests below
# assert exactly that (it is no longer a "contract line to strip").
CONTRACT_MARKER = "[[CONTRACT]]"


class RecordingPublisher:
    """Thread-safe (event_type, payload) recorder standing in for the bus."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[tuple[str, dict[str, Any]]] = []

    def publish(self, event_type: str, payload: Any) -> None:
        with self._lock:
            self.events.append((event_type, dict(payload)))

    def of_type(self, *event_types: str) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            return [(t, p) for (t, p) in self.events if t in event_types]


def make_transcript(
    publisher: RecordingPublisher | None = None,
) -> tuple[TurnTranscript, RecordingPublisher]:
    publisher = publisher or RecordingPublisher()
    transcript = TurnTranscript(
        session_id="sess_t",
        turn_id="turn_t",
        publisher=publisher,
    )
    return transcript, publisher


def tool_part(part_id: str = "", agent: str = "data") -> Part:
    return Part(
        id=part_id,
        type="tool_call",
        agent_id=agent,
        call_id=f"call_{part_id or 'x'}",
        tool_name="fs_read_file",
        input={"path": "README.md"},
        metadata={"stream_source": "live"},
    )


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


def test_ensure_message_mints_once_whichever_producer_arrives_first() -> None:
    # Producer A: streamed delta first.
    transcript, publisher = make_transcript()
    transcript.append_text_delta("main", "answer", "hello")
    transcript.append_part(tool_part("p1"))
    created = publisher.of_type("message.created")
    assert len(created) == 1
    assert created[0][1]["id"] == transcript.message_id
    assert created[0][1]["turn_id"] == "turn_t"
    assert created[0][1]["parts"] == []

    # Producer B: tool part first.
    transcript2, publisher2 = make_transcript()
    transcript2.append_part(tool_part("p1"))
    transcript2.append_text_delta("main", "answer", "hello")
    assert len(publisher2.of_type("message.created")) == 1

    # Explicit ensure_message is idempotent too.
    assert transcript.ensure_message() == transcript.message_id
    assert len(publisher.of_type("message.created")) == 1


# ---------------------------------------------------------------------------
# open / delta / close lifecycle
# ---------------------------------------------------------------------------


def test_text_delta_lifecycle_publishes_added_delta_completed() -> None:
    transcript, publisher = make_transcript()
    transcript.append_text_delta("main", "answer", "Hello ")
    transcript.append_text_delta("main", "answer", "world")
    transcript.close_open_text()

    added = publisher.of_type("message.part.added")
    deltas = publisher.of_type("message.part.delta")
    completed = publisher.of_type("message.part.completed")
    assert len(added) == 1
    part_wire = added[0][1]["part"]
    assert part_wire["type"] == "text"
    assert part_wire["agent_id"] == "main"
    assert part_wire["metadata"]["signature_field_name"] == "answer"
    assert added[0][1]["stream_source"] == "live"
    assert [d[1]["delta"]["text_append"] for d in deltas] == ["Hello ", "world"]
    assert all(d[1]["part_id"] == part_wire["id"] for d in deltas)
    assert len(completed) == 1
    assert completed[0][1]["final_text"] == "Hello world"
    assert completed[0][1]["stream_source"] == "live"

    # The ledger part carries the closed text (mutate-in-place close).
    parts = transcript.snapshot()
    assert len(parts) == 1
    assert parts[0].text == "Hello world"

    # #767 PR5: the normalized turn.text.delta twin is retired — message.part.*
    # is the sole transcript wire vocabulary.
    assert publisher.of_type("turn.text.delta") == []


def test_agent_or_field_change_splits_parts_and_closes_prior() -> None:
    transcript, publisher = make_transcript()
    transcript.append_text_delta("main", "reasoning", "thinking...")
    transcript.append_text_delta("main", "answer", "the answer")
    transcript.append_text_delta("child", "answer", "child says")
    transcript.close_open_text()

    added = publisher.of_type("message.part.added")
    completed = publisher.of_type("message.part.completed")
    assert len(added) == 3
    assert len(completed) == 3
    # Prior part closes BEFORE the next opens (wire order).
    types_in_order = [t for t, _ in publisher.events if t.startswith("message.part.")]
    assert types_in_order.index("message.part.completed") < len(types_in_order) - 1
    parts = transcript.snapshot()
    assert [(p.agent_id, p.metadata["signature_field_name"]) for p in parts] == [
        ("main", "reasoning"),
        ("main", "answer"),
        ("child", "answer"),
    ]
    # #767 PR5: no normalized turn.text.delta twin is published anymore.
    assert publisher.of_type("turn.text.delta") == []


def test_provider_thinking_opens_thinking_part_verbatim() -> None:
    transcript, publisher = make_transcript()
    transcript.append_text_delta("main", "provider_thinking:anthropic", "raw ")
    transcript.append_text_delta("main", "provider_thinking:anthropic", f"{CONTRACT_MARKER} kept")
    transcript.close_open_text()

    parts = transcript.snapshot()
    assert len(parts) == 1
    assert parts[0].type == "thinking"
    assert parts[0].metadata["thinking_source"] == "provider"
    assert parts[0].metadata["provider_source"] == "anthropic"
    assert parts[0].metadata["default_collapsed"] is True
    # Verbatim: provider thinking is stored byte-for-byte (no cleaner exists).
    assert parts[0].text == f"raw {CONTRACT_MARKER} kept"
    # #767 PR5: neither the turn.trace.delta nor the turn.text.delta twin is
    # published anymore — the thinking part rides message.part.* only.
    assert publisher.of_type("turn.trace.delta") == []
    assert publisher.of_type("turn.text.delta") == []


def test_streamed_text_is_stored_verbatim_at_close() -> None:
    """#881: the close stores the WHOLE streamed buffer BYTE-FOR-BYTE — the server
    binds no visible-text cleaner, so a marker-looking line the MODEL wrote in its
    own prose survives unedited. ``finalize`` never rewrites text."""
    transcript, publisher = make_transcript()
    transcript.append_text_delta("main", "answer", "keep me\n")
    transcript.append_text_delta("main", "answer", f"{CONTRACT_MARKER} the model wrote this\n")
    transcript.append_text_delta("main", "answer", "and me")
    transcript.close_open_text()
    transcript.close_open_text()  # idempotent
    transcript.finalize()  # finalize never rewrites text

    verbatim = f"keep me\n{CONTRACT_MARKER} the model wrote this\nand me"
    completed = publisher.of_type("message.part.completed")
    assert len(completed) == 1
    assert completed[0][1]["final_text"] == verbatim
    assert transcript.snapshot()[0].text == verbatim


def test_whitespace_only_part_is_dropped_and_emits_nothing() -> None:
    """The ONLY close-time drop that remains (#881): a part that is whitespace-only
    after buffering carries no content, so it is removed and the close emits
    nothing. Any non-whitespace content is always kept verbatim (test above)."""
    transcript, publisher = make_transcript()
    transcript.append_text_delta("main", "answer", "   \n")
    transcript.append_text_delta("main", "answer", "  \t")
    transcript.close_open_text()

    assert transcript.snapshot() == []
    assert publisher.of_type("message.part.completed") == []
    # The part.added + deltas already went out (live streaming is honest),
    # but the close emits nothing and the ledger no longer holds the part.
    assert len(publisher.of_type("message.part.added")) == 1
    assert not transcript.has_closed_text("main", "answer")


def test_append_part_closes_open_text_first_and_assigns_id() -> None:
    transcript, publisher = make_transcript()
    transcript.append_text_delta("main", "answer", "before tool")
    appended = transcript.append_part(tool_part(""))
    assert appended is not None
    assert appended.id.startswith("part_")

    kinds = [t for t, _ in publisher.events if t.startswith("message.part.")]
    # text added, delta, then completed (the boundary close) BEFORE tool added.
    assert kinds == [
        "message.part.added",
        "message.part.delta",
        "message.part.completed",
        "message.part.added",
    ]
    assert [p.type for p in transcript.snapshot()] == ["text", "tool_call"]
    assert transcript.open_text_part() is None


def test_append_part_stream_source_prefers_part_metadata() -> None:
    transcript, publisher = make_transcript()
    part = tool_part("p_meta")
    part.metadata["stream_source"] = "live"
    transcript.append_part(part, stream_source="batch")
    plain = Part(id="p_plain", type="routing_decision", agent_id="main", selected_agent="data")
    transcript.append_part(plain)
    added = publisher.of_type("message.part.added")
    assert added[0][1]["stream_source"] == "live"
    assert added[1][1]["stream_source"] == "live"  # default


def test_append_part_once_is_idempotent_per_turn() -> None:
    transcript, publisher = make_transcript()
    first = transcript.append_part_once("route:data", tool_part("p1"))
    dup = transcript.append_part_once("route:data", tool_part("p2"))
    assert first is not None
    assert dup is None
    assert transcript.has_part_key("route:data")
    assert not transcript.has_part_key("route:other")
    assert len(publisher.of_type("message.part.added")) == 1
    assert len(transcript.snapshot()) == 1


def test_annotate_merges_metadata_and_publishes_patch_without_text_change() -> None:
    transcript, publisher = make_transcript()
    part = transcript.append_part(tool_part("p1"))
    assert part is not None
    transcript.annotate(part.id, result_preview="42 rows", stream_fallback={"reason": "x"})
    assert part.metadata["result_preview"] == "42 rows"
    assert part.metadata["stream_source"] == "live"  # merge, not replace
    patches = publisher.of_type("message.part.updated")
    assert len(patches) == 1
    assert patches[0][1]["part_id"] == part.id
    assert patches[0][1]["metadata_patch"]["result_preview"] == "42 rows"

    # Unknown part: audited no-op, nothing published.
    transcript.annotate("part_missing", foo="bar")
    assert len(publisher.of_type("message.part.updated")) == 1


# ---------------------------------------------------------------------------
# state queries
# ---------------------------------------------------------------------------


def test_streamed_text_and_has_closed_text_are_identity_checks() -> None:
    transcript, _ = make_transcript()
    transcript.append_text_delta("child", "answer", "part one ")
    transcript.append_text_delta("child", "answer", "part two")
    assert transcript.streamed_text("child", "answer") == "part one part two"
    assert transcript.streamed_text("child", "reasoning") == ""
    assert not transcript.has_closed_text("child", "answer")
    transcript.close_open_text()
    assert transcript.has_closed_text("child", "answer")
    assert not transcript.has_closed_text("main", "answer")


# ---------------------------------------------------------------------------
# finalize + late ops
# ---------------------------------------------------------------------------


def test_finalize_closes_open_text_stamps_sequence_and_freezes() -> None:
    transcript, publisher = make_transcript()
    transcript.append_part_once("route:data", tool_part("p_route"))
    transcript.append_text_delta("data", "answer", "final words")
    parts = transcript.finalize()

    assert [p.sequence for p in parts] == [1, 2]
    assert parts[1].text == "final words"
    assert len(publisher.of_type("message.part.completed")) == 1
    assert transcript.frozen
    # Idempotent: same frozen ledger, no new events.
    events_before = len(publisher.events)
    assert transcript.finalize() == parts
    assert len(publisher.events) == events_before


def test_late_appends_after_finalize_are_rejected_and_audited(monkeypatch: Any) -> None:
    audits: list[tuple[str, dict[str, Any]]] = []

    def spy_audit(stage: str, **fields: Any) -> None:
        audits.append((stage, fields))

    monkeypatch.setattr("clio_agent.gact.transcript.stream_audit", spy_audit)
    transcript, publisher = make_transcript()
    transcript.append_text_delta("main", "answer", "done")
    transcript.finalize()
    events_before = len(publisher.events)

    assert transcript.append_part(tool_part("p_late")) is None
    transcript.append_text_delta("main", "answer", "late chunk")
    assert transcript.append_part_once("late:key", tool_part("p_late2")) is None
    transcript.annotate(transcript.snapshot()[0].id, late="fact")

    assert len(publisher.events) == events_before  # nothing hit the wire
    assert len(transcript.snapshot()) == 1  # nothing absorbed into the ledger
    late_ops = [fields["op"] for stage, fields in audits if stage == "transcript.late_op"]
    assert late_ops == ["append_part", "append_text_delta", "append_part_once", "annotate"]


# ---------------------------------------------------------------------------
# FieldStream truth table (design §3.2)
# ---------------------------------------------------------------------------


def test_field_stream_streamed_then_finish_ignores_fallback(monkeypatch: Any) -> None:
    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "clio_agent.gact.transcript.stream_audit",
        lambda stage, **fields: audits.append((stage, fields)),
    )
    transcript, publisher = make_transcript()
    stream = transcript.field_stream("main", "answer")
    assert stream.part_id is None
    stream.append("streamed ")
    stream.append("answer")
    assert stream.part_id is not None
    final = stream.finish(fallback_text="a DIFFERENT batch copy")

    assert final == "streamed answer"
    parts = transcript.snapshot()
    assert len(parts) == 1
    assert parts[0].text == "streamed answer"
    # The batch copy was dropped by IDENTITY (this handle streamed), audited.
    ignored = [fields for stage, fields in audits if stage.endswith("fallback_ignored")]
    assert len(ignored) == 1
    assert ignored[0]["reason"] == "already_streamed"
    assert len(publisher.of_type("message.part.added")) == 1


def test_field_stream_no_deltas_lands_one_batch_burst() -> None:
    transcript, publisher = make_transcript()
    stream = transcript.field_stream("main", "answer")
    # #881: the batch fallback lands VERBATIM (no cleaner) — a marker-looking line
    # the model wrote survives intact.
    final = stream.finish(fallback_text=f"batch answer\n{CONTRACT_MARKER} kept")

    assert final == f"batch answer\n{CONTRACT_MARKER} kept"
    parts = transcript.snapshot()
    assert len(parts) == 1
    assert parts[0].text == f"batch answer\n{CONTRACT_MARKER} kept"
    assert parts[0].metadata["stream_source"] == "batch"
    added = publisher.of_type("message.part.added")
    completed = publisher.of_type("message.part.completed")
    assert len(added) == 1
    assert added[0][1]["stream_source"] == "batch"
    assert len(completed) == 1
    assert completed[0][1]["stream_source"] == "batch"
    assert completed[0][1]["final_text"] == f"batch answer\n{CONTRACT_MARKER} kept"
    # Batch bursts NEVER emit synthetic deltas (matches today's finalize shape).
    assert publisher.of_type("message.part.delta") == []
    assert stream.part_id == parts[0].id
    assert transcript.has_closed_text("main", "answer")


def test_field_stream_neither_streamed_nor_fallback_returns_none() -> None:
    transcript, publisher = make_transcript()
    stream = transcript.field_stream("main", "answer")
    assert stream.finish() is None
    assert stream.finish(fallback_text="") is None  # second finish: audited no-op
    assert transcript.snapshot() == []
    assert publisher.events == []


def test_promote_open_text_field_moves_one_part_without_copying_text() -> None:
    from clio_agent.gact.direct_response import promote_tool_free_response

    transcript, publisher = make_transcript()
    transcript.append_text_delta("main", "next_thought", "READY ONCE")

    assert promote_tool_free_response(
        transcript, SimpleNamespace(termination_reason="direct_response"), ["main"]
    )

    parts = transcript.snapshot()
    assert len(parts) == 1
    assert parts[0].text == "READY ONCE"
    assert parts[0].metadata["signature_field_name"] == "answer"
    assert not transcript.has_closed_text("main", "next_thought")
    assert transcript.has_closed_text("main", "answer")
    updated = publisher.of_type("message.part.updated")
    assert len(updated) == 1
    assert updated[0][1]["part_id"] == parts[0].id
    assert updated[0][1]["metadata_patch"] == {"signature_field_name": "answer"}

    transcript.turn_answer_stream("main").finish(fallback_text="READY ONCE")
    assert len(transcript.snapshot()) == 1


def test_field_stream_survives_runtime_boundary_split() -> None:
    """A tool part between deltas closes the field's part; finish must not
    resurrect the batch copy — the handle DID stream."""

    transcript, publisher = make_transcript()
    stream = transcript.field_stream("main", "answer")
    stream.append("first half")  # #881: stored verbatim, no trailing-space trim
    transcript.append_part(tool_part("p_tool"))  # boundary closes the text part
    stream.append("second half")
    final = stream.finish(fallback_text="first half second half")

    assert final == "second half"
    parts = transcript.snapshot()
    assert [p.type for p in parts] == ["text", "tool_call", "text"]
    assert parts[0].text == "first half"
    assert parts[2].text == "second half"
    assert len(publisher.of_type("message.part.added")) == 3


# ---------------------------------------------------------------------------
# registry lifecycle
# ---------------------------------------------------------------------------


def test_registry_open_get_close_lifecycle() -> None:
    registry = TurnTranscriptRegistry()
    publisher = RecordingPublisher()
    assert registry.get("s1") is None
    transcript = registry.open_turn("s1", "turn_1", publisher)
    assert registry.get("s1") is transcript
    assert registry.get("s2") is None
    registry.close("s1")
    assert registry.get("s1") is None
    registry.close("s1")  # idempotent


def test_registry_evicts_leaked_ledger_loudly(monkeypatch: Any) -> None:
    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "clio_agent.gact.transcript.stream_audit",
        lambda stage, **fields: audits.append((stage, fields)),
    )
    registry = TurnTranscriptRegistry()
    publisher = RecordingPublisher()
    stale = registry.open_turn("s1", "turn_old", publisher)
    fresh = registry.open_turn("s1", "turn_new", publisher)
    assert registry.get("s1") is fresh
    assert fresh is not stale
    leaked = [fields for stage, fields in audits if stage == "transcript.leaked_ledger_evicted"]
    assert len(leaked) == 1
    assert leaked[0]["stale_turn_id"] == "turn_old"


# ---------------------------------------------------------------------------
# live == reload: the fold property (design §8.2a)
# ---------------------------------------------------------------------------


def fold_published_events(events: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Reconstruct the persisted parts list from the published event stream.

    Fold rule: parts arrive via ``message.part.added`` in order; text/thinking
    parts take their final text from ``message.part.completed`` and are DROPPED
    when no completed event ever arrives (the empty-after-clean removal);
    atomic parts persist as added. Sequence = 1-based fold order.
    """

    order: list[str] = []
    added: dict[str, dict[str, Any]] = {}
    stream_sources: dict[str, str] = {}
    final_texts: dict[str, str] = {}
    for event_type, payload in events:
        if event_type == "message.part.added":
            wire = payload["part"]
            order.append(wire["id"])
            added[wire["id"]] = wire
            stream_sources[wire["id"]] = payload["stream_source"]
        elif event_type == "message.part.completed":
            final_texts[payload["part_id"]] = payload["final_text"]
    folded: list[dict[str, Any]] = []
    for part_id in order:
        wire = added[part_id]
        is_streamed_text = wire["type"] in {"text", "thinking"}
        if is_streamed_text and part_id not in final_texts:
            continue  # dropped whitespace-only part (#881): close emitted nothing
        folded.append(
            {
                "id": part_id,
                "sequence": len(folded) + 1,
                "type": wire["type"],
                "agent_id": wire["agent_id"],
                "text": final_texts.get(part_id, wire.get("text", "")),
                "stream_source": stream_sources[part_id],
            }
        )
    return folded


def projected(parts: list[Part]) -> list[dict[str, Any]]:
    return [
        {
            "id": p.id,
            "sequence": p.sequence,
            "type": p.type,
            "agent_id": p.agent_id,
            "text": p.text,
            "stream_source": str(p.metadata.get("stream_source") or "live"),
        }
        for p in parts
    ]


def test_fold_property_random_interleavings() -> None:
    """Any interleaving of append_part / append_text_delta / close_open_text /
    FieldStream.finish folds to exactly the finalize() output."""

    agents = ["main", "geospatial", "data"]
    fields = ["answer", "reasoning", "provider_thinking:anthropic"]
    chunks = ["alpha ", "beta\n", f"{CONTRACT_MARKER} contract\n", "gamma", "  "]

    for seed in range(30):
        rng = random.Random(seed)
        transcript, publisher = make_transcript()
        open_streams: list[Any] = []
        for _ in range(rng.randint(3, 40)):
            roll = rng.random()
            if roll < 0.45:
                transcript.append_text_delta(
                    rng.choice(agents), rng.choice(fields), rng.choice(chunks)
                )
            elif roll < 0.6:
                transcript.append_part(tool_part("", agent=rng.choice(agents)))
            elif roll < 0.7:
                transcript.append_part_once(
                    f"route:{rng.choice(agents)}",
                    Part(
                        id="",
                        type="routing_decision",
                        agent_id="main",
                        selected_agent=rng.choice(agents),
                        metadata={"stream_source": "live"},
                    ),
                )
            elif roll < 0.8:
                transcript.close_open_text()
            elif roll < 0.9:
                stream = transcript.field_stream(rng.choice(agents), "answer")
                if rng.random() < 0.5:
                    stream.append(rng.choice(chunks))
                open_streams.append(stream)
            elif open_streams:
                stream = open_streams.pop()
                stream.finish(fallback_text=rng.choice(["", "batch fallback text"]))
        for stream in open_streams:
            stream.finish(fallback_text=rng.choice(["", "tail fallback"]))
        persisted = transcript.finalize()

        assert fold_published_events(publisher.events) == projected(persisted), (
            f"fold != finalize for seed {seed}"
        )


# ---------------------------------------------------------------------------
# threading + the publish-under-lock precondition
# ---------------------------------------------------------------------------


def test_thread_interleaving_ledger_order_equals_event_order() -> None:
    transcript, publisher = make_transcript()
    n_threads, parts_per_thread, n_chunks = 4, 25, 200
    errors: list[BaseException] = []

    def tool_worker(worker: int) -> None:
        try:
            for i in range(parts_per_thread):
                transcript.append_part(tool_part(f"w{worker}_{i}"))
        except BaseException as exc:  # noqa: BLE001 - surfaced to the assertion
            errors.append(exc)

    threads = [threading.Thread(target=tool_worker, args=(w,)) for w in range(n_threads)]
    for t in threads:
        t.start()
    for i in range(n_chunks):
        transcript.append_text_delta("main", "answer", f"c{i} ")
    for t in threads:
        t.join()
    persisted = transcript.finalize()

    assert errors == []
    tool_ids = [p.id for p in persisted if p.type == "tool_call"]
    assert len(tool_ids) == n_threads * parts_per_thread
    assert len(set(tool_ids)) == len(tool_ids)  # no lost or duplicated parts
    # Every streamed chunk survived into some closed text part.
    text_concat = "".join(p.text for p in persisted if p.type == "text")
    assert text_concat.split() == [f"c{i}" for i in range(n_chunks)]
    # Ledger order == event (bus) order, and the fold property holds under
    # cross-thread interleaving.
    added_order = [p[1]["part"]["id"] for p in publisher.of_type("message.part.added")]
    persisted_order = [p.id for p in persisted]
    assert [pid for pid in added_order if pid in set(persisted_order)] == persisted_order
    assert fold_published_events(publisher.events) == projected(persisted)


async def test_event_bus_publish_is_non_blocking_with_full_queue() -> None:
    """The transcript publishes while holding its ledger lock, which is only
    safe because ``EventBus.publish`` never blocks — even when a slow
    subscriber's queue is full it drops instead of waiting."""

    bus = EventBus(queue_capacity=4)
    agen = bus.subscribe("s1")  # binds the owning loop; consumer never drains
    first = asyncio.ensure_future(agen.__anext__())
    publisher = EventBusTranscriptPublisher(bus, "s1")

    start = time.monotonic()
    for i in range(500):  # far beyond queue_capacity
        publisher.publish("message.part.delta", {"part_id": "p", "i": i})
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"publish blocked for {elapsed:.2f}s with a full subscriber queue"
    first.cancel()
    await agen.aclose()


async def test_event_bus_transcript_publisher_reaches_subscribers() -> None:
    bus = EventBus()
    received: list[Event] = []

    async def consume() -> None:
        async for event in bus.subscribe("s1"):
            received.append(event)
            if len(received) >= 2:
                break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    transcript = TurnTranscript(
        session_id="s1",
        turn_id="turn_1",
        publisher=EventBusTranscriptPublisher(bus, "s1"),
    )
    transcript.append_part(tool_part("p1"))
    await asyncio.wait_for(task, timeout=5)

    assert [e.type for e in received] == ["message.created", "message.part.added"]
    assert received[0].session_id == "s1"
    assert received[1].payload["part"]["id"] == "p1"
