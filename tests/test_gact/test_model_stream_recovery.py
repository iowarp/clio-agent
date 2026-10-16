"""Real HTTP/SSE interruption through DSPy, the agent loop and ARC context."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import dspy
import pytest
from dspy.utils.exceptions import LMTransportError

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.agents.clio_react_recovery import (
    InterruptedModelStream,
    StreamProgress,
    interrupted_stream,
    recover_stream,
)
from clio_agent.lm.engines.lm_loop import run_on_lm_loop
from tests.test_gact.test_context_projection import _plane


@dataclass
class Attempt:
    deltas: list[dict[str, Any]]
    drop: bool = False


@contextmanager
def endpoint(script: list[Attempt]) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Serve real chunked HTTP; drop attempts omit the terminal HTTP chunk."""
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            size = int(self.headers["Content-Length"])
            body = json.loads(self.rfile.read(size))
            index = len(requests)
            requests.append(body)
            attempt = script[min(index, len(script) - 1)]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def write(data: bytes) -> None:
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()

            for delta in attempt.deltas:
                event = {
                    "id": f"attempt_{index}",
                    "object": "chat.completion.chunk",
                    "model": body["model"],
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                }
                write(f"data: {json.dumps(event)}\n\n".encode())
            if attempt.drop:
                self.close_connection = True
                return
            finish = {
                "id": f"attempt_{index}",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            write(f"data: {json.dumps(finish)}\n\ndata: [DONE]\n\n".encode())
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def lm_for(base: str) -> Any:
    """Use CLIO's actual factory, stateless native HTTP engine and no inference service."""
    from clio_agent.config import LMProviderConfig
    from clio_agent.lm.factory import create_lm

    return create_lm(
        LMProviderConfig(provider="openai", model="gpt-4o-mini", api_base=base, api_key="test")
    )


@pytest.mark.parametrize(
    "delta",
    [
        {"role": "assistant"},
        {"reasoning_content": "Checking the result."},
        {"content": "<think>still thinking"},
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "discard",
                    "type": "function",
                    "function": {"name": "record", "arguments": '{"index":'},
                }
            ]
        },
    ],
)
def test_recovery_preserves_five_completed_tools_without_replaying_them(
    tmp_path: Path, delta: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_LM_TRANSIENT_RETRIES", "2")
    ran: list[int] = []

    def record(index: int) -> str:
        """Record a numbered result on disk."""
        ran.append(index)
        path = tmp_path / f"result-{index}.txt"
        path.write_text("saved", encoding="utf-8")
        return str(path)

    tools = [
        {
            "index": i,
            "id": f"saved_{i}",
            "type": "function",
            "function": {"name": "record", "arguments": json.dumps({"index": i})},
        }
        for i in range(5)
    ]
    script = [
        Attempt([{"tool_calls": tools}]),
        Attempt([delta], drop=True),
        Attempt([{"content": "Done."}]),
    ]
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    with endpoint(script) as (base, requests), _plane(arc):
        lm = lm_for(base)
        try:
            with dspy.context(lm=lm):
                result = ClioReAct("question -> answer", tools=[record])(
                    question="Record five results"
                )
        finally:
            run_on_lm_loop(lm.aclose)
        assert result.answer == "Done."
        assert len(requests) == 3
        assert requests[1]["messages"] == requests[2]["messages"]
        tool_rows = [m for m in requests[2]["messages"] if m["role"] == "tool"]
        assert len(tool_rows) == 5
        assert {m["tool_call_id"] for m in tool_rows} == {f"saved_{i}" for i in range(5)}
    assert sorted(ran) == list(range(5))
    assert len(list(tmp_path.glob("result-*.txt"))) == 5


def test_partial_public_answer_does_not_restart_or_duplicate(tmp_path: Path) -> None:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    with (
        endpoint([Attempt([{"content": "Partial answer"}], drop=True)]) as (base, requests),
        _plane(arc),
    ):
        lm = lm_for(base)
        try:
            with dspy.context(lm=lm), pytest.raises(LMTransportError, match="mid-chunk"):
                ClioReAct("question -> answer", tools=[])(question="Answer")
        finally:
            run_on_lm_loop(lm.aclose)
        assert len(requests) == 1


def test_recovery_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_LM_TRANSIENT_RETRIES", "1")
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    with (
        endpoint([Attempt([{"reasoning_content": "Working"}], drop=True)]) as (base, requests),
        _plane(arc),
    ):
        lm = lm_for(base)
        try:
            with dspy.context(lm=lm), pytest.raises(LMTransportError, match="mid-chunk") as failed:
                ClioReAct("question -> answer", tools=[])(question="Answer")
        finally:
            run_on_lm_loop(lm.aclose)
        assert len(requests) == 2
        assert failed.value.__cause__ is not None
        assert not isinstance(failed.value.__cause__, InterruptedModelStream)


def test_cancel_during_backoff_prevents_another_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact.agents import clio_react_recovery as recovery

    cancelled = False
    requests: list[Any] = []

    def call(lm: Any, request: Any) -> Any:
        requests.append(request)
        raise InterruptedModelStream(LMTransportError("closed"))

    def sleep(seconds: float) -> None:
        nonlocal cancelled
        cancelled = True

    def check() -> None:
        if cancelled:
            raise RuntimeError("cancelled")

    monkeypatch.setattr(recovery.time, "sleep", sleep)
    with pytest.raises(RuntimeError, match="cancelled"):
        recover_stream(None, lambda: None, call, check)
    assert len(requests) == 1


@pytest.mark.parametrize("reason", ["custom", "no_chunk", "answer", "not_transport", "native_tool"])
def test_recovery_requires_explicit_safe_generation_boundary(reason: str) -> None:
    from types import SimpleNamespace

    from dspy.lm15 import Message, Request

    lm = SimpleNamespace(
        _clio_stream_recovery=True, _engine_spec=object() if reason == "custom" else "lm15"
    )
    request = Request(model="m", messages=(Message.user("q"),))
    if reason == "native_tool":
        request = SimpleNamespace(tools=[object()])
    progress = StreamProgress(emitted=reason != "no_chunk", answer=reason == "answer")
    error = RuntimeError("closed") if reason == "not_transport" else LMTransportError("closed")
    assert interrupted_stream(lm, request, progress, error) is None


def test_steer_arriving_during_backoff_is_in_the_new_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.agents import clio_react_recovery as recovery

    pending: list[tuple[str, str]] = []
    slept = False
    original_sleep = recovery.time.sleep

    def sleep(seconds: float) -> None:
        nonlocal slept
        if not slept:
            pending.append(("steer", "Use the saved result; do not repeat the tools."))
            slept = True
        original_sleep(seconds)

    def drain() -> list[tuple[str, str]]:
        result = pending[:]
        pending.clear()
        return result

    monkeypatch.setattr(recovery.time, "sleep", sleep)
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    script = [
        Attempt([{"reasoning_content": "Working"}], drop=True),
        Attempt([{"content": "Updated."}]),
    ]
    with endpoint(script) as (base, requests), _plane(arc, pending_loop_inbox_drain=drain):
        lm = lm_for(base)
        try:
            with dspy.context(lm=lm):
                result = ClioReAct("question -> answer", tools=[])(question="Answer")
        finally:
            run_on_lm_loop(lm.aclose)
        assert result.answer == "Updated."
        assert len(requests) == 2
        assert requests[1]["messages"][-1] == {
            "role": "user",
            "content": "Use the saved result; do not repeat the tools.",
        }


def test_live_retry_boundary_preserves_reasoning_in_order_after_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import context as ctx
    from clio_agent.gact.protocol.v3.message import message_to_v3
    from tests.test_gact.test_react_extract_suppression import _drive

    monkeypatch.setenv("CLIO_LM_TRANSIENT_RETRIES", "2")
    script = [
        Attempt([{"reasoning_content": "Checking the saved results."}], drop=True),
        Attempt(
            [
                {"reasoning_content": "Continuing after reconnect."},
                {"content": "The results are saved."},
            ]
        ),
    ]
    with endpoint(script) as (base, requests):
        lm = lm_for(base)

        def action() -> Any:
            app = ctx.active_app()
            app.state.arc = ARCMemory(data_dir=str(tmp_path / "arc"))
            token = ctx.set_react_session(ctx.active_session_id() or "")
            try:
                with dspy.context(lm=lm):
                    return ClioReAct("question -> answer", tools=[])(
                        question="Check the saved results"
                    )
            finally:
                ctx.reset(token)

        try:
            result = _drive(tmp_path, monkeypatch, scope="main", kind="react", action=action)
        finally:
            run_on_lm_loop(lm.aclose)
        assert result["result"].answer == "The results are saved."
        assert len(requests) == 2
    parts = result["transcript"].snapshot()
    assert [p.type for p in parts] == ["thinking", "notice", "thinking", "text"]
    assert [p.text for p in parts if p.type == "thinking"] == [
        "Checking the saved results.",
        "Continuing after reconnect.",
    ]
    assert parts[1].source == "model_connection_retry"
    assert "(1/2)" in parts[1].text
    assert "model_connection_retry" not in str(requests[-1]["messages"])
    response = result["client"].get(f"/v1/sessions/{result['sid']}/messages")
    assert response.status_code == 200
    reloaded = response.json()["messages"][-1]
    projected = message_to_v3(reloaded)
    assert [b["type"] for b in projected["blocks"]] == ["reasoning", "notice", "reasoning", "text"]
    assert all("duration_ms" in b for b in projected["blocks"] if b["type"] == "reasoning")
    if output := os.environ.get("CLIO_STREAM_REVIEW_OUTPUT"):
        Path(output).write_text(json.dumps(projected, indent=2), encoding="utf-8")


def test_silent_failure_uses_only_dspys_pre_stream_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_LM_TRANSIENT_RETRIES", "1")
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    with endpoint([Attempt([], drop=True)]) as (base, requests), _plane(arc):
        lm = lm_for(base)
        try:
            with dspy.context(lm=lm), pytest.raises(LMTransportError, match="mid-chunk"):
                ClioReAct("question -> answer", tools=[])(question="Answer")
        finally:
            run_on_lm_loop(lm.aclose)
        assert len(requests) == 2


def test_zero_retries_disables_post_chunk_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_LM_TRANSIENT_RETRIES", "0")
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    with (
        endpoint([Attempt([{"reasoning_content": "Working"}], drop=True)]) as (base, requests),
        _plane(arc),
    ):
        lm = lm_for(base)
        try:
            with dspy.context(lm=lm), pytest.raises(LMTransportError, match="mid-chunk"):
                ClioReAct("question -> answer", tools=[])(question="Answer")
        finally:
            run_on_lm_loop(lm.aclose)
        assert len(requests) == 1


def test_stalled_ui_boundary_is_cancelled_instead_of_appending_to_a_later_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.runtime import lm_activity

    appended: list[bool] = []
    cancelled: list[bool] = []

    class StalledBoundary:
        def result(self, timeout: float) -> None:
            raise TimeoutError("emitter loop stalled")

        def cancel(self) -> None:
            cancelled.append(True)

    def schedule(coroutine: Any, loop: Any) -> StalledBoundary:
        coroutine.close()
        return StalledBoundary()

    monkeypatch.setattr(lm_activity.asyncio, "run_coroutine_threadsafe", schedule)
    token = lm_activity._LIVE_CHUNK_EMITTER.set((object(), None, None))
    try:
        with pytest.raises(TimeoutError, match="stalled"):
            lm_activity.after_live_chunks(lambda: appended.append(True))
    finally:
        lm_activity._LIVE_CHUNK_EMITTER.reset(token)
    assert cancelled == [True]
    assert appended == []
