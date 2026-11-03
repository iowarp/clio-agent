"""A new conversation on a pooled Claude Code client starts from an empty CLI.

One ``ClaudeSDKClient`` holds one conversation whatever the query's ``session_id``
(probe 2026-11-03). A full send used to be appended to the history the CLI already held,
so after a compaction the model read the old history, the summary and the new context.
The fake SDK (``tests/_fake_claude_sdk.py``) models that: ``held`` is the CLI's one
conversation, which ``/clear`` empties.

SABOTAGE (each turns the named test red):

* ``_StreamClientEntry.stream``: drop the ``clear_conversation`` call ->
  ``test_a_full_send_after_a_reset_starts_from_an_empty_cli``.
* ``AsyncClaudeCodeEngine._query``: use the session's own key for one-shot calls ->
  ``test_a_one_shot_call_never_touches_the_agents_conversation``.
* ``AsyncClaudeCodeEngine._query``: drop the ``reset_session`` before ``opened`` ->
  ``test_opening_a_conversation_resets_the_sessions_others_typed``.
"""

from __future__ import annotations

from typing import Any

import pytest
from dspy.lm15 import Message, Request, TextPart, ToolCallPart, ToolResultPart

from clio_agent.gact import context as gact_context
from clio_agent.providers import claude_code_engine
from clio_agent.providers import stateful_common as st
from clio_agent.providers.claude_code_clear import side_key
from clio_agent.providers.claude_code_sessions import _STREAM_CLIENT_POOL, _reset_sessions_for_tests
from tests import _fake_claude_sdk as fake

HEAD = Message.user("what?")
STEP = (
    Message.assistant([TextPart(text="step 0"), ToolCallPart(id="c0", name="s", input={})]),
    Message(
        role="tool", parts=(ToolResultPart(id="c0", name="s", content=(TextPart(text="r0"),)),)
    ),
)


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    claude_code_engine._CONVERSATIONS.clear_for_tests()
    _reset_sessions_for_tests()
    yield
    claude_code_engine._CONVERSATIONS.clear_for_tests()
    _reset_sessions_for_tests()


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


def _request(*messages: Message) -> Request:
    return Request(model="claude_code/haiku", messages=messages)


async def test_the_first_conversation_on_a_new_client_is_not_cleared(
    monkeypatch: pytest.MonkeyPatch, loop_scope: None
) -> None:
    sdk = fake.install(monkeypatch)
    await fake.drive(_request(HEAD))
    await fake.drive(_request(HEAD, *STEP))  # a delta: no clear either
    [client] = sdk.clients
    assert client.clears == 0 and len(client.held) == 2


async def test_a_full_send_after_a_reset_starts_from_an_empty_cli(
    monkeypatch: pytest.MonkeyPatch, loop_scope: None
) -> None:
    """A compaction (``ops_reset``) folds the history: the next call is a full send of
    the compacted context, and the CLI holds only that, not the history before it."""
    sdk = fake.install(monkeypatch)
    await fake.drive(_request(HEAD))
    await fake.drive(_request(HEAD, *STEP))
    key = claude_code_engine.conversation_key("haiku", "/w", "")
    claude_code_engine._CONVERSATIONS.reset(key, "ops_reset")
    compacted = _request(Message.user("[earlier context] a summary"), HEAD)
    await fake.drive(compacted)

    [client] = sdk.clients  # the same warm process, no reconnect
    assert client.clears == 1
    [payload] = client.held
    assert "a summary" in payload and "step 0" not in payload


async def test_a_one_shot_call_never_touches_the_agents_conversation(
    monkeypatch: pytest.MonkeyPatch, loop_scope: None
) -> None:
    sdk = fake.install(monkeypatch)
    await fake.drive(_request(HEAD))
    with st.outside_stateful_scope():  # a side call during the turn (a judge, a review)
        await fake.drive(_request(Message.user("rate this")))
        await fake.drive(_request(Message.user("rate that")))
    await fake.drive(_request(HEAD, *STEP))

    main, side = sdk.clients
    assert main.clears == 0 and len(main.held) == 2  # the head, then only the delta
    assert main.queries[1][0] == "[tool results]\n[c0 s]\nr0"
    assert side.clears == 1 and len(side.held) == 1  # each one-shot starts empty


async def test_opening_a_conversation_resets_the_sessions_others_typed(
    monkeypatch: pytest.MonkeyPatch, loop_scope: None
) -> None:
    """Two conversations of one session (another model) share its client: opening one
    clears the CLI, so the other must resend in full, typed."""
    fake.install(monkeypatch)
    await fake.drive(_request(HEAD))
    other = claude_code_engine.conversation_key("sonnet", "/w", "")
    await fake.drive(Request(model="claude_code/sonnet", messages=(HEAD,)))

    first = claude_code_engine.conversation_key("haiku", "/w", "")
    plan = claude_code_engine._CONVERSATIONS.plan(first, _request(HEAD, *STEP), "")
    assert (plan.handle, plan.reason) == (None, "conversation_cleared")
    kept = claude_code_engine._CONVERSATIONS.plan(
        other, Request(model="claude_code/sonnet", messages=(HEAD, *STEP)), ""
    )
    assert kept.handle is not None


def test_releasing_a_session_closes_its_side_client() -> None:
    main = _STREAM_CLIENT_POOL.entry_for(session_id="sess-r")
    side = _STREAM_CLIENT_POOL.entry_for(session_id=side_key("sess-r"))
    assert main is not side
    _STREAM_CLIENT_POOL.release("sess-r")
    assert _STREAM_CLIENT_POOL.entry_for(session_id=side_key("sess-r")) is not side
