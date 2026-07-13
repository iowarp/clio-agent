"""Pooled claude_code SDK streaming transport (#891, post delta-strip).

These pin the surviving contract on the *real* objects — the streaming client
pool and the live ``_astream_sdk`` path with a fake SDK client that records every
``query(prompt, session_id)``:

(a) the pooled client is constructed + connected ONCE across N calls (the
    measured connect-reuse win);
(b) every call sends its FULL prompt under a FRESH ``session_id`` — the per-call
    conversation boundary that makes cross-call/cross-expert context bleed
    impossible (server-side content-prefix caching supplies cache_read, proven
    live: 12K→184K across a turn);
(c) the kill-switch restores the pre-#891 per-call behaviour byte-for-byte;
(d) the pooled client survives separate ``asyncio.run()`` caller loops (BLOCKER);
(e) an abnormal end drops the poisoned client (BLOCKER);
(f) a mid-stream SDK/CLI death becomes a TYPED, audited, transient error the LM
    retry layer re-issues (the #891 live-crash fix) — never an opaque turn kill.

The deleted delta/session-registry layer is history (module docstring tells it);
these tests intentionally contain no session-continuation pins.

Each load-bearing pin carries an inline SABOTAGE note: the exact change that makes
it go red, proving the assertion is not vacuous.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest

from clio_agent.providers import claude_code_litellm, claude_code_sessions
from clio_agent.providers.claude_code_sessions import _reset_sessions_for_tests


@pytest.fixture(autouse=True)
def _clean_pool() -> Any:
    """Every test starts and ends with an empty streaming client pool."""
    _reset_sessions_for_tests()
    yield
    _reset_sessions_for_tests()


# --------------------------------------------------------------------------- #
# Fake SDK: connected clients record their queries, plus construction counters
# so connect-reuse is directly observable.
# --------------------------------------------------------------------------- #
def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"constructed": 0, "connected": 0, "clients": []}

    class FakeTextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeStreamEvent:
        def __init__(self, event: dict[str, Any]) -> None:
            self.event = event

    class FakeAssistantMessage:
        def __init__(self) -> None:
            self.content = [FakeTextBlock("Answer")]
            self.usage = {"input_tokens": 2, "output_tokens": 3}
            self.stop_reason = "end_turn"

    class FakeResultMessage:
        usage = {"input_tokens": 2, "output_tokens": 3}
        stop_reason = "end_turn"
        result = "Answer"
        is_error = False

    class FakeClaudeAgentOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class FakeClaudeSDKClient:
        def __init__(self, options: FakeClaudeAgentOptions) -> None:
            state["constructed"] += 1
            self.options = options
            self.queries: list[tuple[str, str]] = []
            state["clients"].append(self)

        async def connect(self) -> None:
            state["connected"] += 1

        async def disconnect(self) -> None:
            return None

        async def query(self, prompt: str, session_id: str = "default") -> None:
            self.queries.append((prompt, session_id))

        async def receive_response(self) -> Any:
            yield FakeStreamEvent(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Ans"}}
            )
            yield FakeStreamEvent(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "wer"}}
            )
            yield FakeAssistantMessage()
            yield FakeResultMessage()

    fake_sdk = ModuleType("claude_agent_sdk")
    fake_sdk.AssistantMessage = FakeAssistantMessage
    fake_sdk.ClaudeAgentOptions = FakeClaudeAgentOptions
    fake_sdk.ClaudeSDKClient = FakeClaudeSDKClient
    fake_sdk.ResultMessage = FakeResultMessage
    fake_sdk.StreamEvent = FakeStreamEvent
    fake_sdk.TextBlock = FakeTextBlock
    import sys  # noqa: PLC0415

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
    return state


async def _drain(prompt: str) -> None:
    """Run one streaming call to completion (discarding chunks)."""
    async for _ in claude_code_litellm._astream_sdk(
        prompt=prompt,
        model="haiku",
        timeout=5.0,
        cwd="/w",
    ):
        pass


def _all_queries(state: dict[str, Any]) -> list[tuple[str, str]]:
    """Flatten every (payload, session_id) recorded across all fake clients."""
    return [q for client in state["clients"] for q in client.queries]


# --------------------------------------------------------------------------- #
# Reason-catalog discipline.
# --------------------------------------------------------------------------- #
def test_transport_failure_payload_is_typed_and_rejects_unknown_reasons() -> None:
    """Catalog style (#775): a known reason yields queryable structured data; a
    typo raises instead of silently producing an empty reason."""
    payload = claude_code_sessions.transport_failure_payload("send_failed", "boom")
    assert payload["reason"] == "send_failed"
    assert payload["category"] == "session_transport_error"
    assert payload["message"] == "boom"
    # SABOTAGE: return {} for unknown reasons instead of raising -> red.
    with pytest.raises(ValueError, match="Unknown transport failure reason"):
        claude_code_sessions.transport_failure_payload("not_a_reason")


# --------------------------------------------------------------------------- #
# Live streaming-path pins (real _astream_sdk + fake SDK client).
# --------------------------------------------------------------------------- #
async def test_stream_client_constructed_once_across_calls(monkeypatch) -> None:
    """(a) live: N calls construct + connect the pooled client exactly once."""
    state = _install_fake_sdk(monkeypatch)
    for i in range(4):
        await _drain("HEADER-STABLE-PREFIX" + "\nstep" * i)

    assert state["constructed"] == 1  # SABOTAGE: build a fresh client per call -> 4 -> red
    assert state["connected"] == 1  # connect reused, not paid per call


async def test_stream_each_call_sends_full_prompt_under_fresh_session_id(monkeypatch) -> None:
    """(b) live: every call is its own SDK conversation — the FULL prompt under a
    FRESH session_id. Reusing a session_id while sending full prompts would stack
    duplicated context into one conversation (the reason the dead delta layer
    reset instead of continuing); distinct ids also mean one expert's stream can
    never land in another's conversation."""
    state = _install_fake_sdk(monkeypatch)
    await _drain("SHARED-HEADER-PREFIX\nalice-step0")
    await _drain("SHARED-HEADER-PREFIX\nalice-step0\nalice-step1")
    await _drain("SHARED-HEADER-PREFIX\nbob-step0")

    queries = _all_queries(state)
    assert [p for p, _ in queries] == [
        "SHARED-HEADER-PREFIX\nalice-step0",
        "SHARED-HEADER-PREFIX\nalice-step0\nalice-step1",  # SABOTAGE: send a suffix delta -> red
        "SHARED-HEADER-PREFIX\nbob-step0",
    ]
    sids = [sid for _, sid in queries]
    # SABOTAGE: reuse one session_id across calls (stable-session continuation) -> red.
    assert len(set(sids)) == 3
    # And still ONE pooled client served all three conversations.
    assert state["constructed"] == 1


async def test_stream_kill_switch_restores_per_call(monkeypatch) -> None:
    """(c) live: with reuse OFF, each call builds a fresh client + fresh session + full prompt."""
    state = _install_fake_sdk(monkeypatch)
    monkeypatch.setenv("CLIO_CLAUDE_CODE_SESSION_REUSE", "false")
    from clio_agent import conf  # noqa: PLC0415

    conf.reload()
    await _drain("HEADER-STABLE-PREFIX\nstep0")
    await _drain("HEADER-STABLE-PREFIX\nstep0\nstep1")

    # Byte-for-byte the original per-call behaviour: a fresh client each call, the
    # FULL prompt each call, and a fresh random session_id.
    assert state["constructed"] == 2  # SABOTAGE: honour the flag as ON -> 1 -> red
    queries = _all_queries(state)
    assert [p for p, _ in queries] == [
        "HEADER-STABLE-PREFIX\nstep0",
        "HEADER-STABLE-PREFIX\nstep0\nstep1",
    ]
    assert queries[0][1] != queries[1][1]  # distinct per-call session ids
    conf.reload()


# --------------------------------------------------------------------------- #
# BLOCKER pin: the pooled streaming client must survive the per-call
# ``asyncio.run()`` loops the token-liveness driver spins up. A loop-strict fake
# (the real SDK's behaviour: transports bind to the connecting loop) makes call 2
# go red if the client is cached without loop affinity on the caller's loop.
# --------------------------------------------------------------------------- #
def _install_loop_strict_fake_sdk(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake SDK whose client records its connect loop and REFUSES to be driven from
    any other (or a closed) loop — reproducing the real subprocess-transport hazard."""
    import asyncio as _asyncio  # noqa: PLC0415

    state: dict[str, Any] = {"constructed": 0}

    class FakeTextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeStreamEvent:
        def __init__(self, event: dict[str, Any]) -> None:
            self.event = event

    class FakeAssistantMessage:
        content = [FakeTextBlock("Answer")]
        usage = {"input_tokens": 2, "output_tokens": 3}
        stop_reason = "end_turn"

    class FakeResultMessage:
        usage = {"input_tokens": 2, "output_tokens": 3}
        stop_reason = "end_turn"
        result = "Answer"
        is_error = False

    class FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class FakeClient:
        def __init__(self, options: FakeOptions) -> None:
            state["constructed"] += 1
            self._loop: Any = None

        async def connect(self) -> None:
            self._loop = _asyncio.get_running_loop()

        async def disconnect(self) -> None:
            return None

        def _check_loop(self) -> None:
            running = _asyncio.get_running_loop()
            if self._loop is not running or self._loop.is_closed():
                raise RuntimeError("Event loop is closed")

        async def query(self, prompt: str, session_id: str = "default") -> None:
            self._check_loop()

        async def receive_response(self) -> Any:
            self._check_loop()
            yield FakeStreamEvent(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Ans"}}
            )
            yield FakeResultMessage()

    fake_sdk = ModuleType("claude_agent_sdk")
    fake_sdk.AssistantMessage = FakeAssistantMessage
    fake_sdk.ClaudeAgentOptions = FakeOptions
    fake_sdk.ClaudeSDKClient = FakeClient
    fake_sdk.ResultMessage = FakeResultMessage
    fake_sdk.StreamEvent = FakeStreamEvent
    fake_sdk.TextBlock = FakeTextBlock
    import sys  # noqa: PLC0415

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
    return state


def test_pooled_client_survives_separate_asyncio_run_loops(monkeypatch) -> None:
    """(d, BLOCKER) The live driver runs EVERY expert LM call under its own
    ``asyncio.run()``. The pooled client must therefore live on a stable loop, not
    the caller's — otherwise call 2 hits 'Event loop is closed' and the feature
    silently degrades to per-call transport."""
    import asyncio as _asyncio  # noqa: PLC0415

    state = _install_loop_strict_fake_sdk(monkeypatch)

    # Two SEPARATE asyncio.run() invocations — exactly io_logging._clio_streamed_call.
    _asyncio.run(_drain("HEADER-STABLE-PREFIX\nstep0"))
    # SABOTAGE: cache the client on the CALLER loop (old ensure_connected) -> call 2
    # runs against a closed loop -> RuntimeError('Event loop is closed') -> red.
    _asyncio.run(_drain("HEADER-STABLE-PREFIX\nstep0\nstep1"))

    assert state["constructed"] == 1  # one connect, reused across both run() loops


# --------------------------------------------------------------------------- #
# BLOCKER pin: a mid-cycle abnormal end must DROP the pooled client so its
# leftover response can never bleed into the next (possibly different-expert) call.
# --------------------------------------------------------------------------- #
async def test_pooled_client_reset_on_abnormal_end(monkeypatch) -> None:
    """(e) A transport error mid-receive evicts the pooled client; the next call
    reconnects on a FRESH client rather than reusing the poisoned connection."""
    state: dict[str, Any] = {"constructed": 0, "clients": []}

    class FakeTextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeStreamEvent:
        def __init__(self, event: dict[str, Any]) -> None:
            self.event = event

    class FakeAssistantMessage:
        content = [FakeTextBlock("Answer")]
        usage = {"input_tokens": 1, "output_tokens": 1}
        stop_reason = "end_turn"

    class FakeResultMessage:
        usage = {"input_tokens": 1, "output_tokens": 1}
        stop_reason = "end_turn"
        result = "Answer"
        is_error = False

    class FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class FakeClient:
        def __init__(self, options: FakeOptions) -> None:
            state["constructed"] += 1
            self.index = len(state["clients"])
            state["clients"].append(self)

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def query(self, prompt: str, session_id: str = "default") -> None:
            return None

        async def receive_response(self) -> Any:
            if self.index == 0:  # first client: fail mid-stream
                yield FakeStreamEvent(
                    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "X"}}
                )
                raise RuntimeError("transport boom")
            yield FakeResultMessage()

    fake_sdk = ModuleType("claude_agent_sdk")
    fake_sdk.AssistantMessage = FakeAssistantMessage
    fake_sdk.ClaudeAgentOptions = FakeOptions
    fake_sdk.ClaudeSDKClient = FakeClient
    fake_sdk.ResultMessage = FakeResultMessage
    fake_sdk.StreamEvent = FakeStreamEvent
    fake_sdk.TextBlock = FakeTextBlock
    import sys  # noqa: PLC0415

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    with pytest.raises(RuntimeError):  # transport error propagates to the repair loop
        await _drain("HEADER-STABLE-PREFIX\nstep0")
    # SABOTAGE: skip _areset_client on abnormal end -> call 2 reuses client 0 -> red.
    await _drain("HEADER-STABLE-PREFIX\nstep0\nstep1")
    assert state["constructed"] == 2  # poisoned client dropped, a fresh one connected


async def test_midstream_sdk_death_becomes_typed_transient_error(monkeypatch) -> None:
    """(f, #891 live-crash) A pooled CLI subprocess death mid-stream surfaces as a
    SDK ``ClaudeSDKError``; the provider must translate it to a TYPED, audited
    transient ``ClaudeCodeExecError`` the LM retry layer recognises — not fail the
    turn on an opaque ``Command failed with exit code 1`` the classifier ignores."""
    from clio_agent.lm.io_logging import _is_transient_provider_error  # noqa: PLC0415

    class FakeSdkError(Exception):
        """Stands in for claude_agent_sdk.ClaudeSDKError (ProcessError base)."""

    class FakeStreamEvent:
        def __init__(self, event: dict[str, Any]) -> None:
            self.event = event

    class FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class FakeClient:
        def __init__(self, options: FakeOptions) -> None:
            return None

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def query(self, prompt: str, session_id: str = "default") -> None:
            return None

        async def receive_response(self) -> Any:
            yield FakeStreamEvent(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "X"}}
            )
            raise FakeSdkError("Command failed with exit code 1")

    fake_sdk = ModuleType("claude_agent_sdk")
    fake_sdk.AssistantMessage = type("AssistantMessage", (), {})
    fake_sdk.ClaudeAgentOptions = FakeOptions
    fake_sdk.ClaudeSDKClient = FakeClient
    fake_sdk.ResultMessage = type("ResultMessage", (), {})
    fake_sdk.StreamEvent = FakeStreamEvent
    fake_sdk.TextBlock = type("TextBlock", (), {})
    fake_sdk.ClaudeSDKError = FakeSdkError  # the transport-death base the fix keys on
    import sys  # noqa: PLC0415

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    # Capture the audit half (no silent fallback): the death must emit a typed
    # provider.transport_error row carrying the send_failed catalog payload.
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(claude_code_sessions, "stream_audit_enabled", lambda: True)
    monkeypatch.setattr(
        claude_code_sessions,
        "stream_audit",
        lambda event, **fields: rows.append({"event": event, **fields}),
    )

    with pytest.raises(claude_code_litellm.ClaudeCodeExecError) as excinfo:
        await _drain("HEADER-STABLE-PREFIX\nstep0")
    # SABOTAGE: drop the `except transient_transport_error_types()` arm -> the raw
    # FakeSdkError propagates (not a ClaudeCodeExecError) and this pin goes red.
    assert claude_code_sessions.TRANSIENT_TRANSPORT_MARKER in str(excinfo.value)
    # The LM retry layer must classify it transient (so the call re-issues on a
    # fresh connection instead of failing the turn).
    assert _is_transient_provider_error(excinfo.value)
    # And the structured reason reached the audit highway.
    error_rows = [r for r in rows if r["event"] == "provider.transport_error"]
    assert error_rows and error_rows[-1]["reason"] == "send_failed"
    assert error_rows[-1]["category"] == "session_transport_error"
