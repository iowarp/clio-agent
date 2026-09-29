"""Claude Code as a DSPy 3.4 engine (:mod:`clio_agent.providers.claude_code_engine`).

Pins, against a fake pooled SDK client (no CLI, no network):

* the request crosses as text plus native image/document blocks; the system prompt and
  tool rules ride the SDK's ``system_prompt``; the reply's tool block comes back typed;
* one SDK session per conversation inside an agent loop: append-only calls send only
  the new messages under the same session id; an edit or a failure resets typed;
* streaming: thinking, visible text (the block held back), usage with cache reads and
  writes; ``max_tokens`` is a ``length`` finish;
* a refused sign-in / an exhausted plan raise clio's typed errors; a timeout and a
  dead transport raise ``dspy.lm15`` types; an empty reply is a server error.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from dspy.lm15 import (
    DocumentPart,
    FunctionTool,
    ImagePart,
    Message,
    Request,
    ServerError,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from dspy.lm15 import TimeoutError as LMTimeoutError

from clio_agent.gact import context as gact_context
from clio_agent.lm.engines.text_tools import FENCE
from clio_agent.providers import claude_code_engine, stateful_common
from clio_agent.providers.claude_code_engine import AsyncClaudeCodeEngine
from clio_agent.providers.claude_code_errors import ClaudeCodeSignedOutError
from clio_agent.providers.claude_code_plan_limit import ClaudeCodePlanLimitError

MODEL = "claude-sonnet-4-6"
SEARCH = FunctionTool(
    name="search",
    description="Search.",
    parameters={"type": "object", "properties": {"q": {"type": "string"}}},
)


# The engine dispatches on the SDK message class names (duck-typed, no SDK import).
class StreamEvent:
    def __init__(self, event: dict[str, Any]) -> None:
        self.event = event


class TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class AssistantMessage:
    def __init__(self, text: str, error: str | None = None) -> None:
        self.content = [TextBlock(text)] if text else []
        self.error = error
        self.usage = None
        self.stop_reason = None


class ResultMessage:
    def __init__(self, **kw: Any) -> None:
        self.usage = kw.get("usage")
        self.stop_reason = kw.get("stop_reason")
        self.result = kw.get("result")
        self.is_error = kw.get("is_error", False)
        self.api_error_status = kw.get("api_error_status")
        self.subtype = kw.get("subtype")
        self.model_usage = None
        self.total_cost_usd = None


class RateLimitEvent:
    def __init__(self, status: str) -> None:
        self.rate_limit_info = SimpleNamespace(
            status=status, rate_limit_type="five_hour", resets_at=None
        )


def _text(text: str) -> StreamEvent:
    return StreamEvent(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}
    )


def _thinking(text: str) -> StreamEvent:
    return StreamEvent(
        {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": text}}
    )


def _result(**kw: Any) -> ResultMessage:
    kw.setdefault(
        "usage",
        {
            "input_tokens": 20,
            "output_tokens": 4,
            "cache_read_input_tokens": 300,
            "cache_creation_input_tokens": 12,
        },
    )
    return ResultMessage(**kw)


class FakeEntry:
    def __init__(self, pool: FakePool) -> None:
        self.pool = pool
        self.stderr_ring = SimpleNamespace(tail=lambda: "")

    async def stream(self, **kwargs: Any) -> Any:
        self.pool.sends.append(kwargs)
        turn = self.pool.turns.pop(0)
        if isinstance(turn, BaseException):
            raise turn
        for message in turn:
            yield message


class FakePool:
    def __init__(self) -> None:
        self.turns: list[list[Any] | BaseException] = []
        self.sends: list[dict[str, Any]] = []

    def entry_for(self, **_kw: Any) -> FakeEntry:
        return FakeEntry(self)

    def bump_construct(self) -> None:
        return None


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakePool]:
    fake = FakePool()
    monkeypatch.setattr(claude_code_engine, "_STREAM_CLIENT_POOL", fake)
    # No SDK import/install in a unit test: the pool is faked.
    monkeypatch.setattr(claude_code_engine, "require_claude_agent_sdk", lambda: None)
    claude_code_engine._CONVERSATIONS.clear_for_tests()
    claude_code_engine._SESSION_COST.clear()
    yield fake
    claude_code_engine._CONVERSATIONS.clear_for_tests()
    claude_code_engine._SESSION_COST.clear()


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from clio_agent.runtime import stream_audit

    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(stream_audit, "stream_audit_enabled", lambda: True)
    monkeypatch.setattr(
        stream_audit, "stream_audit", lambda stage, **row: rows.append({"stage": stage, **row})
    )
    return rows


@pytest.fixture
def loop_scope() -> Iterator[None]:
    session_token = gact_context.set_session_id("sess-a")
    scope_token = gact_context.set_react_scope("main", "react")
    try:
        with stateful_common.stateful_scope():
            yield
    finally:
        gact_context.reset(scope_token)
        gact_context.reset(session_token)


HEAD = Message.user("what?")


def _request(*messages: Message, system: str = "You are clio.") -> Request:
    return Request(model=f"claude_code/{MODEL}", system=system, messages=messages, tools=(SEARCH,))


def _step(i: int) -> tuple[Message, Message]:
    call = ToolCallPart(id=f"c{i}", name="search", input={"q": str(i)})
    return (
        Message.assistant([TextPart(text=f"step {i}"), call]),
        Message(
            role="tool",
            parts=(ToolResultPart(id=f"c{i}", name="search", content=(TextPart(text=f"r{i}"),)),),
        ),
    )


def _run(request: Request, engine: AsyncClaudeCodeEngine | None = None) -> Any:
    return asyncio.run((engine or AsyncClaudeCodeEngine(MODEL)).complete(request))


def _stateful(rows: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
    return [
        (r["stateful_mode"], r.get("reason")) for r in rows if r["stage"] == "provider.stateful"
    ]


def test_the_request_crosses_as_text_with_the_system_on_the_sdk_option(pool: FakePool) -> None:
    block = f'\n{FENCE}\n[{{"name": "search", "arguments": {{"q": "x"}}}}]\n```'
    pool.turns = [[_thinking("plan"), _text("Looking." + block), _result()]]
    response = _run(_request(HEAD))

    [send] = pool.sends
    assert send["system_prompt"].startswith("You are clio.\n\n# Calling tools")
    assert send["payload"] == "[user]\nwhat?"
    assert send["native_blocks"] == []
    parts = response.message.parts
    assert parts[0].text == "plan"
    assert parts[1] == TextPart(text="Looking.")
    assert (parts[2].name, parts[2].input) == ("search", {"q": "x"})
    usage = response.usage
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (20, 300, 12)


def test_images_and_documents_ride_as_native_blocks(pool: FakePool) -> None:
    pool.turns = [[_text("seen"), _result()]]
    image = ImagePart(data="aGk=", media_type="image/png")
    pdf = DocumentPart(data="JVBE", media_type="application/pdf")
    _run(_request(Message(role="user", parts=(TextPart(text="look"), image, pdf))))
    assert pool.sends[0]["native_blocks"] == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGk="}},
        {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBE"},
        },
    ]


def test_append_only_calls_continue_the_session_with_new_messages_only(
    pool: FakePool, audit: list[dict[str, Any]], loop_scope: None
) -> None:
    pool.turns = [[_text("a"), _result()], [_text("b"), _result()]]
    _run(_request(HEAD))
    _run(_request(HEAD, *_step(0)))

    first, second = pool.sends
    assert second["session_id"] == first["session_id"]
    assert second["payload"] == "[tool results]\n[c0 search]\nr0"
    assert _stateful(audit) == [("full", "first_call"), ("delta", None)]


def test_an_edited_history_opens_a_new_session(
    pool: FakePool, audit: list[dict[str, Any]], loop_scope: None
) -> None:
    pool.turns = [[_text("a"), _result()], [_text("b"), _result()]]
    _run(_request(HEAD))
    _run(_request(Message.user("another head"), *_step(0)))
    first, second = pool.sends
    assert second["session_id"] != first["session_id"]
    assert _stateful(audit)[-1] == ("full", "prefix_mismatch")


def test_a_failed_query_resets_the_session(
    pool: FakePool, audit: list[dict[str, Any]], loop_scope: None
) -> None:
    pool.turns = [[_text("a"), _result()], TimeoutError(), [_text("b"), _result()]]
    _run(_request(HEAD))
    with pytest.raises(LMTimeoutError):
        _run(_request(HEAD, *_step(0)))
    _run(_request(HEAD, *_step(0)))
    assert _stateful(audit)[-1] == ("full", "provider_error")


def test_max_tokens_is_a_length_finish(pool: FakePool) -> None:
    pool.turns = [[_text("The answer is"), _result(stop_reason="max_tokens")]]
    assert _run(_request(HEAD)).finish_reason == "length"


def test_a_refused_sign_in_is_the_typed_signed_out_error(pool: FakePool) -> None:
    pool.turns = [
        [
            AssistantMessage("", error="authentication_failed"),
            _result(is_error=True, api_error_status=401, result="Invalid API key"),
        ]
    ]
    with pytest.raises(ClaudeCodeSignedOutError):
        _run(_request(HEAD))


def test_an_exhausted_plan_is_the_typed_plan_limit_error(pool: FakePool) -> None:
    pool.turns = [[RateLimitEvent("rejected")]]
    with pytest.raises(ClaudeCodePlanLimitError):
        _run(_request(HEAD))


def test_a_blank_reply_is_a_server_error(pool: FakePool) -> None:
    pool.turns = [[_result()]]
    with pytest.raises(ServerError, match="empty content"):
        _run(_request(HEAD))


def test_a_dropped_client_evicts_that_sessions_conversations(
    pool: FakePool, audit: list[dict[str, Any]], loop_scope: None
) -> None:
    """The pool announces a dropped client; the engine resets that GACT session's
    conversations so the next call is a typed full resend on the fresh client."""
    from clio_agent.providers import claude_code_sessions

    pool.turns = [[_text("a"), _result()], [_text("b"), _result()]]
    _run(_request(HEAD))
    claude_code_sessions._notify_client_dropped("sess-a")
    _run(_request(HEAD, *_step(0)))

    first, second = pool.sends
    assert second["session_id"] != first["session_id"]
    assert second["payload"].startswith("[user]\nwhat?")
    # SABOTAGE: drop the engine's on_client_dropped registration -> a delta -> red.
    assert _stateful(audit)[-1] == ("full", "session_evicted")


def test_a_dropped_client_of_another_session_leaves_this_one_kept(
    pool: FakePool, audit: list[dict[str, Any]], loop_scope: None
) -> None:
    from clio_agent.providers import claude_code_sessions

    pool.turns = [[_text("a"), _result()], [_text("b"), _result()]]
    _run(_request(HEAD))
    claude_code_sessions._notify_client_dropped("some-other-session")
    _run(_request(HEAD, *_step(0)))
    assert _stateful(audit)[-1] == ("delta", None)


def test_the_sdk_cost_reaches_the_turn_usage_as_known(pool: FakePool) -> None:
    import dspy

    from clio_agent.gact.usage import _usage_from_history_slice
    from clio_agent.providers.claude_code_engine import ClaudeCodeEngine

    priced = _result()
    priced.total_cost_usd = 0.0125
    pool.turns = [[_text("hi"), priced]]
    lm = dspy.LM(
        f"claude_code/{MODEL}",
        engine=ClaudeCodeEngine(MODEL),
        async_engine=AsyncClaudeCodeEngine(MODEL),
        cache=False,
        num_retries=0,
    )
    lm(_request(HEAD))
    with dspy.context(lm=lm):
        usage = _usage_from_history_slice(0)
    assert (usage["cost_usd"], usage["cost_known"]) == (0.0125, True)


def test_a_kept_sessions_cumulative_cost_is_recorded_per_call(
    pool: FakePool, loop_scope: None
) -> None:
    first, second = _result(), _result()
    first.total_cost_usd, second.total_cost_usd = 0.004, 0.0095  # SDK: session-cumulative
    pool.turns = [[_text("a"), first], [_text("b"), second]]
    one = _run(_request(HEAD))
    two = _run(_request(HEAD, *_step(0)))
    assert pool.sends[1]["session_id"] == pool.sends[0]["session_id"]
    assert one.provider_data == {"cost_usd": 0.004}
    assert two.provider_data == {"cost_usd": pytest.approx(0.0055)}


def test_one_shot_sessions_leave_no_cost_memory(pool: FakePool) -> None:
    priced = _result()
    priced.total_cost_usd = 0.002
    pool.turns = [[_text("a"), priced]]
    assert _run(_request(HEAD)).provider_data == {"cost_usd": 0.002}
    assert claude_code_engine._SESSION_COST == {}


def test_redacted_thinking_is_recorded_typed_not_dropped(
    pool: FakePool, audit: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.providers import claude_code_stream_events

    durable: list[int] = []
    monkeypatch.setattr(
        claude_code_stream_events,
        "stream_audit",
        lambda stage, **row: audit.append({"stage": stage, **row}),
    )
    monkeypatch.setattr(
        claude_code_stream_events,
        "_emit_redacted_thinking_trace_event",
        lambda *, call_index, tokens: durable.append(tokens),
    )
    redacted = StreamEvent(
        {
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": "", "estimated_tokens": 40},
        }
    )
    pool.turns = [[redacted, redacted, _text("ok"), _result()]]
    response = _run(_request(HEAD))

    assert [p.type for p in response.message.parts] == ["text"]  # nothing authored as thinking
    rows = [r for r in audit if r.get("duplicate_reason") == "provider_thinking_redacted"]
    assert [r["thinking_tokens_estimated"] for r in rows] == [40, 40]
    assert durable == [40]  # one durable trace event per call
