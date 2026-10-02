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
* a terminal in-stream failure the Codex CLI retries (an overload, an unknown code, a
  5xx frame; ``error`` or ``response.failed``) is retried by DSPy's own retry policy,
  each attempt logged, and reaches the user as ``LMServerError`` once exhausted; a
  final one (invalid prompt, context window, usage not included, a 4xx) is not;
* HTTP mode streams lm15's own stateless transport (no socket); a usage-limit 429
  there is ``CodexPlanLimitError``;
* the wire signs in with clio's own Codex credential (a fresh token per call) and,
  without one, with the local Codex CLI login (``~/.codex/auth.json``);
* the typed usage carries cached input; ``create_lm`` builds the engine LM.
"""

from __future__ import annotations

import asyncio
import builtins
import dataclasses
import json
from collections.abc import Iterator
from typing import Any

import pytest
import websockets
from dspy.clients.errors import wrap_error
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
    TransportError,
)
from dspy.lm15 import (
    TimeoutError as RequestTimeoutError,
)
from dspy.utils.exceptions import is_retryable_lm_error
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
        # An exception in the script is the connection dropping at that point.
        self._pending = [e if isinstance(e, BaseException) else json.dumps(e) for e in events]

    def __aiter__(self) -> FakeSocket:
        return self

    async def __anext__(self) -> str:
        if not self._pending:
            raise StopAsyncIteration
        item = self._pending.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

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
    for kept in (direct_engine._CONVERSATIONS, direct_engine._RESETS, direct_engine._FORWARDS):
        kept.clear()
    yield h
    for kept in (direct_engine._CONVERSATIONS, direct_engine._RESETS, direct_engine._FORWARDS):
        kept.clear()


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


def test_an_arc_op_on_the_forward_resets_typed_ops_reset(
    harness: Harness, audit: list[dict[str, Any]], loop_scope: None
) -> None:
    """Compaction's hook (``note_prefix_reset_for_active_scope``) reaches Codex direct:
    the next call is a full send on a new socket audited ``ops_reset``, never an
    inferred ``prefix_mismatch``. Sabotage: drop the engine's
    ``register_scope_registry`` -> ``prefix_mismatch`` -> red."""
    engine = _engine()
    _run(engine, _request(HEAD))
    _run(engine, _request(HEAD, *_step(0)))
    assert stateful_common.note_prefix_reset_for_active_scope("ops_reset") is True
    _run(engine, _request(Message.user("[earlier context] summary"), *_step(1)))

    assert _stateful(audit) == [("full", "first_call"), ("delta", None), ("full", "ops_reset")]
    assert len(harness.sockets) == 2 and harness.sockets[0].closed
    assert "previous_response_id" not in harness.sockets[1].frames[0]


def test_each_call_builds_its_wire_request_once(
    harness: Harness, loop_scope: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delta is a slice of the one build (no re-render of the prefix)."""
    wire = _wire()
    builds: list[int] = []
    real = wire.build_request

    def counted(request: Request, stream: bool) -> Any:
        builds.append(len(request.messages))
        return real(request, stream)

    monkeypatch.setattr(wire, "build_request", counted)
    engine = _engine(wire=wire)
    for messages in ((HEAD,), (HEAD, *_step(0)), (HEAD, *_step(0), *_step(1))):
        _run(engine, _request(*messages))
    assert builds == [1, 3, 5]


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


def _service_restart() -> websockets.ConnectionClosedError:
    from websockets.frames import Close

    close = Close(1012, "service restart")
    return websockets.ConnectionClosedError(close, close, True)


def test_a_connection_closed_before_any_output_reconnects_and_resends(
    harness: Harness, audit: list[dict[str, Any]], loop_scope: None
) -> None:
    """Codex restarting (WebSocket 1012) before any output is not the user's problem:
    the call reconnects and resends in full, typed as an evicted session."""
    harness.script[:] = ["one", [_service_restart()], "two"]
    engine = _engine()
    _run(engine, _request(HEAD))
    response = _run(engine, _request(HEAD, *_step(0)))

    first, second = harness.sockets
    [resend] = second.frames
    assert "previous_response_id" not in resend
    assert len(resend["input"]) > len(first.frames[-1]["input"])  # the FULL body
    assert response.message.parts == (TextPart(text="two"),)
    assert _stateful(audit)[-1] == ("full", "session_evicted")


def test_a_connection_closed_mid_reply_is_a_clear_server_error(harness: Harness) -> None:
    started = _answer("resp_1", "partial")[:2]
    harness.script[:] = [[*started, _service_restart()]]
    with pytest.raises(ServerError, match="Codex closed the connection during the reply"):
        _run(_engine(), _request(HEAD))


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


