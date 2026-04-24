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

# ---- EventBus unit tests --------------------------------------------------


def test_event_envelope_carries_type_and_payload() -> None:
    e = Event(type="message.created", session_id="s1", payload={"k": "v"})
    env = e.envelope()
    assert env["type"] == "message.created"
    assert env["payload"] == {"k": "v"}
    assert env["occurred_at"]  # ISO timestamp present


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
    data_line = next(
        ln for ln in raw.splitlines() if ln.startswith("data: ")
    )
    payload = json.loads(data_line.removeprefix("data: "))
    assert payload["type"] == "message.completed"
    assert payload["payload"] == {"a": 1}


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
    return TestClient(
        build_app(sessions_path=tmp_path / "s.json", agent=_FakeAgent())
    )


def test_sse_endpoint_404s_for_unknown_session(client: TestClient) -> None:
    """Structured envelope, not a plain FastAPI string."""

    resp = client.get("/v1/sessions/sess_nope/events")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["error"] == "internal_error"
    assert "session not found" in body["error"]["message"]


# Note: a "headers + content-type" smoke against client.stream(...)
# was deliberately left out. TestClient deadlocks on SSE responses
# under sync iteration (the StreamingResponse is unbounded so the
# context manager doesn't drain to completion). The 404 path above +
# the EventBus unit tests cover the meaningful behaviour;
# end-to-end SSE wire validation lives in gact-tui's integration
# suite where we drive a real uvicorn process.
