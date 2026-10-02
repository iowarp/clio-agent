"""Codex direct: a dropped WebSocket stream is retried by DSPy, loudly, never replayed.

Found live (early adopters' deep-research scenario, 2026-10-02): Codex closed the
connection during a long reply and the turn died with "Codex closed the connection
during the reply (no close frame)". The real direct engine, DSPy's own execution and
retry policy and lm15's Responses wire run here; only the socket is faked. Pins:

* a drop the caller has seen nothing of -- before any event, or after events that are
  not visible output (the response's start), or mid-reply on a call nobody streams --
  is a typed retryable :class:`CodexStreamDroppedError`: DSPy re-issues the call (a
  FULL send on a fresh socket, never a delta against the response that never
  completed), each retry logged ``reason=codex_stream_dropped attempt=N/M``;
* a kept socket found closed is resent once in full (stale-socket recovery); a second
  drop is the typed retryable error, not an untyped engine failure;
* a drop after visible output was streamed is NOT replayed (DSPy's contract: a retry
  would put a second reply after the first in the live transcript): it reaches the
  user as the typed ``LMServerError``, logged ``action=not_retried_partial_output``;
* exhausted retries reach the user as the typed ``LMServerError``.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

import anyio
import dspy
import pytest
from dspy.utils.exceptions import LMServerError, LMUnexpectedError

from clio_agent.lm.policy import lm_retries
from clio_agent.providers.codex.stream_errors import CodexStreamDroppedError
from tests.test_providers import test_codex_direct_engine as base
from tests.test_providers.test_codex_direct_engine import (
    HEAD,
    Harness,
    _answer,
    _codex_lm,
    _request,
    _retry_lines,
    _service_restart,
    _step,
)

# The direct engine suite's fixtures: the faked socket, a ClioReAct-like loop scope.
harness = base.harness
loop_scope = base.loop_scope

_DROP_REASON = "reason=codex_stream_dropped "


def _drop_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [line for line in _retry_lines(caplog) if "codex_stream_dropped" in line]


def _texts(chunks: list[Any]) -> str:
    return "".join(chunk.choices[0]["delta"].get("content") or "" for chunk in chunks)


def _streamed(lm: Any, request: Any, chunks: list[Any] | None = None) -> tuple[Any, list[Any]]:
    """One call streamed to a listener the way ClioReAct streams it (``send_stream``)."""
    seen: list[Any] = [] if chunks is None else chunks

    async def run() -> Any:
        send, receive = anyio.create_memory_object_stream(math.inf)

        async def consume() -> None:
            async with receive:
                async for chunk in receive:
                    seen.append(chunk)

        try:
            async with anyio.create_task_group() as group:
                group.start_soon(consume)
                try:
                    with dspy.context(send_stream=send):
                        return await lm.acall(request)
                finally:
                    await send.aclose()
        except BaseExceptionGroup as group_error:
            [leaf] = group_error.exceptions
            raise leaf from None

    return asyncio.run(run()), seen


def test_a_drop_mid_reply_of_an_unstreamed_call_is_retried_and_logged(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    started = _answer("resp_1", "partial")[:2]
    harness.script[:] = [[*started, _service_restart()], "recovered"]
    lm = _codex_lm(monkeypatch)
    with caplog.at_level("INFO", logger="clio_agent.providers.codex.stream_errors"):
        out = lm("summarize the findings")

    assert out == ["recovered"]  # the dropped attempt's text is not in the reply
    assert len(harness.sockets) == 2
    failed, recovered = _drop_lines(caplog)
    assert _DROP_REASON in failed
    assert f"attempt=1/{lm_retries() + 1}" in failed
    assert "action=dspy_retries" in failed
    assert "reason=codex_stream_dropped_recovered attempt=2" in recovered


def test_a_drop_before_visible_output_of_a_streamed_call_is_retried(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    loop_scope: None,
) -> None:
    """The live shape: a delta send on the kept socket, the response started, then the
    connection dropped. The retry is a full send on a fresh socket; the listener sees
    only the retry's text, once."""
    created = _answer("resp_2", "")[:1]
    harness.script[:] = ["one", [*created, _service_restart()], "two"]
    lm = _codex_lm(monkeypatch)
    _streamed(lm, _request(HEAD))
    with caplog.at_level("INFO", logger="clio_agent.providers.codex.stream_errors"):
        response, chunks = _streamed(lm, _request(HEAD, *_step(0)))

    assert response.text == "two"
    assert _texts(chunks) == "two"
    kept, fresh = harness.sockets
    assert kept.frames[-1]["previous_response_id"] == "resp_1"  # the delta that dropped
    [resend] = fresh.frames
    assert "previous_response_id" not in resend  # never a delta on a response never done
    assert len(resend["input"]) > len(kept.frames[-1]["input"])
    failed, _recovered = _drop_lines(caplog)
    assert "action=dspy_retries" in failed


