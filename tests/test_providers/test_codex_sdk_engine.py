"""The Codex SDK as a DSPy 3.4 engine (:mod:`clio_agent.providers.codex.sdk_engine`).

Pins, against a fake SDK client (no runtime, no network):

* the request crosses as text: system + tool rules on a full send, the new messages
  only on a continued thread; the reply's tool block comes back as typed calls;
* one thread per ``(session, scope, model, cwd, effort)`` inside an agent loop:
  append-only calls continue it; an edit, a lost thread, a provider error, a Codex
  compaction or an ARC op resets it typed (``provider.stateful`` reason);
* outside a loop every call is a full, unkept send;
* streaming: thinking, visible text (the block held back), calls, usage with cached
  input; errors surface as ``dspy.lm15`` types;
* ``dspy.LM`` on the engine pair: ``lm(Request)`` works sync and async.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import dspy
import pytest
from dspy.lm15 import (
    AuthError,
    FunctionTool,
    Message,
    Request,
    ServerError,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)

from clio_agent.gact import context as gact_context
from clio_agent.lm.engines.text_tools import FENCE, TURN_REMINDER
from clio_agent.providers import stateful_common
from clio_agent.providers.codex import sdk_engine
from clio_agent.providers.codex.errors import CodexSDKError
from clio_agent.providers.codex.sdk_client import CodexThreadLostError
from clio_agent.providers.codex.sdk_engine import AsyncCodexSDKEngine, CodexSDKEngine

MODEL = "gpt-x"
SEARCH = FunctionTool(
    name="search",
    description="Search.",
    parameters={"type": "object", "properties": {"query": {"type": "string"}}},
)


def _ev(method: str, **payload: Any) -> Any:
    return SimpleNamespace(method=method, payload=SimpleNamespace(**payload))


def _text(text: str) -> Any:
    return _ev("item/agentMessage/delta", delta=text)


def _usage(inp: int, cached: int, out: int) -> Any:
    last = SimpleNamespace(
        input_tokens=inp,
        cached_input_tokens=cached,
        cache_write_input_tokens=0,
        output_tokens=out,
        reasoning_output_tokens=2,
        total_tokens=inp + out,
    )
    return _ev("thread/tokenUsage/updated", token_usage=SimpleNamespace(last=last))


def _block(*calls: tuple[str, dict[str, Any]]) -> str:
    body = json.dumps([{"name": n, "arguments": a} for n, a in calls])
    return f"\n{FENCE}\n{body}\n```"


class FakeClient:
    """Stands in for the process-wide SDK client: scripted turns, recorded sends."""

    def __init__(self) -> None:
        self.turns: list[list[Any] | BaseException] = []
        self.sends: list[dict[str, Any]] = []
        self.archived: list[str] = []
        self.threads = 0

    async def stream(self, **kwargs: Any) -> Any:
        self.sends.append(kwargs)
        turn = self.turns.pop(0)
        if isinstance(turn, BaseException):
            raise turn
        if kwargs["thread_id"] is None and kwargs["keep_thread"]:
            self.threads += 1
            kwargs["on_thread"](f"thread-{self.threads}")
        for event in turn:
            if event == "compacted":
                kwargs["on_compacted"]()
                continue
            yield event

    def archive_threads(self, ids: list[str]) -> None:
        self.archived.extend(ids)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeClient]:
    fake = FakeClient()
    monkeypatch.setattr(sdk_engine, "_SDK_CLIENT", fake)
    sdk_engine._CONVERSATIONS.clear_for_tests()
    yield fake
    sdk_engine._CONVERSATIONS.clear_for_tests()


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
    """An agent loop: GACT session + react scope + per-forward stateful scope."""
    session_token = gact_context.set_session_id("sess-a")
    scope_token = gact_context.set_react_scope("main", "react")
    try:
        with stateful_common.stateful_scope():
            yield
    finally:
        gact_context.reset(scope_token)
        gact_context.reset(session_token)


def _request(*messages: Message, system: str = "You are clio.") -> Request:
    return Request(model=f"codex_sdk/{MODEL}", system=system, messages=messages, tools=(SEARCH,))


HEAD = Message.user("what?")


def _step(i: int) -> tuple[Message, Message]:
    call = ToolCallPart(id=f"c{i}", name="search", input={"query": str(i)})
    return (
        Message.assistant([TextPart(text=f"step {i}"), call]),
        Message(
            role="tool",
            parts=(ToolResultPart(id=f"c{i}", name="search", content=(TextPart(text=f"r{i}"),)),),
        ),
    )


def _run(engine: AsyncCodexSDKEngine, request: Request) -> Any:
    return asyncio.run(engine.complete(request))


def _stateful(rows: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
    return [
        (r["stateful_mode"], r.get("reason")) for r in rows if r["stage"] == "provider.stateful"
    ]


# --------------------------------------------------------------------------- #
# the request as text, the reply as typed parts                               #
# --------------------------------------------------------------------------- #
def test_a_full_send_carries_system_rules_tools_and_messages(client: FakeClient) -> None:
    client.turns = [[_text("Searching." + _block(("search", {"query": "x"}))), _usage(100, 0, 9)]]
    response = _run(AsyncCodexSDKEngine(MODEL, effort="high"), _request(HEAD))

    [send] = client.sends
    # The system prompt and tool rules are the thread's developer instructions;
    # the prompt is only the conversation, with the per-turn tool reminder.
    assert send["instructions"].startswith("You are clio.\n\n# How you act")
    assert "# Available tools" in send["instructions"]
    assert send["prompt"] == f"[user]\nwhat?\n\n{TURN_REMINDER}"
    assert (send["thread_id"], send["keep_thread"], send["model"]) == (None, False, MODEL)
    assert send["effort"].value == "high"
    parts = response.message.parts
    assert parts[0] == TextPart(text="Searching.")
    assert (parts[1].name, parts[1].input) == ("search", {"query": "x"})
    assert response.finish_reason == "tool_call"
    assert (response.usage.input_tokens, response.usage.cache_read_tokens) == (100, 0)


def test_a_reply_without_a_block_is_the_answer(client: FakeClient) -> None:
    client.turns = [[_text("It is 42.")]]
    response = _run(AsyncCodexSDKEngine(MODEL), _request(HEAD))
    assert response.message.parts == (TextPart(text="It is 42."),)
    assert response.finish_reason == "stop"


def test_the_stream_holds_back_the_block_and_carries_thinking(client: FakeClient) -> None:
    reply = "Looking." + _block(("search", {"query": "x"}))
    client.turns = [
        [
            _ev("item/reasoning/summaryTextDelta", delta="plan"),
            *[_text(reply[i : i + 4]) for i in range(0, len(reply), 4)],
            _usage(50, 40, 5),
        ]
    ]

    async def collect() -> list[Any]:
        return [e async for e in AsyncCodexSDKEngine(MODEL).stream(_request(HEAD))]

    events = asyncio.run(collect())
    deltas = [e.delta for e in events if e.type == "delta"]
    assert "".join(d.text for d in deltas if d.type == "thinking") == "plan"
    assert "".join(d.text for d in deltas if d.type == "text") == "Looking."
    [call] = [d for d in deltas if d.type == "tool_call"]
    assert (call.name, json.loads(call.input)) == ("search", {"query": "x"})
    end = events[-1]
    assert (end.type, end.usage.cache_read_tokens, end.usage.reasoning_tokens) == ("end", 40, 2)


def test_a_blank_reply_is_a_typed_server_error(client: FakeClient) -> None:
    client.turns = [[_text("  ")]]
    with pytest.raises(ServerError, match="empty content"):
        _run(AsyncCodexSDKEngine(MODEL), _request(HEAD))


@pytest.mark.parametrize(
    ("message", "typed"),
    [("401 Unauthorized: please login", AuthError), ("upstream exploded", ServerError)],
)
def test_sdk_failures_raise_lm15_types(
    client: FakeClient, message: str, typed: type[Exception]
) -> None:
    client.turns = [CodexSDKError(message)]
    with pytest.raises(typed):
        _run(AsyncCodexSDKEngine(MODEL), _request(HEAD))


# --------------------------------------------------------------------------- #
# one thread per conversation                                                 #
# --------------------------------------------------------------------------- #
def test_outside_a_loop_every_call_is_a_full_unkept_send(
    client: FakeClient, audit: list[dict[str, Any]]
) -> None:
    client.turns = [[_text("a")], [_text("b")]]
    engine = AsyncCodexSDKEngine(MODEL)
    _run(engine, _request(HEAD))
    _run(engine, _request(HEAD, *_step(0)))
    assert [s["thread_id"] for s in client.sends] == [None, None]
    assert [s["keep_thread"] for s in client.sends] == [False, False]
    assert _stateful(audit) == []


def test_append_only_calls_continue_the_thread_with_the_new_messages_only(
    client: FakeClient, audit: list[dict[str, Any]], loop_scope: None
) -> None:
    client.turns = [[_text("a")], [_text("b")], [_text("c")]]
    engine = AsyncCodexSDKEngine(MODEL)
    _run(engine, _request(HEAD))
    _run(engine, _request(HEAD, *_step(0)))
    _run(engine, _request(HEAD, *_step(0), *_step(1)))

    assert [s["thread_id"] for s in client.sends] == [None, "thread-1", "thread-1"]
    # The continued sends carry only the new tool results: no system, no replayed reply.
    assert client.sends[1]["prompt"] == f"[tool results]\n[c0 search]\nr0\n\n{TURN_REMINDER}"
    assert client.sends[2]["prompt"] == f"[tool results]\n[c1 search]\nr1\n\n{TURN_REMINDER}"
    assert _stateful(audit) == [("full", "first_call"), ("delta", None), ("delta", None)]


def test_an_edited_history_opens_a_new_thread_and_archives_the_old(
    client: FakeClient, audit: list[dict[str, Any]], loop_scope: None
) -> None:
    client.turns = [[_text("a")], [_text("b")]]
    engine = AsyncCodexSDKEngine(MODEL)
    _run(engine, _request(HEAD))
    _run(engine, _request(Message.user("a different head"), *_step(0)))

    assert [s["thread_id"] for s in client.sends] == [None, None]
    assert client.sends[1]["prompt"].startswith("[user]\na different head")
    assert client.sends[1]["instructions"].startswith("You are clio.")
    assert "thread-1" in client.archived
    assert _stateful(audit)[1] == ("full", "prefix_mismatch")


def test_a_changed_system_prompt_is_not_a_delta(
    client: FakeClient, audit: list[dict[str, Any]], loop_scope: None
) -> None:
    client.turns = [[_text("a")], [_text("b")]]
    engine = AsyncCodexSDKEngine(MODEL)
    _run(engine, _request(HEAD))
    _run(engine, _request(HEAD, *_step(0), system="Another prompt."))
    assert _stateful(audit)[1] == ("full", "prefix_mismatch")


def test_a_lost_thread_resends_in_full_typed(
    client: FakeClient, audit: list[dict[str, Any]], loop_scope: None
) -> None:
    client.turns = [[_text("a")], CodexThreadLostError("thread-1"), [_text("b")]]
    engine = AsyncCodexSDKEngine(MODEL)
    _run(engine, _request(HEAD))
    response = _run(engine, _request(HEAD, *_step(0)))

    assert response.message.parts == (TextPart(text="b"),)
    assert [s["thread_id"] for s in client.sends] == [None, "thread-1", None]
    assert _stateful(audit)[-1] == ("full", "session_evicted")


def test_a_provider_error_resets_the_conversation(
    client: FakeClient, audit: list[dict[str, Any]], loop_scope: None
) -> None:
    client.turns = [[_text("a")], CodexSDKError("boom"), [_text("b")]]
    engine = AsyncCodexSDKEngine(MODEL)
    _run(engine, _request(HEAD))
    with pytest.raises(ServerError):
        _run(engine, _request(HEAD, *_step(0)))
    _run(engine, _request(HEAD, *_step(0)))
    assert _stateful(audit)[-1] == ("full", "provider_error")


def test_a_codex_compaction_resets_the_next_call(
    client: FakeClient, audit: list[dict[str, Any]], loop_scope: None
) -> None:
    client.turns = [[_text("a"), "compacted"], [_text("b")]]
    engine = AsyncCodexSDKEngine(MODEL)
    _run(engine, _request(HEAD))
    _run(engine, _request(HEAD, *_step(0)))
    assert _stateful(audit)[-1] == ("full", "provider_compacted")


def test_an_arc_op_resets_every_conversation_the_forward_drove(
    client: FakeClient, audit: list[dict[str, Any]], loop_scope: None
) -> None:
    client.turns = [[_text("a")], [_text("b")]]
    engine = AsyncCodexSDKEngine(MODEL)
    _run(engine, _request(HEAD))
    assert stateful_common.note_prefix_reset_for_active_scope()
    _run(engine, _request(HEAD, *_step(0)))
    assert _stateful(audit)[-1] == ("full", "ops_reset")


def test_the_thread_outlives_the_forward(client: FakeClient, audit: list[dict[str, Any]]) -> None:
    client.turns = [[_text("a")], [_text("b")]]
    engine = AsyncCodexSDKEngine(MODEL)
    session_token = gact_context.set_session_id("sess-a")
    scope_token = gact_context.set_react_scope("main", "react")
    try:
        with stateful_common.stateful_scope():
            _run(engine, _request(HEAD))
        with stateful_common.stateful_scope():
            _run(engine, _request(HEAD, *_step(0)))
    finally:
        gact_context.reset(scope_token)
        gact_context.reset(session_token)
    assert [s["thread_id"] for s in client.sends] == [None, "thread-1"]


# --------------------------------------------------------------------------- #
# dspy.LM on the engine pair                                                  #
# --------------------------------------------------------------------------- #
def test_dspy_lm_runs_the_engine_sync_and_async(client: FakeClient) -> None:
    client.turns = [[_text("sync")], [_text("async")]]
    lm = dspy.LM(
        f"codex_sdk/{MODEL}",
        engine=CodexSDKEngine(MODEL),
        async_engine=AsyncCodexSDKEngine(MODEL),
        cache=False,
        num_retries=0,
    )
    assert lm(_request(HEAD)).message.parts == (TextPart(text="sync"),)
    assert asyncio.run(lm.acall(_request(HEAD))).message.parts == (TextPart(text="async"),)


# --------------------------------------------------------------------------- #
# the factory                                                                 #
# --------------------------------------------------------------------------- #
def test_create_lm_builds_the_engine_lm_for_the_sdk_variant() -> None:
    from clio_agent.config import LMProviderConfig
    from clio_agent.lm.factory import create_lm
    from clio_agent.lm.request_config import config_from_lm_kwargs

    config = LMProviderConfig(
        provider="codex", model="gpt-5.5", api_base="codex://direct", codex_variant="sdk"
    )
    config.thinking_level = "high"  # type: ignore[attr-defined]
    lm = create_lm(config)

    assert isinstance(lm, dspy.LM)
    assert lm.model == "codex_sdk/gpt-5.5"
    assert isinstance(lm.engine, CodexSDKEngine)
    assert (lm.engine.model, lm.engine.effort) == ("gpt-5.5", "high")
    assert isinstance(lm._async_engine_spec, AsyncCodexSDKEngine)
    assert lm._clio_provider_id == "codex"  # type: ignore[attr-defined]
    # Every kwarg left on the LM has a Request.config spelling (nothing silently dropped).
    config_from_lm_kwargs(lm.kwargs)


def test_a_plan_limit_hit_is_the_terminal_codex_plan_limit_never_retried() -> None:
    """#1529 on the SDK engine: an exhausted plan window is the typed terminal
    ``CodexPlanLimitError`` (as on the direct engine), never a retryable ``ServerError``
    that DSPy would re-issue with backoff."""
    from dspy.utils.exceptions import is_retryable_lm_error

    from clio_agent.providers.codex.errors import CodexPlanLimitError

    err = sdk_engine._typed(
        CodexSDKError("turn failed: You've hit your usage limit. Try again in 3 hours."),
        "gpt-6-sol",
    )

    assert isinstance(err, CodexPlanLimitError)
    assert not is_retryable_lm_error(err)


def test_an_oversized_image_is_refused_typed_before_any_send(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dspy.lm15 import ImagePart

    from clio_agent.providers import native_attachment_bounds
    from clio_agent.providers.native_attachment_bounds import NativeAttachmentTooLargeError

    monkeypatch.setattr(native_attachment_bounds, "image_max_bytes", lambda: 4)
    image = ImagePart(data="aGVsbG8gd29ybGQ=", media_type="image/png")  # 11 bytes
    with pytest.raises(NativeAttachmentTooLargeError):
        _run(
            AsyncCodexSDKEngine(MODEL),
            _request(Message(role="user", parts=(TextPart(text="see"), image))),
        )
    assert client.sends == []