@pytest.mark.parametrize(
    ("raised", "typed"),
    [
        (builtins.TimeoutError("timed out during opening handshake"), RequestTimeoutError),
        (ConnectionRefusedError("refused"), TransportError),
        (OSError("getaddrinfo failed"), TransportError),
    ],
)
def test_a_connect_that_never_completes_is_a_retryable_typed_error(
    monkeypatch: pytest.MonkeyPatch, raised: BaseException, typed: type[Exception]
) -> None:
    """Found live (opal, 2026-09-30): a handshake timeout reached the user raw.

    Typed as lm15 errors DSPy retries them, and a lasting failure reaches the
    user as a provider error in plain words.
    """

    async def stall(*_a: Any, **_k: Any) -> Any:
        raise raised

    monkeypatch.setattr(direct_engine.websockets, "connect", stall)
    with pytest.raises(typed) as caught:
        asyncio.run(direct_engine._connect({"Authorization": "Bearer t"}, None))
    assert "Codex" in str(caught.value)
    assert is_retryable_lm_error(wrap_error(caught.value, model="codex_direct/gpt-6-sol"))


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


def test_without_a_clio_sign_in_the_wire_reads_the_codex_home_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """No clio sign-in: the wire reads ``$CODEX_HOME/auth.json``, not lm15's fixed ``~/.codex``."""
    from clio_agent.providers.codex import credentials

    class Store:
        def load(self) -> None:
            return None

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "tok-cli", "account_id": "acct-cli"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(credentials, "CodexCredentialStore", Store)

    wire = direct_engine.default_wire()
    headers = dict(wire.build_request(_request(HEAD), stream=True).headers)
    assert headers["Authorization"] == "Bearer tok-cli"
    assert headers["chatgpt-account-id"] == "acct-cli"
    assert headers["originator"] == c.ORIGINATOR


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


def test_codex_is_asked_for_its_reasoning_summary(harness: Harness) -> None:
    """The model's thinking is shown to the user (owner: everything the model outputs
    is displayed) -- Codex only streams it when the request asks for a summary."""
    _run(_engine(), _request(HEAD))
    [frame] = harness.sockets[0].frames
    assert frame["reasoning"]["summary"] == "auto"


def test_a_set_effort_keeps_its_value_and_gains_the_summary(harness: Harness) -> None:
    from dspy.lm15 import Config, Reasoning

    request = dataclasses.replace(_request(HEAD), config=Config(reasoning=Reasoning(effort="low")))
    _run(_engine(), request)
    [frame] = harness.sockets[0].frames
    assert frame["reasoning"] == {"effort": "low", "summary": "auto"}


def _calling(rid: str, call_id: str) -> list[dict[str, Any]]:
    """A reply that calls ``search`` (``call_id``), as the backend streams it."""
    item = {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": "search",
        "arguments": '{"q": "x"}',
        "status": "completed",
    }
    return [
        {"type": "response.created", "response": {"id": rid}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**item, "arguments": ""},
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": item["id"],
            "output_index": 0,
            "delta": item["arguments"],
        },
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {
            "type": "response.completed",
            "response": {"id": rid, "status": "completed", "output": [item], "usage": {}},
        },
    ]


def test_a_reply_that_is_not_the_providers_own_is_a_full_send(
    harness: Harness, audit: list[dict[str, Any]], loop_scope: None
) -> None:
    """Found live (2026-10-02): after a drafts pick the next turn failed "No tool output
    found for function call": the provider's reply was the ``draft_alternatives`` call,
    the conversation continued from the picked draft's text, and the delta left that call
    unanswered on the backend. Sabotage: drop the reply-calls check -> a delta -> red."""
    harness.script[:] = [_calling("resp_1", "call_draft"), "two"]
    engine = _engine()
    _run(engine, _request(HEAD))
    picked = Message.assistant([TextPart(text="Hi Dana, the shipment is late.")])
    _run(engine, _request(HEAD, picked, Message.user("make it shorter")))

    assert _stateful(audit) == [("full", "first_call"), ("full", "prefix_mismatch")]
    assert "previous_response_id" not in harness.sockets[-1].frames[-1]


def test_the_providers_own_calls_answered_continue_as_a_delta(
    harness: Harness, audit: list[dict[str, Any]], loop_scope: None
) -> None:
    harness.script[:] = [_calling("resp_1", "call_0"), "two"]
    engine = _engine()
    _run(engine, _request(HEAD))
    _run(engine, _request(HEAD, *_step(0)))

    assert _stateful(audit) == [("full", "first_call"), ("delta", None)]
    [socket] = harness.sockets
    assert socket.frames[1]["previous_response_id"] == "resp_1"
    assert [item.get("type") for item in socket.frames[1]["input"]] == ["function_call_output"]


# --------------------------------------------------------------------------- #
# transient in-stream failures: DSPy retries them, loudly                     #
# --------------------------------------------------------------------------- #
OVERLOADED = "Our servers are currently overloaded. Please try again later."


def _failure(
    error: dict[str, Any], *, kind: str = "error", status: int | None = None
) -> list[dict[str, Any]]:
    """One reply that is only a terminal error frame (``error`` or ``response.failed``)."""
    if kind == "response.failed":
        return [{"type": kind, "response": {"id": "resp_x", "status": "failed", "error": error}}]
    frame: dict[str, Any] = {"type": kind, "error": error}
    if status is not None:
        frame["status"] = status
    return [frame]


