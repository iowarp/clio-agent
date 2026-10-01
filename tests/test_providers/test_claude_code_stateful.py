"""Kept Claude Code conversations (#901, the TTFT closer) on the engine.

Pins the conversation registry (:class:`clio_agent.lm.engines.conversations.ConversationRegistry`,
the Claude Code engine's instance) and the stateful scope plumbing
(:mod:`clio_agent.providers.stateful_common`) on the REAL objects, then the live seam:
the engine inside an agent loop sends call 1 in full and call 2 as only the new
messages under the SAME SDK session id, through the real pooled transport.

A call continues its conversation only when its messages repeat everything the SDK
session was sent plus the SDK's own reply, then add only non-assistant messages;
anything else is a typed full resend (``first_call`` / ``prefix_mismatch`` /
``ops_reset`` / ``session_evicted`` / ``provider_error``).

Each load-bearing pin carries an inline SABOTAGE note.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from dspy.lm15 import Message, Request, TextPart, ToolCallPart, ToolResultPart

from clio_agent.gact import context as gact_context
from clio_agent.lm.engines.conversations import ConversationRegistry
from clio_agent.providers import claude_code_engine
from clio_agent.providers import stateful_common as st
from clio_agent.providers.claude_code_sessions import _reset_sessions_for_tests
from tests import _fake_claude_sdk as fake


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    """Every test starts and ends with an empty conversation registry + client pool."""
    claude_code_engine._CONVERSATIONS.clear_for_tests()
    _reset_sessions_for_tests()
    yield
    claude_code_engine._CONVERSATIONS.clear_for_tests()
    _reset_sessions_for_tests()


HEAD = Message.user("what?")


def _step(i: int) -> tuple[Message, Message]:
    call = ToolCallPart(id=f"c{i}", name="search", input={"q": str(i)})
    return (
        Message.assistant([TextPart(text=f"step {i}"), call]),
        Message(
            role="tool",
            parts=(ToolResultPart(id=f"c{i}", name="search", content=(TextPart(text=f"r{i}"),)),),
        ),
    )


def _request(*messages: Message) -> Request:
    return Request(model="claude_code/haiku", messages=messages)


def _key(scope: str) -> tuple[str, ...]:
    return ("sess", scope, "haiku", "/w", "")


def _open(reg: ConversationRegistry, scope: str, handle: str, *messages: Message) -> None:
    reg.opened(_key(scope), handle, _request(*messages), "sys")


# --------------------------------------------------------------------------- #
# 1. Reason-catalog discipline (#775 no-silent-fallback).
# --------------------------------------------------------------------------- #
def test_reset_payload_is_typed_and_rejects_unknown_reasons() -> None:
    payload = st.stateful_reset_payload("ops_reset", "compacted")
    assert payload["reason"] == "ops_reset"
    assert payload["category"] == "stateful_reset"
    assert payload["message"] == "compacted"
    with pytest.raises(ValueError, match="Unknown stateful reset reason"):
        st.stateful_reset_payload("not_a_reason")


def test_reset_catalog_covers_the_declared_reasons() -> None:
    assert set(st.STATEFUL_RESET_REASONS) == {
        "first_call",
        "prefix_mismatch",
        "ops_reset",
        "session_evicted",
        "provider_error",
        "provider_compacted",
    }


# --------------------------------------------------------------------------- #
# 2. The bounded conversation registry.
# --------------------------------------------------------------------------- #
def test_registry_first_call_then_delta_reuses_one_conversation() -> None:
    reg = ConversationRegistry(lambda: 8)
    first = reg.plan(_key("s"), _request(HEAD), "sys")
    assert (first.handle, first.reason, first.messages) == (None, "first_call", (HEAD,))
    _open(reg, "s", "sid-1", HEAD)

    second = reg.plan(_key("s"), _request(HEAD, *_step(0)), "sys")
    assert second.handle == "sid-1"  # the SAME conversation across the delta run
    assert second.messages == (_step(0)[1],)  # only what the SDK has not seen
    assert second.reason is None


def test_registry_edited_history_restarts_typed() -> None:
    reg = ConversationRegistry(lambda: 8)
    _open(reg, "s", "sid-1", HEAD)
    send = reg.plan(_key("s"), _request(Message.user("different"), *_step(0)), "sys")
    # SABOTAGE: compare only lengths in _new_messages -> this becomes a delta -> red.
    assert (send.handle, send.reason) == (None, "prefix_mismatch")
    assert reg.take_released() == ["sid-1"]


def test_registry_changed_system_prompt_is_not_a_delta() -> None:
    reg = ConversationRegistry(lambda: 8)
    _open(reg, "s", "sid-1", HEAD)
    send = reg.plan(_key("s"), _request(HEAD, *_step(0)), "another system")
    assert (send.handle, send.reason) == (None, "prefix_mismatch")


def test_registry_equal_or_assistant_only_extensions_are_not_deltas() -> None:
    reg = ConversationRegistry(lambda: 8)
    _open(reg, "s", "sid-1", HEAD)
    # The same messages again: nothing new to send.
    assert reg.plan(_key("s"), _request(HEAD), "sys").reason == "prefix_mismatch"
    _open(reg, "s", "sid-2", HEAD)
    # Two assistant messages in a row: the second is not the SDK's own reply.
    assistant, _tool = _step(0)
    send = reg.plan(_key("s"), _request(HEAD, assistant, assistant), "sys")
    assert send.reason == "prefix_mismatch"


def test_registry_reset_forces_full_even_on_a_valid_extension() -> None:
    reg = ConversationRegistry(lambda: 8)
    _open(reg, "s", "sid-1", HEAD)
    reg.reset(_key("s"), "provider_error")
    send = reg.plan(_key("s"), _request(HEAD, *_step(0)), "sys")
    # SABOTAGE: drop the pending-reset pop in plan() -> a delta -> red.
    assert (send.handle, send.reason) == (None, "provider_error")


def test_registry_lru_eviction_releases_the_oldest_handle() -> None:
    reg = ConversationRegistry(lambda: 1)
    _open(reg, "A", "sid-A", HEAD)
    _open(reg, "B", "sid-B", HEAD)  # capacity 1: A is evicted
    assert reg.take_released() == ["sid-A"]
    assert reg.plan(_key("A"), _request(HEAD, *_step(0)), "sys").handle is None


def test_registry_parallel_scopes_never_share_a_conversation() -> None:
    reg = ConversationRegistry(lambda: 8)
    _open(reg, "A", "sid-A", HEAD)
    _open(reg, "B", "sid-B", HEAD)
    assert reg.plan(_key("A"), _request(HEAD, *_step(0)), "sys").handle == "sid-A"
    assert reg.plan(_key("B"), _request(HEAD, *_step(0)), "sys").handle == "sid-B"


def test_registry_reset_session_drops_every_conversation_of_that_session() -> None:
    reg = ConversationRegistry(lambda: 8)
    _open(reg, "A", "sid-A", HEAD)
    other = ("other-sess", "A", "haiku", "/w", "")
    reg.opened(other, "sid-O", _request(HEAD), "sys")
    reg.reset_session("sess", "session_evicted")

    assert reg.plan(_key("A"), _request(HEAD, *_step(0)), "sys").reason == "session_evicted"
    assert reg.plan(other, _request(HEAD, *_step(0)), "sys").handle == "sid-O"


# --------------------------------------------------------------------------- #
# 3. The scope contextmanager and the ARC-op hook.
# --------------------------------------------------------------------------- #
def test_stateful_scope_sets_and_restores() -> None:
    assert st.active_stateful_scope() is None
    with st.stateful_scope("tok") as token:
        assert token == "tok"
        assert st.active_stateful_scope() == "tok"
    assert st.active_stateful_scope() is None


def test_stateful_scopes_nest_and_restore() -> None:
    with st.stateful_scope("outer"):
        assert st.active_stateful_scope() == "outer"
        with st.stateful_scope("inner"):
            assert st.active_stateful_scope() == "inner"
        assert st.active_stateful_scope() == "outer"
    assert st.active_stateful_scope() is None


def test_an_arc_op_resets_every_conversation_the_forward_drove() -> None:
    """``note_prefix_reset_for_active_scope`` (what compaction calls) reaches the engine.

    SABOTAGE: drop ``register_scope_registry`` from ``ConversationRegistry`` -> red.
    """
    reg = claude_code_engine._CONVERSATIONS
    assert st.note_prefix_reset_for_active_scope() is False  # a no-op off-scope
    with st.stateful_scope("fwd"):
        reg.plan(_key("s"), _request(HEAD), "sys")  # the forward drives this key
        _open(reg, "s", "sid-1", HEAD)
        assert st.note_prefix_reset_for_active_scope("ops_reset") is True
        send = reg.plan(_key("s"), _request(HEAD, *_step(0)), "sys")
    assert (send.handle, send.reason) == (None, "ops_reset")


# --------------------------------------------------------------------------- #
# 4. The live seam: the engine inside an agent loop, over the real pool.
# --------------------------------------------------------------------------- #
@pytest.fixture
def loop_scope() -> Any:
    session_token = gact_context.set_session_id("sess-a")
    scope_token = gact_context.set_react_scope("main", "react")
    try:
        with st.stateful_scope():
            yield
    finally:
        gact_context.reset(scope_token)
        gact_context.reset(session_token)


async def test_the_engine_sends_a_delta_over_a_stable_session_in_a_loop(
    monkeypatch: pytest.MonkeyPatch, loop_scope: None
) -> None:
    sdk = fake.install(monkeypatch)
    await fake.drive(_request(HEAD))
    await fake.drive(_request(HEAD, *_step(0)))

    (first, sid1), (second, sid2) = sdk.queries()
    assert first == "[user]\nwhat?"
    # Call 2: ONLY the new tool results, under the SAME SDK session.
    assert second == "[tool results]\n[c0 search]\nr0"
    assert sid2 == sid1
    assert sdk.constructed == 1


async def test_outside_a_loop_every_call_is_full_under_a_fresh_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = fake.install(monkeypatch)
    await fake.drive(_request(HEAD))
    await fake.drive(_request(HEAD, *_step(0)))

    (_first, sid1), (second, sid2) = sdk.queries()
    assert second.startswith("[user]\nwhat?")  # the FULL transcript, not the delta
    assert sid1 != sid2


async def test_a_failed_query_drops_the_conversation_typed(
    monkeypatch: pytest.MonkeyPatch, loop_scope: None
) -> None:
    fake.install(monkeypatch, script=[fake.answer(), [RuntimeError("boom")]])
    await fake.drive(_request(HEAD))
    with pytest.raises(RuntimeError, match="boom"):
        await fake.drive(_request(HEAD, *_step(0)))

    key = claude_code_engine.conversation_key("haiku", "/w", "")
    send = claude_code_engine._CONVERSATIONS.plan(key, _request(HEAD, *_step(0)), "")
    assert (send.handle, send.reason) == (None, "provider_error")


async def test_a_dropped_client_evicts_that_sessions_conversations(
    monkeypatch: pytest.MonkeyPatch, loop_scope: None
) -> None:
    """A client drop (reap, release, dead transport) announces itself; the engine
    resets that GACT session's conversations so no delta reaches a fresh subprocess."""
    from clio_agent.providers.claude_code_sessions import _STREAM_CLIENT_POOL

    sdk = fake.install(monkeypatch)
    await fake.drive(_request(HEAD))
    _STREAM_CLIENT_POOL.release_session_resources("sess-a")
    await fake.drive(_request(HEAD, *_step(0)))

    (_first, sid1), (second, sid2) = sdk.queries()
    assert second.startswith("[user]\nwhat?")  # a full resend on the fresh client
    assert sid2 != sid1


