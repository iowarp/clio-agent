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
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from clio_agent.tools.mcp_task_records import (
    InMemoryTaskRecordStore,
    TaskKey,
    TaskRecord,
    set_task_console_listener,
)
from clio_agent.tools.relay_console import (
    CONSOLE_TAIL_TRUNCATED_REASON,
    RELAY_LOG_PULL_HARD_CAP_BYTES,
    console_enabled,
    console_pull_limit_bytes,
    console_tail_cap_bytes,
    make_console_on_poll,
)


class _FakeConsoleStreamRegistry:
    """Records dispatch decisions WITHOUT actually running an SSE reader --
    keeps this file's tests scoped to ``relay_console.py``'s own dispatch
    logic (pull vs. ensure-a-reader), never ``relay_console_stream.py``'s real
    reader mechanics (covered in ``test_relay_console_stream.py``)."""

    def __init__(self, *, fallen_back: bool = False, stopped: bool = False) -> None:
        self._fallen_back = fallen_back
        self._stopped = stopped
        self.ensure_calls: list[tuple[str, str]] = []
        self.cancel_calls: list[tuple[str, str]] = []

    def has_fallen_back(self, job_id: str, stream: str) -> bool:
        return self._fallen_back

    def has_stopped(self, job_id: str, stream: str) -> bool:
        return self._stopped

    def is_sse_exhausted(self, job_id: str, stream: str) -> bool:
        return self._fallen_back or self._stopped

    def ensure_reader(self, job_id: str, stream: str, factory: Any) -> None:
        self.ensure_calls.append((job_id, stream))

    async def cancel_one(self, job_id: str, stream: str) -> None:
        self.cancel_calls.append((job_id, stream))


class _FakeRelayHttpClient:
    """Minimal ``RelayTransportClient`` stand-in exposing only what this module
    needs: ``_require_http_client()`` returning a real ``httpx.AsyncClient``
    wired to a FAKE HTTP handler (``httpx.MockTransport``) standing in for
    relay's ``GET /jobs/{job_id}/logs/stdout`` door. ``sse_supported``/
    ``registry`` default to the pull-path's exact pre-#221/#259 shape (no
    capability) so every EXISTING test below keeps exercising that path
    byte-for-byte, unchanged."""

    def __init__(
        self,
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        sse_supported: bool = False,
        registry: _FakeConsoleStreamRegistry | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://relay.invalid"
        )
        self._sse_supported = sse_supported
        self._console_stream_registry = registry or _FakeConsoleStreamRegistry()

    def _require_http_client(self) -> httpx.AsyncClient:  # noqa: SLF001 - mirrors the real client's own name
        return self._client

    def console_sse_supported(self) -> bool:
        return self._sse_supported


class _BareFakeRelayHttpClient:
    """A client with NO ``console_sse_supported``/``_console_stream_registry``
    attributes at all -- proves ``make_console_on_poll``'s ``getattr`` guard
    treats absence as "no capability" rather than crashing (a minimal test
    double, exactly as the module's own docstring anticipates)."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://relay.invalid"
        )

    def _require_http_client(self) -> httpx.AsyncClient:  # noqa: SLF001
        return self._client


def _scripted_log_handler(
    chunks: list[tuple[str, int]], calls: list[httpx.Request]
) -> Callable[[httpx.Request], httpx.Response]:
    """Return each queued ``(text, next_offset)`` pair in order, one per call.

    Serves relay's REAL log envelope (``job_id``/``stream``/``offset``/
    ``next_offset``/``eof``/``text`` -- verified against the live door), not
    an invented shape: the pre-fix client parsed a ``data`` key relay never
    serves, so every live pull failed while these fixtures stayed green.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        text, next_offset = chunks[len(calls) - 1]
        return httpx.Response(
            200,
            json={
                "job_id": request.url.path.split("/")[2],
                "stream": request.url.path.rsplit("/", 1)[-1],
                "offset": int(request.url.params.get("offset", "0")),
                "next_offset": next_offset,
                "eof": len(calls) >= len(chunks),
                "text": text,
            },
        )

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


