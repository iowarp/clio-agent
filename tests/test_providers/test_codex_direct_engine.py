"""Codex direct engine (:mod:`clio_agent.providers.codex.direct_engine`).

lm15 builds the Responses payload and parses the events (the real ``OpenAICodexLM``,
with a static test credential); the WebSocket is faked. Pins:

* a full send opens a socket and keeps it inside an agent loop; append-only calls
  send only the new messages' input items with ``previous_response_id`` on the same
  socket (calls 2 and 3 of a conversation are both deltas);
* an edited history or a changed tool list sends in full on a new socket, typed;
  outside a loop every call is a one-shot socket;
* a lost continuation (``previous_response_not_found``) closes the socket, opens a
  fresh one and resends the FULL body once, audited ``session_evicted``;
* errors reach the user clearly: an exhausted plan (in-stream or at the handshake) is
  ``CodexPlanLimitError``, a refused sign-in at the handshake is ``AuthError``, any
  other handshake status is ``ServerError``, a stream that ends without a completion
  event is ``ServerError``;
* HTTP mode streams lm15's own stateless transport (no socket); a usage-limit 429
  there is ``CodexPlanLimitError``;
* the wire signs in with clio's own Codex credential (a fresh token per call) and,
  without one, with the local Codex CLI login (``~/.codex/auth.json``);
* the typed usage carries cached input; ``create_lm`` builds the engine LM.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest
import websockets
from dspy.lm15 import (
    AuthError,
    FunctionTool,
    Message,
    OpenAICodexLM,
    RateLimitError,
    Request,
    ServerError,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from websockets.datastructures import Headers
from websockets.http11 import Response as HandshakeResponse

from clio_agent.gact import context as gact_context
from clio_agent.providers import stateful_common
from clio_agent.providers.codex import constants as c
from clio_agent.providers.codex import direct_engine
from clio_agent.providers.codex.direct_engine import AsyncCodexDirectEngine
from clio_agent.providers.codex.errors import CodexPlanLimitError

MODEL = "gpt-5.5"
SEARCH = FunctionTool(
    name="search",
    description="Search.",
    parameters={"type": "object", "properties": {"q": {"type": "string"}}},
)


def _answer(rid: str, text: str) -> list[dict[str, Any]]:
    return [
        {"type": "response.created", "response": {"id": rid}},
        {
            "type": "response.output_text.delta",
            "delta": text,
            "item_id": "m1",
            "output_index": 0,
            "content_index": 0,
        },
        {
            "type": "response.completed",
            "response": {
                "id": rid,
                "status": "completed",
                "output": [],
                "usage": {
                    "input_tokens": 1000,
                    "input_tokens_details": {"cached_tokens": 900},
                    "output_tokens": 5,
                },
            },
        },
    ]


class FakeSocket:
    """One WebSocket: records frames and answers each from the shared script."""

    def __init__(self, sockets: list[FakeSocket], script: list[Any]) -> None:
        self.frames: list[dict[str, Any]] = []
        self.closed = False
        self._script = script
        self._pending: list[str] = []
        sockets.append(self)

    async def send(self, raw: str) -> None:
        self.frames.append(json.loads(raw))
        reply = self._script.pop(0)
        events = _answer(f"resp_{len(self.frames)}", reply) if isinstance(reply, str) else reply
        self._pending = [json.dumps(e) for e in events]

    def __aiter__(self) -> FakeSocket:
        return self

    async def __anext__(self) -> str:
        if not self._pending:
            raise StopAsyncIteration
        return self._pending.pop(0)

    async def close(self) -> None:
        self.closed = True


class Harness:
    def __init__(self) -> None:
        self.sockets: list[FakeSocket] = []
        self.script: list[Any] = ["one", "two", "three", "four"]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    h = Harness()

    async def connect(_headers: dict[str, str], _key: Any) -> FakeSocket:
        return FakeSocket(h.sockets, h.script)

    monkeypatch.setattr(direct_engine, "_connect", connect)
    direct_engine._CONVERSATIONS.clear()
    yield h
    direct_engine._CONVERSATIONS.clear()


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


def _wire() -> OpenAICodexLM:
    return OpenAICodexLM(api_key="test", account_id="acct")


def _engine(**kwargs: Any) -> AsyncCodexDirectEngine:
    return AsyncCodexDirectEngine(MODEL, wire=kwargs.pop("wire", None) or _wire(), **kwargs)


HEAD = Message.user("what?")


def _step(i: int) -> tuple[Message, Message]:
    call = ToolCallPart(id=f"call_{i}", name="search", input={"q": str(i)})
    return (
        Message.assistant([TextPart(text=f"step {i}"), call]),
        Message(
            role="tool",
            parts=(
                ToolResultPart(id=f"call_{i}", name="search", content=(TextPart(text=f"r{i}"),)),
            ),
        ),
    )


def _request(*messages: Message, tools: tuple[FunctionTool, ...] = (SEARCH,)) -> Request:
    return Request(
        model="codex_direct/gpt-5.5", system="You are clio.", messages=messages, tools=tools
    )


def _run(engine: AsyncCodexDirectEngine, request: Request) -> Any:
    return asyncio.run(engine.complete(request))


def _stateful(rows: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
    return [
        (r["stateful_mode"], r.get("reason")) for r in rows if r["stage"] == "provider.stateful"
    ]


# --------------------------------------------------------------------------- #
# continuation                                                                #
# --------------------------------------------------------------------------- #
def test_append_only_calls_send_only_new_items_on_the_same_socket(
    harness: Harness, audit: list[dict[str, Any]], loop_scope: None
) -> None:
    engine = _engine()
    first = _run(engine, _request(HEAD))
    _run(engine, _request(HEAD, *_step(0)))
    _run(engine, _request(HEAD, *_step(0), *_step(1)))

    [socket] = harness.sockets
    full, second, third = socket.frames
    assert full["model"] == MODEL  # the bare backend model id
    assert "previous_response_id" not in full
    assert (second["previous_response_id"], third["previous_response_id"]) == ("resp_1", "resp_2")
    assert [item.get("type") for item in second["input"]] == ["function_call_output"]
    assert [item.get("type") for item in third["input"]] == ["function_call_output"]
    assert first.message.parts == (TextPart(text="one"),)
    assert (first.usage.input_tokens, first.usage.cache_read_tokens) == (1000, 900)
    assert _stateful(audit) == [("full", "first_call"), ("delta", None), ("delta", None)]


def test_an_edited_history_is_a_full_send_on_a_new_socket(
    harness: Harness, audit: list[dict[str, Any]], loop_scope: None
) -> None:
    engine = _engine()
    _run(engine, _request(HEAD))
    _run(engine, _request(Message.user("another head"), *_step(0)))
    assert len(harness.sockets) == 2
    assert "previous_response_id" not in harness.sockets[1].frames[0]
    assert _stateful(audit)[-1] == ("full", "prefix_mismatch")


def test_a_changed_tool_list_is_a_full_send(harness: Harness, loop_scope: None) -> None:
    engine = _engine()
    _run(engine, _request(HEAD))
    _run(engine, _request(HEAD, *_step(0), tools=()))
    assert "previous_response_id" not in harness.sockets[-1].frames[-1]


def test_outside_a_loop_every_call_is_a_one_shot_socket(harness: Harness) -> None:
    engine = _engine()
    _run(engine, _request(HEAD))
    _run(engine, _request(HEAD, *_step(0)))
    assert len(harness.sockets) == 2
    assert all(s.closed for s in harness.sockets)
    assert all("previous_response_id" not in s.frames[0] for s in harness.sockets)


def test_a_lost_continuation_resends_in_full_on_a_fresh_socket(
    harness: Harness, audit: list[dict[str, Any]], loop_scope: None
) -> None:
    lost = [{"type": "error", "error": {"code": c.PREVIOUS_RESPONSE_NOT_FOUND_CODE}}]
    harness.script[:] = ["one", lost, "two"]
    engine = _engine()
    _run(engine, _request(HEAD))
    response = _run(engine, _request(HEAD, *_step(0)))

    first, second = harness.sockets
    assert first.frames[-1]["previous_response_id"] == "resp_1"  # the delta that was lost
    [resend] = second.frames
    assert "previous_response_id" not in resend
    assert len(resend["input"]) > len(first.frames[-1]["input"])  # the FULL body
    assert response.message.parts == (TextPart(text="two"),)
    assert _stateful(audit)[-1] == ("full", "session_evicted")


# --------------------------------------------------------------------------- #
# clear errors                                                                #
# --------------------------------------------------------------------------- #
def test_an_in_stream_usage_limit_is_the_terminal_plan_limit_error(harness: Harness) -> None:
    harness.script[:] = [
        [
            {
                "type": "error",
                "error": {"code": "rate_limited", "message": "You hit your usage limit."},
            }
        ]
    ]
    with pytest.raises(CodexPlanLimitError, match="usage limit"):
        _run(_engine(), _request(HEAD))


def test_a_stream_without_a_completion_event_is_a_server_error(harness: Harness) -> None:
    harness.script[:] = [[{"type": "response.created", "response": {"id": "resp_1"}}]]
    with pytest.raises(ServerError, match="before the response completed"):
        _run(_engine(), _request(HEAD))


@pytest.mark.parametrize(
    ("status", "body", "typed"),
    [
        (401, b"invalid token", AuthError),
        (429, b"You have reached your monthly usage limit", CodexPlanLimitError),
        (503, b"upstream down", ServerError),
    ],
)
def test_handshake_failures_are_typed(
    monkeypatch: pytest.MonkeyPatch, status: int, body: bytes, typed: type[Exception]
) -> None:
    async def refuse(*_a: Any, **_k: Any) -> Any:
        raise websockets.InvalidStatus(HandshakeResponse(status, "x", Headers(), body))

    monkeypatch.setattr(direct_engine.websockets, "connect", refuse)
    with pytest.raises(typed):
        asyncio.run(direct_engine._connect({"Authorization": "Bearer t"}, None))


# --------------------------------------------------------------------------- #
# HTTP mode                                                                   #
# --------------------------------------------------------------------------- #
class _HttpWire:
    def __init__(self, fail: BaseException | None = None) -> None:
        self.requests: list[Request] = []
        self.fail = fail

    def stream(self, request: Request) -> Iterator[Any]:
        from dspy.lm15 import Response, Usage, response_to_events

        self.requests.append(request)
        if self.fail is not None:
            raise self.fail
        yield from response_to_events(
            Response(
                id="r",
                model=request.model,
                message=Message.assistant([TextPart(text="over http")]),
                finish_reason="stop",
                usage=Usage(input_tokens=10, output_tokens=2),
            )
        )


def test_http_mode_streams_lm15_http_without_a_socket(harness: Harness, loop_scope: None) -> None:
    wire = _HttpWire()
    response = _run(_engine(wire=wire, http=True), _request(HEAD))
    assert response.message.parts == (TextPart(text="over http"),)
    assert harness.sockets == []
    assert wire.requests[0].model == MODEL


def test_http_mode_usage_limit_is_the_plan_limit_error(harness: Harness) -> None:
    wire = _HttpWire(fail=RateLimitError("insufficient_quota: you are out of budget"))
    with pytest.raises(CodexPlanLimitError):
        _run(_engine(wire=wire, http=True), _request(HEAD))


# --------------------------------------------------------------------------- #
# credentials and the factory                                                 #
# --------------------------------------------------------------------------- #
def test_the_wire_uses_clios_sign_in_with_a_fresh_token_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from clio_agent.providers.codex import credentials

    tokens = iter(["tok-1", "tok-2"])

    class Store:
        def load(self) -> Any:
            return SimpleNamespace(account_id="acct-9")

        def get_valid_credential(self) -> Any:
            return SimpleNamespace(access_token=next(tokens))

    monkeypatch.setattr(credentials, "CodexCredentialStore", Store)
    wire = direct_engine.default_wire()
    headers = [dict(wire.build_request(_request(HEAD), stream=True).headers) for _ in range(2)]
    assert [h["Authorization"] for h in headers] == ["Bearer tok-1", "Bearer tok-2"]
    assert headers[0]["chatgpt-account-id"] == "acct-9"


def test_without_a_clio_sign_in_the_wire_reads_the_codex_cli_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.providers.codex import credentials

    seen: list[str] = []

    class Store:
        def load(self) -> None:
            return None

    monkeypatch.setattr(credentials, "CodexCredentialStore", Store)
    monkeypatch.setattr(
        OpenAICodexLM,
        "from_codex_cli",
        classmethod(lambda cls, **kw: seen.append(kw["originator"])),
    )
    direct_engine.default_wire()
    assert seen == [c.ORIGINATOR]  # ~/.codex/auth.json via lm15 -- allowed by the owner


def test_create_lm_builds_the_direct_engine_lm(monkeypatch: pytest.MonkeyPatch) -> None:
    import dspy

    from clio_agent.config import LMProviderConfig
    from clio_agent.lm.factory import create_lm

    monkeypatch.setattr(direct_engine, "default_wire", _wire)
    config = LMProviderConfig(provider="codex", model="gpt-5.5", api_base="codex://direct")
    config.thinking_level = "high"  # type: ignore[attr-defined]
    lm = create_lm(config)

    assert isinstance(lm, dspy.LM)
    assert lm.model == "codex_direct/gpt-5.5"
    assert isinstance(lm._engine_spec, direct_engine.CodexDirectEngine)
    assert lm._engine_spec.http is False
    assert lm._clio_prompt_cache_key is True  # type: ignore[attr-defined]
    assert lm.kwargs["reasoning_effort"] == "high"
    assert not any(k.startswith("codex_") for k in lm.kwargs)


def test_an_oversized_image_is_refused_typed_before_any_send(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dspy.lm15 import ImagePart

    from clio_agent.providers import native_attachment_bounds
    from clio_agent.providers.native_attachment_bounds import NativeAttachmentTooLargeError

    monkeypatch.setattr(native_attachment_bounds, "image_max_bytes", lambda: 4)
    image = ImagePart(data="aGVsbG8gd29ybGQ=", media_type="image/png")  # 11 bytes
    with pytest.raises(NativeAttachmentTooLargeError):
        _run(_engine(), _request(Message(role="user", parts=(TextPart(text="see"), image))))
    assert harness.sockets == []
