"""CLIO-BBBBBBBBBB13: tests for /v1/sessions/{sid}/events.

Two-layer testing strategy:

  1. EventBus unit tests — publish/subscribe/cleanup/QueueFull,
     plus the wire envelope shape.
  2. SSE endpoint smoke — assert content-type, 404 for unknown
     session. Streaming-during-POST behaviour is fragile to
     test via TestClient/ASGITransport (both buffer SSE chunks
     unpredictably) and is verified end-to-end in the gact-tui
     integration tests instead, where a real uvicorn process
     hosts the server and gact-tui's SSE client drives it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import _format_sse, build_app
from clio_agent.gact.events import Event, EventBus
from clio_agent.gact.routes.misc import _sse_wire_tap

# ---- EventBus unit tests --------------------------------------------------


def test_event_envelope_carries_type_and_payload() -> None:
    e = Event(type="message.created", session_id="s1", payload={"k": "v"})
    env = e.envelope()
    assert env["type"] == "message.created"
    assert env["payload"] == {"k": "v"}
    assert env["occurred_at"]  # ISO timestamp present
    assert "replay" not in env


def test_replay_event_envelope_is_distinguishable() -> None:
    e = Event(type="message.created", session_id="s1", payload={"k": "v"})
    replay = e.replay_copy()

    assert replay.id == e.id
    assert replay.occurred_at == e.occurred_at
    assert replay.envelope()["replay"] is True


def test_event_ids_are_monotonic() -> None:
    a = Event(type="x", session_id="s", payload={})
    b = Event(type="x", session_id="s", payload={})
    assert b.id > a.id


def test_format_sse_emits_canonical_wire_shape() -> None:
    e = Event(type="message.completed", session_id="s1", payload={"a": 1})
    raw = _format_sse(e).decode("utf-8")
    # Three lines: event/id/data, then a blank separator.
    assert raw.startswith("event: message.completed\n")
    assert f"id: {e.id}\n" in raw
    assert "data: " in raw
    assert raw.endswith("\n\n")
    # The data line is valid JSON matching the envelope.
    data_line = next(ln for ln in raw.splitlines() if ln.startswith("data: "))
    payload = json.loads(data_line.removeprefix("data: "))
    assert payload["type"] == "message.completed"
    assert payload["payload"] == {"a": 1}


def test_sse_wire_tap_writes_timestamped_event_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw_path = tmp_path / "sse.raw"
    event_log_path = tmp_path / "sse.events.jsonl"
    audit_path = tmp_path / "stream-audit.jsonl"
    monkeypatch.setenv("CLIO_SSE_WIRE_TAP", str(raw_path))
    monkeypatch.setenv("CLIO_SSE_EVENT_LOG", str(event_log_path))
    monkeypatch.setenv("CLIO_STREAM_AUDIT_LOG", str(audit_path))
    event = Event(type="message.part.delta", session_id="sess_1", payload={"turn_id": "t1"})
    frame = _format_sse(event)

    _sse_wire_tap("sess_1", frame, event)

    assert raw_path.read_bytes() == frame
    row = json.loads(event_log_path.read_text(encoding="utf-8"))
    assert row["session_id"] == "sess_1"
    assert row["event_id"] == event.id
    assert row["event_type"] == "message.part.delta"
    assert row["event_occurred_at"] == event.occurred_at
    assert row["sse_written_at"]
    assert row["frame_bytes"] == len(frame)

    audit_row = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit_row["stage"] == "sse.write"
    assert audit_row["event_id"] == event.id


@pytest.mark.asyncio
async def test_eventbus_subscribe_receives_published_events() -> None:
    bus = EventBus()
    received: list[Event] = []

    async def reader() -> None:
        async for ev in bus.subscribe("s1"):
            received.append(ev)
            if len(received) >= 3:
                break

    task = asyncio.create_task(reader())
    # Yield so the subscriber registers.
    await asyncio.sleep(0)
    bus.publish(Event(type="a", session_id="s1", payload={}))
    bus.publish(Event(type="b", session_id="s1", payload={}))
    bus.publish(Event(type="c", session_id="s1", payload={}))
    await asyncio.wait_for(task, timeout=2.0)

    assert [e.type for e in received] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_eventbus_only_delivers_to_matching_session() -> None:
    bus = EventBus()
    s1_received: list[str] = []

    async def reader() -> None:
        async for ev in bus.subscribe("s1"):
            s1_received.append(ev.type)
            break

    task = asyncio.create_task(reader())
    await asyncio.sleep(0)
    # Publishing to a different session MUST NOT wake up our s1 sub.
    bus.publish(Event(type="other", session_id="s_other", payload={}))
    bus.publish(Event(type="mine", session_id="s1", payload={}))
    await asyncio.wait_for(task, timeout=2.0)

    assert s1_received == ["mine"]


@pytest.mark.asyncio
async def test_eventbus_global_events_fan_out_to_session_subscribers() -> None:
    bus = EventBus()
    received: list[str] = []

    async def reader() -> None:
        async for ev in bus.subscribe("s1"):
            received.append(ev.type)
            if len(received) >= 2:
                break

    task = asyncio.create_task(reader())
    await asyncio.sleep(0)

    bus.publish(Event(type="lm.provider.changed", session_id="", payload={}))
    bus.publish(Event(type="message.created", session_id="s1", payload={}))

    await asyncio.wait_for(task, timeout=2.0)

    assert received == ["lm.provider.changed", "message.created"]


@pytest.mark.asyncio
async def test_eventbus_replays_global_and_session_history_as_replay_events() -> None:
    bus = EventBus()
    bus.publish(Event(type="lm.provider.changed", session_id="", payload={}))
    bus.publish(Event(type="message.created", session_id="s1", payload={}))

    received: list[Event] = []
    sub = bus.subscribe("s1")
    async for ev in sub:
        received.append(ev)
        if len(received) >= 2:
            await sub.aclose()
            break

    assert [event.type for event in received] == [
        "lm.provider.changed",
        "message.created",
    ]
    assert all(event.replay for event in received)


@pytest.mark.asyncio
async def test_eventbus_cleans_up_subscriber_on_drop() -> None:
    bus = EventBus()

    async def reader() -> None:
        # Bind the generator + close it explicitly so the finally
        # block runs synchronously with the test (don't rely on GC
        # timing).
        sub = bus.subscribe("s1")
        async for _ in sub:
            await sub.aclose()
            break

    task = asyncio.create_task(reader())
    # Yield so the subscriber registers.
    await asyncio.sleep(0)
    assert bus.subscriber_count("s1") == 1

    bus.publish(Event(type="x", session_id="s1", payload={}))
    await asyncio.wait_for(task, timeout=2.0)
    # Yield once more so the generator's finally has a chance to
    # run after the await aclose() resolves.
    await asyncio.sleep(0)

    assert bus.subscriber_count("s1") == 0


# ---- SSE endpoint smoke ---------------------------------------------------


@dataclass
class _FakePred:
    answer: str = "ok"
    selected_expert: str = "data"
    routing_rationale: str = ""


class _FakeAgent:
    def forward(self, question: str, session_id: str) -> Any:
        return _FakePred()


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_FakeAgent()))


def test_sse_endpoint_404s_for_unknown_session(client: TestClient) -> None:
    """Structured envelope, not a plain FastAPI string."""

    resp = client.get("/v1/sessions/sess_nope/events")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["error"] == "not_found"
    assert "session not found" in body["error"]["message"]


# Note: a "headers + content-type" smoke against client.stream(...)
# was deliberately left out. TestClient deadlocks on SSE responses
# under sync iteration (the StreamingResponse is unbounded so the
# context manager doesn't drain to completion). The 404 path above +
# the EventBus unit tests cover the meaningful behaviour;
# end-to-end SSE wire validation lives in gact-tui's integration
# suite where we drive a real uvicorn process.
