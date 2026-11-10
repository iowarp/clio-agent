"""Kept Claude Code conversations (#901, the TTFT closer) on the engine.

Pins the conversation a pooled client holds (:mod:`clio_agent.providers.claude_code_conversation`) and the stateful scope plumbing
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
from types import SimpleNamespace
from typing import Any

import pytest
from dspy.lm15 import Message, Request, TextPart, ThinkingPart, ToolCallPart, ToolResultPart

from clio_agent.gact import context as gact_context
from clio_agent.providers import claude_code_conversation, claude_code_engine
from clio_agent.providers import stateful_common as st
from clio_agent.providers.claude_code_sessions import _reset_sessions_for_tests
from tests import _fake_claude_sdk as fake


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    """Every test starts and ends with an empty conversation registry + client pool."""
    claude_code_conversation.forward_resets().clear_for_tests()
    _reset_sessions_for_tests()
    yield
    claude_code_conversation.forward_resets().clear_for_tests()
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
        "conversation_cleared",
    }


# --------------------------------------------------------------------------- #
# 2. The conversation a pooled client holds (claude_code_conversation).
# --------------------------------------------------------------------------- #
IDENTITY = ("haiku", "/w", "")


def _entry() -> Any:
    """What the planner reads and writes on a pooled entry."""
    return SimpleNamespace(conversation=None, pending_reset=None)


def _plan(entry: Any, *messages: Message, system: str = "sys") -> Any:
    return claude_code_conversation.plan(entry, _request(*messages), system, IDENTITY, kept=True)


def _open(entry: Any, handle: str, *messages: Message) -> None:
    send = _plan(entry, *messages)
    claude_code_conversation.record_sent(entry, send, handle, _request(*messages), "sys")


def test_first_call_then_delta_continues_the_entrys_conversation() -> None:
    entry = _entry()
    first = _plan(entry, HEAD)
    assert (first.handle, first.reason, first.messages) == (None, "first_call", (HEAD,))
    claude_code_conversation.record_sent(entry, first, "sid-1", _request(HEAD), "sys")

    second = _plan(entry, HEAD, *_step(0))
    assert second.handle == "sid-1"  # the SAME conversation across the delta run
    assert second.messages == (_step(0)[1],)  # only what the CLI has not seen
    assert second.reason is None


def test_an_edited_history_restarts_typed() -> None:
    entry = _entry()
    _open(entry, "sid-1", HEAD)
    send = _plan(entry, Message.user("different"), *_step(0))
    # SABOTAGE: compare only lengths in new_messages -> this becomes a delta -> red.
    assert (send.handle, send.reason) == (None, "prefix_mismatch")
    assert entry.conversation is None


def test_a_changed_system_prompt_or_identity_is_not_a_delta() -> None:
    entry = _entry()
    _open(entry, "sid-1", HEAD)
    send = _plan(entry, HEAD, *_step(0), system="another system")
    assert (send.handle, send.reason) == (None, "prefix_mismatch")
    _open(entry, "sid-2", HEAD)
    other = claude_code_conversation.plan(
        entry, _request(HEAD, *_step(0)), "sys", ("sonnet", "/w", ""), kept=True
    )
    assert (other.handle, other.reason) == (None, "prefix_mismatch")


def test_equal_or_assistant_only_extensions_are_not_deltas() -> None:
    entry = _entry()
    _open(entry, "sid-1", HEAD)
    # The same messages again: nothing new to send.
    assert _plan(entry, HEAD).reason == "prefix_mismatch"
    _open(entry, "sid-2", HEAD)
    # Two assistant messages in a row: the second is not the CLI's own reply.
    assistant, _tool = _step(0)
    assert _plan(entry, HEAD, assistant, assistant).reason == "prefix_mismatch"


def test_a_reset_forces_full_even_on_a_valid_extension() -> None:
    entry = _entry()
    _open(entry, "sid-1", HEAD)
    claude_code_conversation.reset(entry, "provider_error")
    send = _plan(entry, HEAD, *_step(0))
    # SABOTAGE: drop the pending-reset read in plan() -> a delta -> red.
    assert (send.handle, send.reason) == (None, "provider_error")


def test_a_one_shot_call_is_full_and_never_kept() -> None:
    entry = _entry()
    send = claude_code_conversation.plan(entry, _request(HEAD), "sys", IDENTITY, kept=False)
    assert (send.kept, send.handle, send.reason) == (False, None, None)
    assert entry.conversation is None


def test_each_entry_holds_its_own_conversation() -> None:
    a, b = _entry(), _entry()
    _open(a, "sid-A", HEAD)
    _open(b, "sid-B", HEAD)
    assert _plan(a, HEAD, *_step(0)).handle == "sid-A"
    assert _plan(b, HEAD, *_step(0)).handle == "sid-B"


def test_replacing_a_conversation_returns_the_old_handle_for_its_cost_tally() -> None:
    entry = _entry()
    _open(entry, "sid-1", HEAD)
    send = _plan(entry, Message.user("new head"))
    replaced = claude_code_conversation.record_sent(
        entry, send, "sid-2", _request(Message.user("new head")), "sys"
    )
    assert replaced is None  # plan() already forgot the old one on the mismatch
    entry.conversation = None
    _open(entry, "sid-3", HEAD)
    send = _plan(entry, HEAD, *_step(0))
    assert (
        claude_code_conversation.record_sent(
            entry, send, "ignored", _request(HEAD, *_step(0)), "sys"
        )
        is None
    )  # a delta keeps its handle


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
    """``note_prefix_reset_for_active_scope`` (what compaction calls) reaches the entry.

    SABOTAGE: drop ``register_scope_registry(_FORWARDS)`` -> red.
    """
    entry = _entry()
    assert st.note_prefix_reset_for_active_scope() is False  # a no-op off-scope
    with st.stateful_scope("fwd"):
        _open(entry, "sid-1", HEAD)  # the forward drives this entry
        assert st.note_prefix_reset_for_active_scope("ops_reset") is True
        send = _plan(entry, HEAD, *_step(0))
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

    from clio_agent.providers.claude_code_sessions import _STREAM_CLIENT_POOL

    # The failed client is dead; its replacement starts over, typed with the failure.
    entry = _STREAM_CLIENT_POOL.entry_for(session_id="sess-a")
    send = claude_code_conversation.plan(
        entry, _request(HEAD, *_step(0)), "", ("haiku", "/w", ""), kept=True
    )
    assert (send.handle, send.reason) == (None, "provider_error")


async def test_a_dropped_client_evicts_that_sessions_conversations(
    monkeypatch: pytest.MonkeyPatch, loop_scope: None
) -> None:
    """A client drop (reap, release, dead transport) takes its conversation with it,
    so no delta reaches a fresh subprocess."""
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
# Claude tokens validate the Claude Code engine on Sonnet only (owner rule).
_LIVE_MODEL = "sonnet"


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

    engine = claude_code_engine.AsyncClaudeCodeEngine(_LIVE_MODEL)
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


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("CLIO_RUN_LIVE") != "1",
    reason="live claude_code SDK thinking probe: set CLIO_RUN_LIVE=1 (2 billed API calls)",
)
async def test_live_thinking_streams_and_survives_the_delta_send(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, loop_scope: None
) -> None:
    """Thinking pass-back on the stateful engine: with thinking on, call 1 streams
    thinking, and call 2 continues the SAME SDK session as a delta -- the session holds
    call 1's signed thinking blocks, so nothing has to be re-sent and nothing 400s."""
    import json

    audit_log = tmp_path / "stream_audit.jsonl"
    monkeypatch.setenv("CLIO_STREAM_AUDIT_LOG", str(audit_log))
    engine = claude_code_engine.AsyncClaudeCodeEngine(
        _LIVE_MODEL, thinking={"type": "adaptive", "display": "summarized", "effort": "high"}
    )
    # Adaptive thinking is the model's choice: an easy question gets none, so ask one
    # that needs reasoning.
    turn1 = Message.user(
        "How many positive integers below 10000 have digits summing to 20 and are "
        "divisible by 7? Reason it out, then give just the number."
    )
    first = await engine.complete(_request(turn1))
    second = await engine.complete(
        _request(turn1, first.message, Message.user("And below 1000? Just the number."))
    )

    assert any(isinstance(p, ThinkingPart) and p.text for p in first.message.parts)
    assert second.message.parts
    rows = [json.loads(x) for x in audit_log.read_text(encoding="utf-8").splitlines() if x.strip()]
    stateful = [r for r in rows if r.get("stage") == "provider.stateful"]
    assert [r["stateful_mode"] for r in stateful] == ["full", "delta"]
    assert stateful[1]["session_id"] == stateful[0]["session_id"]
