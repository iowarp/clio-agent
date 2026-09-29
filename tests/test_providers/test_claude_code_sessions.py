"""Pooled claude_code SDK transport under the Claude Code engine.

These pin the pool contract on the *real* objects -- the streaming client pool and
the engine -- with a fake SDK client that records every ``query(prompt, session_id)``:

(a) the pooled client is constructed + connected ONCE across N calls;
(b) outside an agent loop every call sends its FULL transcript under a FRESH
    ``session_id`` (no kept conversation to continue -- one expert's stream can never
    land in another's conversation);
(c) S2 (B1): the pool key is the ACTIVE GACT session -- distinct sessions never share
    a connection;
(d) the pooled client survives separate ``asyncio.run()`` caller loops (BLOCKER);
(e) an abnormal end drops the poisoned client (BLOCKER);
(f) a mid-stream SDK/CLI death becomes a TYPED, audited, retryable ``dspy.lm15``
    server error carrying the CLI's stderr tail -- DSPy re-issues it on a fresh
    connection instead of the turn dying on an opaque error.

Each load-bearing pin carries an inline SABOTAGE note: the exact change that makes it
go red, proving the assertion is not vacuous.
"""

from __future__ import annotations

import asyncio
from typing import Any

import dspy
import pytest
from dspy.lm15 import Message, ServerError

from clio_agent.providers import claude_code_engine, claude_code_sessions
from clio_agent.providers.claude_code_engine import AsyncClaudeCodeEngine, ClaudeCodeEngine
from clio_agent.providers.claude_code_sessions import _reset_sessions_for_tests
from tests import _fake_claude_sdk as fake


@pytest.fixture(autouse=True)
def _clean_pool() -> Any:
    """Every test starts and ends with an empty client pool and conversation registry."""
    _reset_sessions_for_tests()
    claude_code_engine._CONVERSATIONS.clear_for_tests()
    yield
    _reset_sessions_for_tests()
    claude_code_engine._CONVERSATIONS.clear_for_tests()


async def _drain(prompt: str) -> None:
    """Run one engine call to completion."""
    await fake.drive(fake.request(Message.user(prompt)))


class _SdkError(Exception):
    """Stands in for ``claude_agent_sdk.ClaudeSDKError`` (the process-death base)."""


def _install_dying_sdk(monkeypatch: pytest.MonkeyPatch, *, stderr_line: str = "") -> fake.FakeSDK:
    """A fake SDK whose FIRST query dies mid-stream with an SDK error; later ones answer."""
    sdk = fake.install(
        monkeypatch,
        script=[[fake.text("X"), _SdkError("Command failed with exit code 1")]],
    )
    import sys  # noqa: PLC0415

    module = sys.modules["claude_agent_sdk"]
    module.ClaudeSDKError = _SdkError  # type: ignore[attr-defined]
    if stderr_line:
        base = module.ClaudeSDKClient  # type: ignore[attr-defined]

        class _StderrClient(base):  # type: ignore[misc, valid-type]
            async def connect(self) -> None:
                await super().connect()
                callback = self.options.kwargs.get("stderr")
                if callback is not None:
                    callback(stderr_line)

        module.ClaudeSDKClient = _StderrClient  # type: ignore[attr-defined]
    return sdk


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
# Live pins (real engine + pool + fake SDK client).
# --------------------------------------------------------------------------- #
async def test_stream_client_constructed_once_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """(a) N calls construct + connect the pooled client exactly once."""
    sdk = fake.install(monkeypatch)
    for i in range(4):
        await _drain("HEADER-STABLE-PREFIX" + "\nstep" * i)

    assert sdk.constructed == 1  # SABOTAGE: build a fresh client per call -> 4 -> red
    assert sdk.connected == 1  # connect reused, not paid per call


async def test_outside_a_loop_each_call_is_a_full_send_under_a_fresh_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) With no agent loop there is no kept conversation: each call is its own SDK
    conversation -- the FULL transcript under a FRESH session_id."""
    sdk = fake.install(monkeypatch)
    await _drain("SHARED-HEADER-PREFIX\nalice-step0")
    await _drain("SHARED-HEADER-PREFIX\nalice-step0\nalice-step1")
    await _drain("SHARED-HEADER-PREFIX\nbob-step0")

    queries = sdk.queries()
    assert [p for p, _ in queries] == [
        "[user]\nSHARED-HEADER-PREFIX\nalice-step0",
        "[user]\nSHARED-HEADER-PREFIX\nalice-step0\nalice-step1",
        "[user]\nSHARED-HEADER-PREFIX\nbob-step0",
    ]
    # SABOTAGE: reuse one session_id across calls outside a loop -> red.
    assert len({sid for _, sid in queries}) == 3
    assert sdk.constructed == 1  # and still ONE pooled client served all three


async def test_stream_two_gact_sessions_get_distinct_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) S2 B1: the pool key is the ACTIVE GACT session."""
    from clio_agent.gact import context as gact_context

    sdk = fake.install(monkeypatch)
    for session in ("sess-alice", "sess-bob"):
        token = gact_context.set_session_id(session)
        try:
            await _drain("HEADER-STABLE-PREFIX\nstep0")
        finally:
            gact_context.reset(token)

    # SABOTAGE: key by something other than the active GACT session -> 1 -> red.
    assert sdk.constructed == 2


