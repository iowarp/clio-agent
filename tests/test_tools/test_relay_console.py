"""#1231 Part 2: relay console-tail pull-and-fold (the owner module relay_console.py).

Unit-level acceptance against a FAKE HTTP log endpoint (``httpx.MockTransport`` --
real HTTP request/response semantics, no live relay). The end-to-end wiring
through ``RelayTransportClient.wait()`` -> #1115's poll loop -> ``store.put`` ->
the ``mcp_task.updated`` SSE fan-out is covered separately in
``test_relay_transport.py`` (the module that owns the fake in-process relay
server); this file exercises ``relay_console`` in isolation so a failure here
points straight at the fold/config/error-handling logic and never at the
transport plumbing.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from clio_agent.tools.mcp_task_records import (
    InMemoryTaskRecordStore,
    TaskKey,
    TaskRecord,
)
from clio_agent.tools.relay_console import (
    CONSOLE_TAIL_TRUNCATED_REASON,
    RELAY_LOG_PULL_HARD_CAP_BYTES,
    console_enabled,
    console_pull_limit_bytes,
    console_tail_cap_bytes,
    make_console_on_poll,
)


class _FakeRelayHttpClient:
    """Minimal ``RelayTransportClient`` stand-in exposing only what this module
    needs: ``_require_http_client()`` returning a real ``httpx.AsyncClient``
    wired to a FAKE HTTP handler (``httpx.MockTransport``) standing in for
    relay's ``GET /jobs/{job_id}/logs/stdout`` door."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://relay.invalid"
        )

    def _require_http_client(self) -> httpx.AsyncClient:  # noqa: SLF001 - mirrors the real client's own name
        return self._client


def _scripted_log_handler(
    chunks: list[tuple[str, int]], calls: list[httpx.Request]
) -> Callable[[httpx.Request], httpx.Response]:
    """Return each queued ``(data, next_offset)`` pair in order, one per call."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        data, next_offset = chunks[len(calls) - 1]
        return httpx.Response(200, json={"data": data, "next_offset": next_offset})

    return handler


def _failing_handler(calls: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    """A log door that always 500s -- the resilience-under-failure fixture."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, text="log door unavailable")

    return handler


def _key(task_id: str = "job-1") -> TaskKey:
    return TaskKey(server_id="relay", session_id="sess_console", task_id=task_id)


async def _drive(
    on_poll: Callable[[Any, TaskKey, InMemoryTaskRecordStore], Awaitable[None]] | None,
    store: InMemoryTaskRecordStore,
    key: TaskKey,
    rounds: int,
) -> None:
    """Invoke ``on_poll`` ``rounds`` times, mirroring #1115's per-poll call site."""

    assert on_poll is not None
    for _ in range(rounds):
        await on_poll(None, key, store)


# --------------------------------------------------------------------------- #
# Config knobs (relay.console.* / CLIO_RELAY_CONSOLE_*, conf.resolve -- never  #
# a bespoke os.getenv read).                                                  #
# --------------------------------------------------------------------------- #


def test_console_enabled_defaults_true_and_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert console_enabled() is True
    monkeypatch.setenv("CLIO_RELAY_CONSOLE_ENABLED", "false")
    assert console_enabled() is False


def test_pull_limit_bytes_default_and_clamped_to_relays_hard_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert console_pull_limit_bytes() == 65_536
    monkeypatch.setenv(
        "CLIO_RELAY_CONSOLE_PULL_LIMIT_BYTES", str(RELAY_LOG_PULL_HARD_CAP_BYTES * 4)
    )
    assert console_pull_limit_bytes() == RELAY_LOG_PULL_HARD_CAP_BYTES


def test_tail_cap_bytes_default_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    assert console_tail_cap_bytes() == 8_192
    monkeypatch.setenv("CLIO_RELAY_CONSOLE_TAIL_CAP_BYTES", "256")
    assert console_tail_cap_bytes() == 256


def test_make_console_on_poll_returns_none_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_RELAY_CONSOLE_ENABLED", "false")
    calls: list[httpx.Request] = []
    client = _FakeRelayHttpClient(_scripted_log_handler([("x", 1)], calls))
    assert make_console_on_poll(client, "job-1") is None  # type: ignore[arg-type]
    assert calls == []


# --------------------------------------------------------------------------- #
# The core fold: growing tail, advancing offset, put-only-on-new-bytes.       #
# --------------------------------------------------------------------------- #


