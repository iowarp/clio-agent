"""Stateful Codex SDK transport: one thread per conversation, deltas only.

Pins the send-plan semantics (:mod:`clio_agent.providers.codex.sdk_stateful`), the
client's thread continuation / typed thread-lost / compaction detection
(:class:`clio_agent.providers.codex.sdk_client.CodexSDKClient`), the transport's
typed reset-and-resend on a lost thread, and the cached-token pass-through. Every
test fakes the ``openai_codex`` boundary; no real runtime or network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import context as gact_context
from clio_agent.providers import stateful_common
from clio_agent.providers.codex import sdk_client, sdk_stateful, sdk_stream, sdk_transport
from clio_agent.providers.codex.sdk_client import CodexThreadLostError
from clio_agent.providers.codex.sdk_stateful import resolve_codex_send

SYSTEM = {"role": "system", "content": "sys"}
HEAD = {"role": "user", "content": "[[ ## question ## ]]\nWhat is it?"}
TAIL = {"role": "user", "content": "[[ ## tools ## ]]\n[...]\n\nRespond with ..."}


def _step(i: int) -> list[dict[str, Any]]:
    return [
        {"role": "assistant", "content": f"[[ ## next_thought ## ]]\nstep {i}"},
        {"role": "user", "content": f"[[ ## tool_call_results ## ]]\nresult {i}"},
    ]


def _render(steps: int) -> list[dict[str, Any]]:
    """A DSPy-shaped render: head, the appended steps, then the static tail."""
    body = [SYSTEM, HEAD]
    for i in range(steps):
        body += _step(i)
    return [*body, TAIL]


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex_home"))
    sdk_stateful._reset_for_tests()
    yield
    sdk_stateful._reset_for_tests()


@pytest.fixture
def loop_scope() -> Iterator[None]:
    """An active ReAct loop: GACT session + react scope + per-forward stateful scope."""
    session_token = gact_context.set_session_id("sess-a")
    scope_token = gact_context.set_react_scope("main", "react")
    try:
        with stateful_common.stateful_scope():
            yield
    finally:
        gact_context.reset(scope_token)
        gact_context.reset(session_token)


def _plan(messages: list[dict[str, Any]], sink: list[dict[str, Any]] | None = None) -> Any:
    def _audit(stage: str, **row: Any) -> None:
        if sink is not None:
            sink.append({"stage": stage, **row})

    return resolve_codex_send(
        messages=messages, model="gpt-x", cwd="/tmp", effort="high", audit=_audit
    )


# --------------------------------------------------------------------------- #
# Send plans
# --------------------------------------------------------------------------- #


def test_inert_outside_a_react_loop_is_a_plain_full_send() -> None:
    send = _plan(_render(1))
    assert send.engaged is False
    assert send.mode == "full"
    assert send.thread_id is None
    assert send.messages == _render(1)


def test_first_call_is_a_typed_full_send_then_append_only_calls_are_deltas(
    loop_scope: None,
) -> None:
    rows: list[dict[str, Any]] = []
    first = _plan(_render(0), rows)
    assert (first.engaged, first.mode, first.reason) == (True, "full", "first_call")
    first.bind_thread("thread-1")

    second = _plan(_render(1), rows)
    assert second.mode == "delta"
    assert second.thread_id == "thread-1"
    # Only the newly appended step is sent; DSPy's moving tail is not re-sent.
    assert second.messages == _step(0)

    third = _plan(_render(2), rows)
    assert (third.mode, third.thread_id, third.messages) == ("delta", "thread-1", _step(1))

    assert [r["stateful_mode"] for r in rows] == ["full", "delta", "delta"]
    assert rows[0]["reason"] == "first_call"
    assert all(r["provider"] == "codex_sdk" for r in rows)


def test_rewritten_history_opens_a_new_thread_and_archives_the_old_one(
    loop_scope: None,
) -> None:
    _plan(_render(0)).bind_thread("thread-1")
    _plan(_render(1))
    rewritten = [SYSTEM, {"role": "user", "content": "different head"}, TAIL]
    send = _plan(rewritten)
    assert (send.mode, send.reason, send.thread_id) == ("full", "prefix_mismatch", None)
    send.bind_thread("thread-2")
    assert sdk_stateful.take_threads_to_archive() == ["thread-1"]


def test_lost_thread_resets_typed_session_evicted(loop_scope: None) -> None:
    _plan(_render(0)).bind_thread("thread-1")
    delta = _plan(_render(1))
    delta.note_thread_lost()
    again = _plan(_render(1))
    assert (again.mode, again.reason) == ("full", "session_evicted")
    assert sdk_stateful.take_threads_to_archive() == ["thread-1"]


def test_provider_compaction_resets_typed(loop_scope: None) -> None:
    first = _plan(_render(0))
    first.bind_thread("thread-1")
    first.note_provider_compacted()
    nxt = _plan(_render(1))
    assert (nxt.mode, nxt.reason) == ("full", "provider_compacted")


def test_mid_flight_error_resets_typed_provider_error(loop_scope: None) -> None:
    first = _plan(_render(0))
    first.bind_thread("thread-1")
    first.note_error()
    nxt = _plan(_render(1))
    assert (nxt.mode, nxt.reason) == ("full", "provider_error")


def test_arc_op_reset_reaches_the_conversation_the_forward_drove(loop_scope: None) -> None:
    _plan(_render(0)).bind_thread("thread-1")
    assert stateful_common.note_prefix_reset_for_active_scope("ops_reset") is True
    nxt = _plan(_render(1))
    assert (nxt.mode, nxt.reason) == ("full", "ops_reset")


def test_the_thread_survives_across_forwards_of_the_same_conversation() -> None:
    session_token = gact_context.set_session_id("sess-a")
    scope_token = gact_context.set_react_scope("main", "react")
    try:
        with stateful_common.stateful_scope():
            _plan(_render(0)).bind_thread("thread-1")
        # A later turn: a NEW per-forward scope, same session + agent scope.
        with stateful_common.stateful_scope():
            nxt = _plan(_render(1))
        assert (nxt.mode, nxt.thread_id) == ("delta", "thread-1")
    finally:
        gact_context.reset(scope_token)
        gact_context.reset(session_token)


def test_conversations_are_isolated_per_agent_scope(loop_scope: None) -> None:
    _plan(_render(0)).bind_thread("thread-main")
    child_token = gact_context.set_react_scope("child", "react")
    try:
        child = _plan(_render(1))
        assert (child.mode, child.reason) == ("full", "first_call")
    finally:
        gact_context.reset(child_token)


def test_a_delta_with_no_bound_thread_resets_typed(loop_scope: None) -> None:
    # The opening send failed before the SDK reported a thread: nothing to continue.
    _plan(_render(0))
    nxt = _plan(_render(1))
    assert (nxt.mode, nxt.reason) == ("full", "session_evicted")


# --------------------------------------------------------------------------- #
# The SDK client: thread continuation, typed thread-lost, compaction detection
# --------------------------------------------------------------------------- #


class _FakeTurn:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    async def stream(self) -> Any:
        for event in self._events:
            yield event

    async def interrupt(self) -> None:
        return None


class _FakeThread:
    def __init__(self, thread_id: str, events: list[Any]) -> None:
        self.id = thread_id
        self.inputs: list[Any] = []
        self._events = events

    async def turn(self, turn_input: Any, **_kw: Any) -> _FakeTurn:
        self.inputs.append(turn_input)
        return _FakeTurn(self._events)


def _fake_client_cls(events: list[Any], started: list[_FakeThread]) -> type:
    class _FakeClient:
        def __init__(self, *_a: object, **_kw: object) -> None:
            self.archived: list[str] = []

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *_a: object) -> bool:
            return False

        async def thread_start(self, **kwargs: Any) -> _FakeThread:
            assert kwargs["config"]["model_auto_compact_token_limit"] == (
                sdk_client.NO_AUTO_COMPACT_TOKEN_LIMIT
            )
            thread = _FakeThread(f"thread-{len(started) + 1}", events)
            started.append(thread)
            return thread

        async def thread_archive(self, thread_id: str) -> None:
            self.archived.append(thread_id)

        async def close(self) -> None:
            return None

    return _FakeClient


def _delta(text: str) -> Any:
    return SimpleNamespace(method="item/agentMessage/delta", payload=SimpleNamespace(delta=text))


async def _drain(client: Any, **kwargs: Any) -> list[Any]:
    return [event async for event in client.stream(**kwargs)]


@pytest.mark.asyncio
async def test_client_keeps_a_thread_and_continues_it(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[_FakeThread] = []
    monkeypatch.setattr(sdk_client, "AsyncCodex", _fake_client_cls([_delta("ok")], started))
    client = sdk_client.CodexSDKClient()
    opened: list[str] = []
    common = {"images": None, "model": "gpt-x", "cwd": None, "effort": None, "timeout": 30.0}
    try:
        await _drain(client, prompt="full", keep_thread=True, on_thread=opened.append, **common)
        await _drain(client, prompt="delta", thread_id="thread-1", **common)
        assert opened == ["thread-1"]
        assert len(started) == 1
        assert started[0].inputs == ["full", "delta"]
    finally:
        client.close_blocking()


@pytest.mark.asyncio
async def test_unknown_thread_raises_typed_and_keeps_the_shared_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[_FakeThread] = []
    monkeypatch.setattr(sdk_client, "AsyncCodex", _fake_client_cls([_delta("ok")], started))
    client = sdk_client.CodexSDKClient()
    common = {"images": None, "model": "gpt-x", "cwd": None, "effort": None, "timeout": 30.0}
    try:
        await _drain(client, prompt="warm", **common)
        generation = client._generation
        with pytest.raises(CodexThreadLostError):
            await _drain(client, prompt="delta", thread_id="thread-gone", **common)
        # A lost thread is not a broken runtime: the shared client is NOT torn down.
        await asyncio.sleep(0.05)
        assert client._generation == generation
        assert client._client is not None
    finally:
        client.close_blocking()


@pytest.mark.asyncio
async def test_codex_compaction_is_reported_once(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[_FakeThread] = []
    compacted_event = SimpleNamespace(method="thread/compacted", payload=SimpleNamespace())
    events = [_delta("a"), compacted_event, compacted_event, _delta("b")]
    monkeypatch.setattr(sdk_client, "AsyncCodex", _fake_client_cls(events, started))
    client = sdk_client.CodexSDKClient()
    calls: list[bool] = []
    try:
        await _drain(
            client,
            prompt="p",
            images=None,
            model="gpt-x",
            cwd=None,
            effort=None,
            timeout=30.0,
            on_compacted=lambda: calls.append(True),
        )
        assert calls == [True]
    finally:
        client.close_blocking()


# --------------------------------------------------------------------------- #
# Transport: typed reset + one full resend on a lost thread; cached tokens
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_transport_resends_in_full_after_a_lost_thread(
    loop_scope: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sends: list[Any] = []

    async def _fake_astream(**kwargs: Any) -> Any:
        send = kwargs["send"]
        sends.append((send.mode, send.reason, kwargs["prompt"]))
        if send.mode == "delta":
            raise CodexThreadLostError(send.thread_id or "")
        send.bind_thread(f"thread-{len(sends)}")
        yield {"text": "ok", "is_finished": True, "usage": None}

    monkeypatch.setattr(sdk_transport, "astream_sdk", _fake_astream)

    async def _collect(messages: list[dict[str, Any]]) -> list[Any]:
        return [c async for c in sdk_transport._astream_planned(messages, "gpt-x", {}, None)]

    await _collect(_render(0))
    await _collect(_render(1))
    assert [(mode, reason) for mode, reason, _ in sends] == [
        ("full", "first_call"),
        ("delta", None),
        ("full", "session_evicted"),
    ]
    # The resend carries the whole conversation, not the dropped delta.
    assert "What is it?" in sends[-1][2]


def test_usage_carries_cached_input_tokens() -> None:
    usage = {"input_tokens": 1000, "cached_input_tokens": 900, "output_tokens": 10}
    chunk = sdk_stream.usage_chunk(usage)
    assert chunk is not None
    assert chunk["prompt_tokens_details"] == {"cached_tokens": 900}
    response = sdk_transport._build_model_response(text="x", model="gpt-x", usage_payload=usage)
    assert response.usage.prompt_tokens_details.cached_tokens == 900  # type: ignore[union-attr]