# --------------------------------------------------------------------------- #
# live: the real mid-loop delta send against the real SDK/API (task #58 / #1211 A4 --
# "the layer-3 SDK 400"). CLIO_RUN_LIVE=1 only; two billed calls.
# --------------------------------------------------------------------------- #
@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("CLIO_RUN_LIVE") != "1",
    reason="live claude_code SDK stateful-delta probe: set CLIO_RUN_LIVE=1 "
    "(needs `claude` on PATH + `claude login`; 2 billed API calls)",
)
async def test_live_mid_loop_delta_send_does_not_400(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, loop_scope: None
) -> None:
    """iowarp/clio-agent#1211 A4: a real mid-loop delta send (call 2 continues call
    1's SDK session with only the appended messages) does NOT 400. The fix that makes
    it work is ``build_sdk_options``'s ``max_turns=0`` (a stale ``max_turns=1`` rejects
    exactly this second-call shape with ``error_max_turns``).

    Pins the DELTA SHAPE, not just "no exception": the ``provider.stateful`` rows show
    call 1 full/first_call and call 2 a delta under the SAME session id.
    """
    import json

    audit_log = tmp_path / "stream_audit.jsonl"
    monkeypatch.setenv("CLIO_STREAM_AUDIT_LOG", str(audit_log))
    turn1 = Message.user("Probe turn 1: what is 2+2? Reply with just the digit.")
    turn2 = (turn1, Message.assistant("4"), Message.user("Probe turn 2: 3+3? Just the digit."))

    engine = claude_code_engine.AsyncClaudeCodeEngine("haiku")
    first = await engine.complete(_request(turn1))
    # Continue with the SDK's own reply as the assistant message it holds.
    second = await engine.complete(_request(turn1, first.message, turn2[2]))

    assert first.message.parts and second.message.parts
    rows = [
        json.loads(line)
        for line in audit_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    stateful = [r for r in rows if r.get("stage") == "provider.stateful"]
    assert [(r["stateful_mode"], r.get("reason")) for r in stateful] == [
        ("full", "first_call"),
        ("delta", None),
    ]
    assert stateful[1]["session_id"] == stateful[0]["session_id"]