async def test_on_poll_pulls_the_console_stream_not_the_process_stdio() -> None:
    """The fold must ask for relay's ``console`` stream (the application
    output clio-relay#259 feeds), never ``stdout`` -- for an ``mcp_call`` job
    that stream is the MCP jsonrpc wire, which is plumbing, not console.
    Run 13's live diagnosis: the client pulled ``/logs/stdout`` and would have
    folded protocol frames had the envelope parse not also been broken."""

    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))
    calls: list[httpx.Request] = []
    client = _FakeRelayHttpClient(_scripted_log_handler([("app output\n", 11)], calls))
    on_poll = make_console_on_poll(client, "job-1")

    await _drive(on_poll, store, key, rounds=1)

    assert len(calls) == 1
    assert calls[0].url.path == "/jobs/job-1/logs/console"


async def test_on_poll_rejects_the_legacy_data_envelope_without_folding(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Run 13's root cause, pinned: a door answering the pre-fix invented
    ``{"data": ...}`` shape is a MALFORMED envelope -- the hook must warn and
    fold nothing, never guess. (The live door serves ``text``; this guards
    against the fixture-only shape ever counting as valid again.)"""

    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))
    before = store.get(key)
    calls: list[httpx.Request] = []

    def legacy_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": "app output\n", "next_offset": 11})

    client = _FakeRelayHttpClient(legacy_handler)
    on_poll = make_console_on_poll(client, "job-1")

    with caplog.at_level(logging.WARNING, logger="clio_agent.tools.relay_console"):
        await _drive(on_poll, store, key, rounds=1)

    assert store.get(key) is before
    assert any("relay_console_pull_failed" in message for message in caplog.messages)


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


# --------------------------------------------------------------------------- #
# #1236: the lean console-delta listener. Separate from the full-record       #
# store.put notify (which fans a whole-tail snapshot out via                  #
# task_change_listener on every fold) -- this hook exists so the live stream  #
# does not have to re-consume a growing snapshot on every poll.               #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_console_listener() -> Any:
    """Never leak one test's installed listener into another's process-global."""

    yield
    set_task_console_listener(None)


async def test_on_poll_notifies_the_console_listener_with_just_the_delta() -> None:
    """FAILING-FIRST for #1236: the listener must fire on each fold with ONLY
    the new bytes (never the accumulated tail) -- proven across two polls so a
    listener that received the whole tail would visibly fail the second
    assertion."""

    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))
    calls: list[httpx.Request] = []
    handler = _scripted_log_handler([("first line\n", 11), ("second line\n", 23)], calls)
    client = _FakeRelayHttpClient(handler)
    on_poll = make_console_on_poll(client, "job-1")

    notified: list[tuple[Any, ...]] = []
    set_task_console_listener(
        lambda k, channel, delta, offset, truncated: notified.append(
            (k, channel, delta, offset, truncated)
        )
    )

    await _drive(on_poll, store, key, rounds=2)

    assert len(notified) == 2
    k0, channel0, delta0, offset0, truncated0 = notified[0]
    assert k0 == key
    assert channel0 == "console"
    assert delta0 == "first line\n", "the delta, not the accumulated tail"
    assert offset0 == 11
    assert truncated0 is False
    k1, channel1, delta1, offset1, truncated1 = notified[1]
    assert delta1 == "second line\n", "the SECOND delta must not repeat the first"
    assert offset1 == 23


async def test_on_poll_does_not_notify_the_console_listener_when_nothing_grew() -> None:
    """The deliverable's other half: a poll that observes zero new bytes must
    fire NO console event at all -- matches the existing
    ``test_on_poll_skips_put_when_no_new_bytes`` contract for the record write,
    now proven for the listener too."""

    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))
    calls: list[httpx.Request] = []
    client = _FakeRelayHttpClient(_scripted_log_handler([("", 0)], calls))
    on_poll = make_console_on_poll(client, "job-1")

    notified: list[Any] = []
    set_task_console_listener(lambda *args: notified.append(args))

    await _drive(on_poll, store, key, rounds=1)

    assert notified == []


async def test_on_poll_console_listener_absent_is_a_quiet_noop() -> None:
    """No listener installed (the default, e.g. a bare unit test with no gact
    server booted) must not raise -- mirrors task_change_listener's own
    documented absent-is-fine contract."""

    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))
    calls: list[httpx.Request] = []
    client = _FakeRelayHttpClient(_scripted_log_handler([("data\n", 5)], calls))
    on_poll = make_console_on_poll(client, "job-1")

    await _drive(on_poll, store, key, rounds=1)  # must not raise

    record = store.get(key)
    assert record is not None
    assert record.backend["console"]["tail"] == "data\n"


async def test_on_poll_console_listener_failure_is_non_fatal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken listener must never break the wait -- same contract as a
    broken log-pull, caught and warned, never propagated."""

    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))
    calls: list[httpx.Request] = []
    client = _FakeRelayHttpClient(_scripted_log_handler([("data\n", 5)], calls))
    on_poll = make_console_on_poll(client, "job-1")

    def _broken(*args: Any) -> None:
        raise RuntimeError("listener boom")

    set_task_console_listener(_broken)

    with caplog.at_level(logging.WARNING, logger="clio_agent.tools.relay_console"):
        await _drive(on_poll, store, key, rounds=1)  # must not raise

    assert any("relay_console_delta_listener_failed" in m for m in caplog.messages)
    # The record write itself still succeeded -- only the listener call failed.
    record = store.get(key)
    assert record is not None
    assert record.backend["console"]["tail"] == "data\n"


