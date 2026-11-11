"""Shared model admission and actual HTTP concurrency/stream cleanup."""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import anyio
import dspy
import pytest
from dspy.lm15 import (
    Message,
    RateLimitError,
    Request,
    Response,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    Usage,
)

from clio_agent.lm import admission
from clio_agent.lm.admission import RequestLease, capacity_cooldown, endpoint_key
from clio_agent.lm.admitted_dspy import AdmittedLM
from clio_agent.runtime.cancellation import cancellation_checker
from tests._config_layer import set_config


@pytest.fixture(autouse=True)
def isolated_budget() -> Iterator[None]:
    assert not any(b.active or b.waiting for b in admission._BUCKETS.values())
    admission._BUCKETS.clear()
    yield
    assert not any(b.active or b.waiting for b in admission._BUCKETS.values())
    admission._BUCKETS.clear()


def test_identity_uses_actual_origin_and_key_across_models_and_copies() -> None:
    lm = SimpleNamespace(
        model="openai/a", kwargs={"api_base": "https://HOST/v1", "api_key": "secret"}
    )
    first = endpoint_key(lm, {})
    lm.model = "openai/b"
    assert endpoint_key(lm, {"api_base": "https://host/v2"}) == first
    assert endpoint_key(lm, {"api_base": "https://host:443/v1"}) == first
    assert endpoint_key(lm, {"api_key": "other"}) != first
    assert endpoint_key(lm, {"api_base": "https://other/v1"}) != first
    assert "secret" not in first and "HOST" not in first


def test_mixed_threads_and_event_loops_share_fifo_budget() -> None:
    set_config("limits.lm_max_concurrent_requests", 2)
    active = maximum = 0
    lock = threading.Lock()

    def call(index: int) -> None:
        nonlocal active, maximum

        def inside() -> None:
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            with lock:
                active -= 1

        if index % 2:
            lease = RequestLease("same")
            lease.acquire()
            try:
                inside()
            finally:
                lease.release()
        else:

            async def run() -> None:
                lease = RequestLease("same")
                await lease.aacquire()
                try:
                    await asyncio.to_thread(inside)
                finally:
                    lease.release()

            asyncio.run(run())

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(call, range(24)))
    assert maximum == 2
    assert admission._BUCKETS == {}


def test_fifo_cancel_and_independent_credentials() -> None:
    set_config("limits.lm_max_concurrent_requests", 1)

    async def run() -> None:
        first = RequestLease("one")
        await first.aacquire()
        blocked = RequestLease("one")
        task = asyncio.create_task(blocked.aacquire())
        await asyncio.sleep(0)
        independent = RequestLease("two")
        await independent.aacquire()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(first.bucket.waiting) == 0
        first.release()
        independent.release()
        again = RequestLease("one")
        await again.aacquire()
        again.release()

    asyncio.run(run())