async def test_on_poll_folds_growing_tail_and_advances_offset() -> None:
    """FAILING-FIRST for #1231 Part 2: each poll's console increment must land
    in the durable record's ``backend["console"]`` with the offset advancing,
    so the session's task thread (mcp_task_events, once Part 1 binds the
    session) sees the console grow across polls, not just a final snapshot."""

    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))
    calls: list[httpx.Request] = []
    handler = _scripted_log_handler(
        [("first line\n", 11), ("second line\n", 23), ("third line\n", 34)], calls
    )
    client = _FakeRelayHttpClient(handler)
    on_poll = make_console_on_poll(client, "job-1")

    await _drive(on_poll, store, key, rounds=3)

    assert [call.url.params.get("offset") for call in calls] == ["0", "11", "23"]
    record = store.get(key)
    assert record is not None
    console = record.backend["console"]
    assert console["tail"] == "first line\nsecond line\nthird line\n"
    assert console["offset"] == 34
    assert console["truncated"] is False


async def test_on_poll_skips_put_when_no_new_bytes() -> None:
    """A poll that observes zero growth must not touch the record at all --
    proven by object identity, since ``InMemoryTaskRecordStore.put`` always
    installs a NEW record instance."""

    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))
    before = store.get(key)
    calls: list[httpx.Request] = []
    client = _FakeRelayHttpClient(_scripted_log_handler([("", 0)], calls))
    on_poll = make_console_on_poll(client, "job-1")

    await _drive(on_poll, store, key, rounds=1)

    assert store.get(key) is before


async def test_on_poll_no_record_is_a_silent_noop() -> None:
    """No persisted record for the key (settled/dropped) -> nothing to fold into."""

    key = _key()
    store = InMemoryTaskRecordStore()
    calls: list[httpx.Request] = []
    client = _FakeRelayHttpClient(_scripted_log_handler([("data", 4)], calls))
    on_poll = make_console_on_poll(client, "job-1")

    await _drive(on_poll, store, key, rounds=1)

    assert calls == []
    assert store.get(key) is None


# --------------------------------------------------------------------------- #
# Truncation: typed marker, bounded tail cap.                                 #
# --------------------------------------------------------------------------- #


async def test_on_poll_truncates_typed_when_tail_exceeds_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILING-FIRST: the tail must never grow past the configured cap, and a
    cut is a TYPED, visible fact -- never a silent shrink (project-wide
    no-silent-fallback rule)."""

    monkeypatch.setenv("CLIO_RELAY_CONSOLE_TAIL_CAP_BYTES", "100")
    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))
    calls: list[httpx.Request] = []
    big_chunk = "x" * 100 + "\n"
    client = _FakeRelayHttpClient(_scripted_log_handler([(big_chunk, len(big_chunk))], calls))
    on_poll = make_console_on_poll(client, "job-1")

    await _drive(on_poll, store, key, rounds=1)

    record = store.get(key)
    assert record is not None
    console = record.backend["console"]
    assert console["truncated"] is True
    assert CONSOLE_TAIL_TRUNCATED_REASON in console["tail"]
    assert len(console["tail"].encode("utf-8")) <= 100
    # The retained bytes are the MOST RECENT ones (a tail, not a head).
    assert console["tail"].endswith("x\n")


async def test_on_poll_never_exceeds_cap_even_when_cap_is_smaller_than_the_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded memory is release-gating (hard bound, proven live): even a
    misconfigured cap smaller than the truncation marker itself must never
    let the stored tail exceed it. The marker text is dropped in that edge
    case, but ``truncated`` still carries the fact -- never a silent shrink."""

    monkeypatch.setenv("CLIO_RELAY_CONSOLE_TAIL_CAP_BYTES", "8")
    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))
    calls: list[httpx.Request] = []
    big_chunk = "x" * 50 + "\n"
    client = _FakeRelayHttpClient(_scripted_log_handler([(big_chunk, len(big_chunk))], calls))
    on_poll = make_console_on_poll(client, "job-1")

    await _drive(on_poll, store, key, rounds=1)

    record = store.get(key)
    assert record is not None
    console = record.backend["console"]
    assert console["truncated"] is True
    assert len(console["tail"].encode("utf-8")) <= 8


# --------------------------------------------------------------------------- #
# Resilience: a log-pull failure never breaks the wait.                       #
# --------------------------------------------------------------------------- #


async def test_on_poll_log_failure_is_non_fatal_and_warns_once_then_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """FAILING-FIRST: relay's log door being down (not yet deployed, network
    blip, malformed envelope) must never propagate out of ``on_poll`` and
    break #1115's drive-to-terminal loop -- reported once at WARNING, then
    DEBUG on every later poll of the same drive (never floods the log)."""

    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))
    calls: list[httpx.Request] = []
    client = _FakeRelayHttpClient(_failing_handler(calls))
    on_poll = make_console_on_poll(client, "job-1")

    with caplog.at_level(logging.DEBUG, logger="clio_agent.tools.relay_console"):
        await _drive(on_poll, store, key, rounds=2)  # must not raise

    assert len(calls) == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(warnings) == 1
    assert len(debugs) == 1
    assert "relay_console_pull_failed" in warnings[0].message
    # The record is untouched -- a failed pull writes nothing.
    record = store.get(key)
    assert record is not None
    assert "console" not in record.backend