def _codex_lm(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The real ``create_lm`` Codex LM (DSPy's execution + retry over the direct engine)."""
    from clio_agent.config import LMProviderConfig
    from clio_agent.lm.factory import create_lm

    monkeypatch.setattr(direct_engine, "default_wire", _wire)
    return create_lm(LMProviderConfig(provider="codex", model=MODEL, api_base="codex://direct"))


def _retry_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "clio_agent.providers.codex.stream_errors"
    ]


@pytest.mark.parametrize(
    "failure",
    [
        # The live failure (early adopters' data scenario, 2026-10-02).
        _failure(
            {
                "type": "service_unavailable_error",
                "code": "server_is_overloaded",
                "message": OVERLOADED,
            }
        ),
        _failure({"code": "server_is_overloaded", "message": OVERLOADED}, kind="response.failed"),
        _failure({"code": "slow_down", "message": "Slow down."}),
        # An unknown code (the live opal case): the Codex CLI retries what it does not know.
        _failure(
            {
                "code": "access_check_failed",
                "message": "Unable to verify Daybreak Blue access. Please try again.",
            }
        ),
        _failure({"message": OVERLOADED}, status=503),  # no code at all: a 5xx frame
    ],
    ids=["overloaded", "response_failed", "slow_down", "unknown_code", "status_5xx"],
)
def test_a_transient_in_stream_failure_is_retried_by_dspy_and_logged(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: list[dict[str, Any]],
) -> None:
    """Found live (data scenario, 2026-10-02): Codex streamed "Our servers are currently
    overloaded" as its terminal frame; lm15 typed it a generic ``ProviderError``, DSPy
    never retried it, and the user's turn died. The Codex CLI retries it; so does clio,
    through DSPy's own retry policy, each retry logged with its attempt."""
    from clio_agent.lm.policy import lm_retries

    harness.script[:] = [failure, "recovered"]
    lm = _codex_lm(monkeypatch)
    with caplog.at_level("INFO", logger="clio_agent.providers.codex.stream_errors"):
        out = lm("describe the data")

    assert out == ["recovered"]
    assert len(harness.sockets) == 2  # one failed attempt, one retry
    failed, recovered = _retry_lines(caplog)
    assert "reason=codex_transient_stream_error " in failed
    assert f"attempt=1/{lm_retries() + 1}" in failed
    assert "action=dspy_retries" in failed
    assert "reason=codex_transient_stream_error_recovered attempt=2" in recovered


def test_the_overload_code_is_carried_on_the_retryable_error(harness: Harness) -> None:
    harness.script[:] = [
        _failure(
            {
                "type": "service_unavailable_error",
                "code": "server_is_overloaded",
                "message": OVERLOADED,
            }
        )
    ]
    with pytest.raises(ServerError) as caught:
        _run(_engine(), _request(HEAD))
    assert caught.value.provider_code == "server_is_overloaded"
    assert caught.value.message == OVERLOADED
    assert is_retryable_lm_error(wrap_error(caught.value, model="codex_direct/gpt-6-sol"))


def test_retries_exhausted_reach_the_user_as_the_typed_server_error(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from dspy.utils.exceptions import LMServerError

    from clio_agent.lm.policy import lm_retries

    attempts = lm_retries() + 1
    overloaded = _failure({"code": "server_is_overloaded", "message": OVERLOADED})
    harness.script[:] = [overloaded] * attempts
    lm = _codex_lm(monkeypatch)
    with (
        caplog.at_level("INFO", logger="clio_agent.providers.codex.stream_errors"),
        pytest.raises(LMServerError, match="currently overloaded"),
    ):
        lm("describe the data, again")

    assert len(harness.sockets) == attempts  # bounded by DSPy's retry count
    lines = _retry_lines(caplog)
    assert len(lines) == attempts
    for n, line in enumerate(lines, 1):
        assert f"attempt={n}/{attempts}" in line
    assert "action=retries_exhausted" in lines[-1]


@pytest.mark.parametrize(
    ("failure", "typed"),
    [
        (
            _failure({"code": "invalid_prompt", "message": "Invalid prompt."}),
            "LMInvalidRequestError",
        ),
        (
            _failure(
                {"code": "context_length_exceeded", "message": "Too long."},
                kind="response.failed",
            ),
            "ContextWindowExceededError",
        ),
        (_failure({"code": "usage_not_included", "message": "Not in plan."}), "LMProviderError"),
        (_failure({"code": "bad_thing", "message": "Rejected."}, status=400), "LMProviderError"),
    ],
    ids=["invalid_prompt", "context_length_failed", "usage_not_included", "status_4xx"],
)
def test_a_final_in_stream_failure_is_not_retried(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: list[dict[str, Any]],
    typed: str,
) -> None:
    import dspy.utils.exceptions as dspy_errors

    harness.script[:] = [failure, "never sent"]
    lm = _codex_lm(monkeypatch)
    with (
        caplog.at_level("INFO", logger="clio_agent.providers.codex.stream_errors"),
        pytest.raises(dspy_errors.LMError) as caught,
    ):
        lm(f"final failure {typed}")

    assert type(caught.value).__name__ == typed
    assert not is_retryable_lm_error(caught.value)
    assert len(harness.sockets) == 1  # never retried
    assert _retry_lines(caplog) == []