async def test_on_poll_console_listener_channel_follows_the_configured_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The channel field must never be hardcoded to "stdout"/"console" -- it
    follows :func:`console_stream`'s config, so a future relay stderr tail
    (item 4, relay-side work) slots in without a clio-agent shape change."""

    monkeypatch.setenv("CLIO_RELAY_CONSOLE_STREAM", "stderr")
    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))
    calls: list[httpx.Request] = []
    client = _FakeRelayHttpClient(_scripted_log_handler([("oops\n", 5)], calls))
    on_poll = make_console_on_poll(client, "job-1")

    notified: list[Any] = []
    set_task_console_listener(lambda k, channel, delta, offset, truncated: notified.append(channel))

    await _drive(on_poll, store, key, rounds=1)

    assert notified == ["stderr"]


# --------------------------------------------------------------------------- #
# clio-relay#221/#259: the SSE-vs-pull dispatch decision. The reader's own    #
# mechanics live in test_relay_console_stream.py -- this file only proves     #
# _on_poll picks the right path and never pulls when SSE owns the tail.       #
# --------------------------------------------------------------------------- #


async def test_on_poll_ensures_an_sse_reader_and_never_pulls_when_supported() -> None:
    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))
    calls: list[httpx.Request] = []
    registry = _FakeConsoleStreamRegistry()
    client = _FakeRelayHttpClient(
        _scripted_log_handler([("x", 1)], calls), sse_supported=True, registry=registry
    )
    on_poll = make_console_on_poll(client, "job-1")

    await _drive(on_poll, store, key, rounds=3)

    assert calls == [], "SSE mode must never fall back to a pull request"
    assert registry.ensure_calls == [("job-1", "console")] * 3
    assert registry.cancel_calls == []
    # The pull path never ran, so the record is untouched by _on_poll itself --
    # the (fake) SSE reader owns folding when it actually runs.
    assert "console" not in store.get(key).backend  # type: ignore[union-attr]


async def test_on_poll_terminal_tick_cancels_the_sse_reader_and_drains_via_pull() -> None:
    """Adversarial review D1 (BLOCKER): a terminal tick must NEVER ensure a
    reader -- it stops whatever reader IS running (cancel_one) and falls
    through to ONE final bounded pull, so the tail already in flight is never
    silently dropped (the pre-fix bug: PULL 'hello from the job' vs. PUSH '').
    This replaces the old (bug-enshrining) test that asserted ensure_calls
    non-empty on a terminal tick."""

    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))
    calls: list[httpx.Request] = []
    registry = _FakeConsoleStreamRegistry()
    client = _FakeRelayHttpClient(
        _scripted_log_handler([("hello from the job", 19)], calls),
        sse_supported=True,
        registry=registry,
    )
    on_poll = make_console_on_poll(client, "job-1")
    assert on_poll is not None

    # A non-terminal tick first -- ensures the SSE reader (never a pull).
    await on_poll(SimpleNamespace(status="working"), key, store)
    assert registry.ensure_calls == [("job-1", "console")]
    assert calls == []

    # The terminal tick: cancel the (now-running) reader, then drain via ONE
    # final bounded pull -- never a second ensure_reader call.
    await on_poll(SimpleNamespace(status="completed"), key, store)

    assert registry.ensure_calls == [("job-1", "console")]  # unchanged -- never re-ensured
    assert registry.cancel_calls == [("job-1", "console")]
    assert len(calls) == 1
    record = store.get(key)
    assert record is not None
    assert record.backend["console"]["tail"] == "hello from the job"


async def test_on_poll_fast_job_terminal_at_first_lookup_still_folds_via_pull() -> None:
    """#1231's fast-job/observe-peek guarantee (relay_transport.py's poll()):
    a job already terminal on the FIRST (and only) on_poll call -- no prior
    tick ever ran -- must still fold its tail via the pull path. The pre-fix
    bug spawned-then-cancelled a reader that read nothing, losing the tail
    entirely."""

    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))
    calls: list[httpx.Request] = []
    registry = _FakeConsoleStreamRegistry()
    client = _FakeRelayHttpClient(
        _scripted_log_handler([("hello from the job", 19)], calls),
        sse_supported=True,
        registry=registry,
    )
    on_poll = make_console_on_poll(client, "job-1")
    assert on_poll is not None

    await on_poll(
        SimpleNamespace(status="completed"), key, store
    )  # ONE call, terminal from the start

    assert registry.ensure_calls == []  # never spawned
    assert len(calls) == 1
    record = store.get(key)
    assert record is not None
    assert record.backend["console"]["tail"] == "hello from the job"


async def test_on_poll_falls_through_to_pull_once_sse_has_fallen_back() -> None:
    """Even with ``console_sse_supported() == True``, a (job, stream) the
    registry already marked fallen-back must route straight to the pull path
    -- never a repeated SSE attempt for the rest of this client's lifetime."""

    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))
    calls: list[httpx.Request] = []
    registry = _FakeConsoleStreamRegistry(fallen_back=True)
    client = _FakeRelayHttpClient(
        _scripted_log_handler([("first line\n", 11)], calls), sse_supported=True, registry=registry
    )
    on_poll = make_console_on_poll(client, "job-1")

    await _drive(on_poll, store, key, rounds=1)

    assert len(calls) == 1
    assert registry.ensure_calls == []
    record = store.get(key)
    assert record is not None
    assert record.backend["console"]["tail"] == "first line\n"