def test_fifo_order_and_cancelled_ticket_cannot_starve_the_next_call() -> None:
    set_config("limits.lm_max_concurrent_requests", 1)

    async def run() -> None:
        order: list[int] = []
        first = RequestLease("one")
        await first.aacquire()
        leases = [RequestLease("one") for _ in range(4)]

        async def waiter(index: int) -> None:
            lease = leases[index]
            await lease.aacquire()
            try:
                order.append(index)
            finally:
                lease.release()

        tasks = [asyncio.create_task(waiter(i)) for i in range(4)]
        await asyncio.sleep(0)
        tasks[1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[1]
        first.release()
        await asyncio.gather(tasks[0], tasks[2], tasks[3])
        assert order == [0, 2, 3]

    asyncio.run(run())


def test_cooperative_cancel_and_queue_deadline_do_not_dispatch() -> None:
    set_config("limits.lm_max_concurrent_requests", 1)
    first = RequestLease("one")
    first.acquire()
    try:
        from clio_agent.gact.runtime.globals import _TurnCancelled

        with cancellation_checker(lambda: True), pytest.raises(_TurnCancelled):
            RequestLease("one").acquire()
        set_config("limits.lm_request_queue_s", 0.02)
        with pytest.raises(TimeoutError, match="no request was sent"):
            RequestLease("one").acquire()
        assert len(first.bucket.waiting) == 0
    finally:
        first.release()


@pytest.mark.parametrize(
    "key,value",
    [
        ("lm_max_concurrent_requests", -1),
        ("lm_request_queue_s", 0),
        ("lm_request_queue_s", float("inf")),
    ],
)
def test_invalid_configuration_is_explicit(key: str, value: float) -> None:
    set_config(f"limits.{key}", value)
    with pytest.raises(ValueError):
        RequestLease("one")


def test_unlimited_does_not_disable_provider_cooldown() -> None:
    set_config("limits.lm_max_concurrent_requests", 0)
    leases = [RequestLease("one") for _ in range(25)]
    for lease in leases:
        lease.acquire()
    assert leases[0].snapshot()["active"] == 25
    for lease in leases:
        lease.release()
    future = datetime.now(UTC) + timedelta(seconds=15)
    error = RateLimitError(
        f"max_parallel_requests; Limit resets at: {future:%Y-%m-%d %H:%M:%S} UTC", status=429
    )
    assert 13 < capacity_cooldown("one", error) <= 15
    assert admission._BUCKETS["one"].cooldown_until > time.monotonic()
    blocked = RequestLease("one")
    try:
        assert blocked._try_acquire() is False
    finally:
        blocked.release()


@pytest.fixture
def http_model() -> Iterator[tuple[str, dict[str, Any]]]:
    state: dict[str, Any] = {
        "active": 0,
        "maximum": 0,
        "calls": 0,
        "refusals": 0,
        "stream": False,
        "started": threading.Event(),
        "closed": threading.Event(),
    }
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                state["calls"] += 1
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
                refused = state["refusals"] > 0
                if refused:
                    state["refusals"] -= 1
            try:
                if refused:
                    payload = json.dumps(
                        {
                            "error": {
                                "message": "Limit type: max_parallel_requests. Current limit: 20, Remaining: 0.",
                                "type": "rate_limit",
                            }
                        }
                    ).encode()
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if request.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    state["started"].set()
                    # Keep sending until the real HTTP caller closes its stream.
                    while True:
                        payload = json.dumps(
                            {
                                "id": "s",
                                "model": request["model"],
                                "choices": [
                                    {"index": 0, "delta": {"content": "x"}, "finish_reason": None}
                                ],
                            }
                        )
                        self.wfile.write(f"data: {payload}\n\n".encode())
                        self.wfile.flush()
                        time.sleep(0.02)
                else:
                    time.sleep(0.03)
                    payload = json.dumps(
                        {
                            "id": "reply",
                            "object": "chat.completion",
                            "model": request["model"],
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": "ok"},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 1,
                                "completion_tokens": 1,
                                "total_tokens": 2,
                            },
                        }
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                state["closed"].set()
            finally:
                with lock:
                    state["active"] -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def _lm(base: str, *, retries: int = 0) -> AdmittedLM:
    return AdmittedLM(
        "openai/test-model", api_base=base, api_key="test-only", cache=False, num_retries=retries
    )


def _request(lm: dspy.LM) -> Request:
    return Request(model=lm.model, messages=(Message.user("test"),))


def test_real_http_requests_from_fresh_lms_and_copies_stay_bounded(
    http_model: tuple[str, dict[str, Any]],
) -> None:
    base, state = http_model
    set_config("limits.lm_max_concurrent_requests", 3)

    async def run() -> None:
        lms = [_lm(base) for _ in range(12)]
        lms += [lm.copy() for lm in lms]
        try:
            responses = await asyncio.gather(*(lm.acall(_request(lm)) for lm in lms))
            assert len(responses) == 24
            assert all(response.message.parts[0].text == "ok" for response in responses)
        finally:
            for lm in lms:
                await lm.aclose()

    asyncio.run(run())
    assert state["calls"] == 24
    assert state["maximum"] <= 3


def test_real_stream_cancel_closes_socket_before_releasing_budget(
    http_model: tuple[str, dict[str, Any]],
) -> None:
    base, state = http_model
    set_config("limits.lm_max_concurrent_requests", 1)

    async def run() -> None:
        lm = _lm(base)
        send, receive = anyio.create_memory_object_stream[Any](math.inf)
        try:
            with dspy.context(send_stream=send):
                task = asyncio.create_task(lm.acall(_request(lm)))
            # A start signal, not a speed limit: a loaded shard can take seconds to connect.
            assert await asyncio.to_thread(state["started"].wait, 30)
            await receive.receive()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert admission._BUCKETS == {}
            assert await asyncio.to_thread(state["closed"].wait, 2)
        finally:
            await send.aclose()
            await receive.aclose()
            await lm.aclose()

    asyncio.run(run())


def test_session_stop_closes_real_http_stream_on_the_persistent_lm_loop(
    http_model: tuple[str, dict[str, Any]],
) -> None:
    from clio_agent.gact import context
    from clio_agent.gact.agents.clio_react import _call_lm
    from clio_agent.gact.runtime.globals import _TurnCancelled
    from clio_agent.lm.engines.lm_loop import run_on_lm_loop
    from clio_agent.providers.claude_code_cancel import (
        abort_session_streams,
        active_stream_sessions,
    )

    base, state = http_model
    lm = _lm(base)
    killed: list[int] = []

    def stop() -> None:
        assert state["started"].wait(30)
        killed.append(abort_session_streams("admission-test"))

    stopper = threading.Thread(target=stop)
    token = context.set_session_id("admission-test")
    try:
        stopper.start()
        with cancellation_checker(lambda: False), pytest.raises(_TurnCancelled):
            _call_lm(lm, _request(lm))
        stopper.join(5)
        assert killed == [1]
        assert state["closed"].wait(2)
        assert admission._BUCKETS == {}
        assert "admission-test" not in active_stream_sessions()
    finally:
        context.reset(token)
        run_on_lm_loop(lm.aclose)


def test_capacity_recovery_is_sequential_and_no_retry_setting_is_respected(
    http_model: tuple[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    base, state = http_model
    real_cooldown = capacity_cooldown

    def short_cooldown(key: str, exc: BaseException) -> float:
        delay = real_cooldown(key, exc)
        assert delay == 5
        with admission._LOCK:
            admission._BUCKETS[key].cooldown_until = time.monotonic() + 0.05
        return 0.05

    monkeypatch.setattr("clio_agent.lm.admitted_dspy.capacity_cooldown", short_cooldown)
    lm = _lm(base, retries=1)
    state["refusals"] = 2  # DSPy's initial attempt + ordinary retry.
    try:
        assert lm(_request(lm)).message.parts[0].text == "ok"
        assert state["calls"] == 3
        assert state["maximum"] == 1
    finally:
        lm.close()
    state["refusals"] = 1
    no_retry = _lm(base)
    try:
        with pytest.raises(Exception) as caught:
            no_retry(_request(no_retry))
        leaf = admission.capacity_error(caught.value)
        assert leaf is not None
        assert leaf.clio_request_admission == {"active": 1, "queued": 0, "limit": 8}
        assert state["calls"] == 4
    finally:
        no_retry.close()
    state["refusals"] = 20
    repeated = _lm(base, retries=1)
    try:
        from clio_agent.gact.stream_failures import provider_failure_message

        with pytest.raises(Exception) as caught:
            repeated(_request(repeated))
        assert state["calls"] == 8  # two ordinary attempts per bounded recovery leg
        assert state["maximum"] == 1
        assert (
            "1 admitted model calls (including this call) and 0 queued"
            in provider_failure_message(caught.value, provider_label="AMSC")
        )
    finally:
        repeated.close()


@pytest.mark.parametrize(
    "part",
    [
        TextPart(text="answer"),
        ThinkingPart(text="thinking"),
        ToolCallPart(id="one", name="tool", input={"x": 1}),
    ],
)
def test_capacity_refusal_after_streamed_output_is_never_replayed(part: Any) -> None:
    from dspy._vendor.lm15.result import response_to_events

    class PartialEngine:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, request: Request) -> Response:
            raise AssertionError("stream expected")

        def stream(self, request: Request) -> Iterator[Any]:
            self.calls += 1
            response = Response(
                id="one",
                model=request.model,
                message=Message.assistant([part]),
                finish_reason="stop",
                usage=Usage(),
            )
            for event in response_to_events(response):
                if event.type == "end":
                    raise RateLimitError("max_parallel_requests", status=429)
                yield event

        def close(self) -> None:
            pass

    class AsyncPartial:
        async def complete(self, request: Request) -> Response:
            raise AssertionError("stream expected")

        async def stream(self, request: Request) -> Any:
            for event in engine.stream(request):
                yield event

        async def aclose(self) -> None:
            pass

    engine = PartialEngine()
    lm = AdmittedLM(
        "scripted/model", engine=engine, async_engine=AsyncPartial(), cache=False, num_retries=2
    )

    async def run() -> None:
        send, receive = anyio.create_memory_object_stream[Any](math.inf)
        try:
            with dspy.context(send_stream=send), pytest.raises(Exception) as caught:
                await lm.acall(_request(lm))
            assert admission.capacity_error(caught.value) is not None
            assert engine.calls == 1
            assert send.statistics().current_buffer_used > 0
        finally:
            await send.aclose()
            await receive.aclose()
            await lm.aclose()

    asyncio.run(run())


def test_final_capacity_failure_reports_local_counts_without_claiming_account_ownership() -> None:
    from dspy.utils.exceptions import LMRateLimitError

    from clio_agent.gact.stream_failures import forward_error_info, provider_failure_message

    error = LMRateLimitError("Limit type: max_parallel_requests. Remaining: 0.", status=429)
    error.clio_request_admission = {"active": 1, "queued": 0, "limit": 8}
    message = provider_failure_message(error, provider_label="AMSC")
    assert "1 admitted model calls (including this call) and 0 queued" in message
    assert "account-wide capacity" in message
    state = SimpleNamespace(
        user_msg=SimpleNamespace(metadata={"effective_model": {"provider_id": "amsc_mag"}})
    )
    info = forward_error_info(state, error, "")
    assert info.details["model_request_admission"] == error.clio_request_admission


def test_factory_and_attention_use_admission_without_legacy_forward_overrides() -> None:
    from clio_agent.config import LMProviderConfig
    from clio_agent.lm.attention_dspy import AttentionLM
    from clio_agent.lm.factory import create_lm

    lm = create_lm(
        LMProviderConfig(
            provider="openai",
            provider_id="amsc_mag",
            model="test",
            api_base="https://mag.example/v1",
            api_key="test-only",
        )
    )
    assert isinstance(lm, AdmittedLM)
    assert isinstance(lm.copy(), AdmittedLM)
    assert issubclass(AttentionLM, AdmittedLM)
    assert AdmittedLM.forward is dspy.LM.forward
    assert AdmittedLM.aforward is dspy.LM.aforward


def test_wait_notice_failure_cannot_fail_the_model_call(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def unavailable(text: str, snapshot: dict[str, int]) -> None:
        raise RuntimeError("private storage diagnostic")

    monkeypatch.setattr(admission, "_wait_notice", unavailable)
    admission.wait_notice("Waiting", {"active": 1, "queued": 1, "limit": 1})
    assert "error_type=RuntimeError" in caplog.text
    assert "private storage diagnostic" not in caplog.text