def test_a_second_drop_before_any_output_is_a_typed_retry(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    loop_scope: None,
) -> None:
    """A kept socket found closed is resent in full; when the fresh socket drops too,
    the call is DSPy's typed retry -- not an untyped engine failure."""
    harness.script[:] = ["one", [_service_restart()], [_service_restart()], "two"]
    lm = _codex_lm(monkeypatch)
    _streamed(lm, _request(HEAD))
    with caplog.at_level("INFO", logger="clio_agent.providers.codex.stream_errors"):
        response, chunks = _streamed(lm, _request(HEAD, *_step(0)))

    assert response.text == "two"
    assert _texts(chunks) == "two"
    assert len(harness.sockets) == 3  # kept, stale-socket resend, DSPy's retry
    assert all("previous_response_id" not in s.frames[0] for s in harness.sockets[1:])
    [failed, _recovered] = _drop_lines(caplog)
    assert "attempt=1/" in failed


def test_a_drop_after_visible_output_is_not_replayed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """DSPy never re-issues a call whose output the user already saw: the typed error
    reaches the user, the partial reply is shown once, and the log says why."""
    started = _answer("resp_1", "partial")[:2]
    harness.script[:] = [[*started, _service_restart()], "never sent"]
    lm = _codex_lm(monkeypatch)
    chunks: list[Any] = []
    with (
        caplog.at_level("INFO", logger="clio_agent.providers.codex.stream_errors"),
        pytest.raises(LMServerError, match="Codex closed the connection during the reply"),
    ):
        _streamed(lm, _request(HEAD), chunks)

    assert _texts(chunks) == "partial"
    assert len(harness.sockets) == 1
    [line] = _drop_lines(caplog)
    assert "action=not_retried_partial_output" in line


def test_exhausted_drop_retries_reach_the_user_typed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    attempts = lm_retries() + 1
    harness.script[:] = [[_service_restart()]] * attempts
    lm = _codex_lm(monkeypatch)
    with (
        caplog.at_level("INFO", logger="clio_agent.providers.codex.stream_errors"),
        pytest.raises(LMServerError, match="Codex closed the connection") as caught,
    ):
        lm("summarize the findings, again")

    assert not isinstance(caught.value, LMUnexpectedError)
    assert len(harness.sockets) == attempts  # bounded by DSPy's retry count
    lines = _drop_lines(caplog)
    assert [f"attempt={n}/{attempts}" in line for n, line in enumerate(lines, 1)] == [
        True
    ] * attempts
    assert "action=retries_exhausted" in lines[-1]


def test_the_drop_is_an_lm15_server_error_dspy_retries() -> None:
    from dspy.clients.errors import wrap_error
    from dspy.lm15 import ServerError
    from dspy.utils.exceptions import is_retryable_lm_error

    error = CodexStreamDroppedError("Codex closed the connection")
    assert isinstance(error, ServerError)
    assert error.reason == "codex_stream_dropped"
    assert is_retryable_lm_error(wrap_error(error, model="codex_direct/gpt-6-sol"))