async def test_on_poll_falls_through_to_pull_once_sse_has_stopped() -> None:
    """Adversarial review D2's dispatch-layer half: a (job, stream) the
    registry marked STOPPED (relay reported it gone or cleanly finished --
    distinct from a connectivity fallback) must also route straight to the
    pull path, never re-ensure a reader."""

    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))
    calls: list[httpx.Request] = []
    registry = _FakeConsoleStreamRegistry(stopped=True)
    client = _FakeRelayHttpClient(
        _scripted_log_handler([("first line\n", 11)], calls), sse_supported=True, registry=registry
    )
    on_poll = make_console_on_poll(client, "job-1")

    await _drive(on_poll, store, key, rounds=1)

    assert len(calls) == 1
    assert registry.ensure_calls == []
    record = store.get(key)
    assert record is not None
    assert record.backend["console"]["tail"] == "first line\n"


async def test_on_poll_treats_missing_sse_attributes_as_no_capability() -> None:
    """A client exposing neither ``console_sse_supported`` nor
    ``_console_stream_registry`` (a bare test double, or a probe that never
    ran) must drive the SAME pull path as an explicit ``sse_supported=False``
    -- design point 2: no capability, no behavior change, byte-for-byte."""

    key = _key()
    store = InMemoryTaskRecordStore()
    store.put(TaskRecord(key=key, tool="relay_run", status="working"))
    calls: list[httpx.Request] = []
    client = _BareFakeRelayHttpClient(_scripted_log_handler([("data\n", 5)], calls))
    on_poll = make_console_on_poll(client, "job-1")

    await _drive(on_poll, store, key, rounds=1)  # must not raise

    assert len(calls) == 1
    record = store.get(key)
    assert record is not None
    assert record.backend["console"]["tail"] == "data\n"