# --------------------------------------------------------------------------- #
# BLOCKER pin: the pooled client must survive separate ``asyncio.run()`` loops (the
# loop's synchronous calls each run their own). A loop-strict fake (the real SDK's
# behaviour: transports bind to the connecting loop) makes call 2 go red if the
# client is cached on the caller's loop.
# --------------------------------------------------------------------------- #
def test_pooled_client_survives_separate_asyncio_run_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = fake.install(monkeypatch)
    import sys  # noqa: PLC0415

    module = sys.modules["claude_agent_sdk"]
    base = module.ClaudeSDKClient  # type: ignore[attr-defined]

    class _LoopStrictClient(base):  # type: ignore[misc, valid-type]
        async def connect(self) -> None:
            self._loop = asyncio.get_running_loop()
            await super().connect()

        async def query(self, prompt: Any, session_id: str = "default") -> None:
            running = asyncio.get_running_loop()
            if self._loop is not running or self._loop.is_closed():
                raise RuntimeError("Event loop is closed")
            await super().query(prompt, session_id)

    module.ClaudeSDKClient = _LoopStrictClient  # type: ignore[attr-defined]

    asyncio.run(_drain("HEADER-STABLE-PREFIX\nstep0"))
    # SABOTAGE: cache the client on the CALLER loop -> call 2 runs against a closed
    # loop -> RuntimeError('Event loop is closed') -> red.
    asyncio.run(_drain("HEADER-STABLE-PREFIX\nstep0\nstep1"))

    assert sdk.constructed == 1  # one connect, reused across both run() loops


# --------------------------------------------------------------------------- #
# BLOCKER pin: a mid-cycle abnormal end must DROP the pooled client so its leftover
# response can never bleed into the next (possibly different-expert) call.
# --------------------------------------------------------------------------- #
async def test_pooled_client_reset_on_abnormal_end(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = fake.install(monkeypatch, script=[[fake.text("X"), RuntimeError("transport boom")]])

    with pytest.raises(RuntimeError, match="transport boom"):
        await _drain("HEADER-STABLE-PREFIX\nstep0")
    # SABOTAGE: skip _areset_client on abnormal end -> call 2 reuses client 0 -> red.
    await _drain("HEADER-STABLE-PREFIX\nstep0\nstep1")
    assert sdk.constructed == 2  # poisoned client dropped, a fresh one connected


async def test_midstream_sdk_death_becomes_a_typed_retryable_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(f, #891 live-crash) A pooled CLI subprocess death mid-stream (an SDK
    ``ClaudeSDKError``) is translated to a typed, audited ``dspy.lm15.ServerError``."""
    _install_dying_sdk(monkeypatch)
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(claude_code_sessions, "stream_audit_enabled", lambda: True)
    monkeypatch.setattr(
        claude_code_sessions,
        "stream_audit",
        lambda event, **fields: rows.append({"event": event, **fields}),
    )

    with pytest.raises(ServerError) as excinfo:
        await _drain("HEADER-STABLE-PREFIX\nstep0")
    # SABOTAGE: drop the engine's `except transient_transport_error_types()` arm -> the
    # raw SDK error propagates (not a ServerError) and this pin goes red.
    assert claude_code_sessions.TRANSIENT_TRANSPORT_MARKER in str(excinfo.value)
    error_rows = [r for r in rows if r["event"] == "provider.transport_error"]
    assert error_rows and error_rows[-1]["reason"] == "send_failed"
    assert error_rows[-1]["category"] == "session_transport_error"


def test_dspy_retries_a_dead_transport_on_a_fresh_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retryable type is load-bearing: DSPy re-issues the call, which reconnects."""
    from dspy.lm15 import Request

    sdk = _install_dying_sdk(monkeypatch)
    lm = dspy.LM(
        "claude_code/haiku",
        engine=ClaudeCodeEngine("haiku", cwd="/w", timeout=5.0),
        async_engine=AsyncClaudeCodeEngine("haiku", cwd="/w", timeout=5.0),
        cache=False,
        num_retries=1,
    )
    response = lm(Request(model=lm.model, messages=(Message.user("hi"),)))

    assert response.message.parts[0].text == "Answer"
    assert sdk.constructed == 2  # the dead client was dropped, the retry reconnected


async def test_midstream_death_attaches_the_stderr_tail_to_the_crash_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(B17) The dead client's stderr ring rides the raised error, so the CLI's own
    diagnostic output reaches the user/trace.

    SABOTAGE: stop passing ``stderr_tail=entry.stderr_ring.tail()`` at the engine's
    ``transient_transport_error_message`` call -> this goes red.
    """
    _install_dying_sdk(
        monkeypatch, stderr_line="fatal: authentication expired, please re-run `claude login`\n"
    )

    with pytest.raises(ServerError) as excinfo:
        await _drain("HEADER-STABLE-PREFIX\nstep0")
    assert "authentication expired" in str(excinfo.value)
