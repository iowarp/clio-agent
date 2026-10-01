"""Codex direct as a DSPy 3.4 engine: lm15's Responses wire over a kept WebSocket.

The Codex backend (``chatgpt.com/backend-api/codex``) speaks the OpenAI Responses API.
lm15's :class:`OpenAICodexLM` builds the request payload (native function tools,
reasoning pass-back, images, ``prompt_cache_key``) and parses the event stream; this
engine changes only the transport:

* **One WebSocket per conversation.** Inside an agent loop the socket for ``(session,
  scope, model)`` is kept open. A call whose messages repeat everything the
  conversation was sent plus the model's own reply, then add only non-assistant
  messages, sends only the input items for those new messages with
  ``previous_response_id`` -- the continuation lives on the open connection, so it
  works with ``store: false``. Anything else (an edit, a different system prompt or
  tool list, an idle or aged socket) sends the full input, typed on the
  ``provider.stateful`` audit row. Outside a loop every call is a full send on a
  short-lived socket.
* **One build per call.** The wire request is built once; the delta is a slice of its
  input items. Whether the kept conversation continues is decided by the item count
  it was sent and a running sha256 over those items (never an element-wise compare
  of the message history).
* **ARC ops reset typed.** The engine takes part in the per-forward scope registry
  (:func:`~clio_agent.providers.stateful_common.register_scope_registry`): an ARC op
  on a forward (compaction, delete) drops the conversations it drove, and the next
  call is a full send audited ``ops_reset`` -- not an inferred ``prefix_mismatch``.
* **An owner loop.** The loop runs each step under its own ``asyncio.run``; a socket
  is bound to the event loop that opened it, so every socket lives on this module's
  own daemon loop and events are bridged to the caller.
* **HTTP mode.** ``CLIO_CODEX_TRANSPORT=sse`` (a proxy that blocks WebSocket
  upgrades) runs lm15's own stateless HTTP transport instead -- an explicit choice,
  never a silent fallback.
* **Errors.** An exhausted plan window is clio's terminal ``CodexPlanLimitError``
  (never retried); a refused sign-in is a typed ``AuthError``; everything else is
  lm15's typed error.
* **Auth** is clio's own Codex sign-in when there is one (refreshed per call), else
  the local Codex CLI login (``$CODEX_HOME/auth.json``, default ``~/.codex``, read
  by lm15).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import queue
import threading
import time
import uuid
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass, field
from typing import Any

import websockets
from dspy.lm15 import (
    AuthError,
    Request,
    Response,
    ServerError,
    TransportError,
    materialize_response,
)
from dspy.lm15 import TimeoutError as ProviderTimeoutError

from clio_agent.lm.engines.conversations import conversation_key
from clio_agent.providers.codex import constants as c
from clio_agent.providers.codex.audit import (
    emit_call_started,
    emit_call_usage,
    emit_raw_event,
)
from clio_agent.providers.codex.errors import CodexPlanLimitError, is_usage_limit_text
from clio_agent.providers.stateful_common import (
    active_stateful_scope,
    register_scope_registry,
    stateful_reset_payload,
)

__all__ = [
    "AsyncCodexDirectEngine",
    "CodexDirectEngine",
    "PreparedRequest",
    "close_all",
    "continuation",
    "default_wire",
    "prepare_request",
]

_END = object()
_TERMINAL = frozenset({"response.completed", "response.failed", "response.incomplete", "error"})


@dataclass(frozen=True)
class _WireEvent:
    """One WebSocket message in the shape lm15's Responses parser reads (SSE-like)."""

    event: str
    data: str


class PreparedRequest:
    """One call's wire request, built once, with a running sha256 over its input items.

    Attributes:
        body: The full Responses payload (``input`` holds every item).
        headers: The transport headers.
        system: The instructions the payload carries.
        tools: The payload's tool list, serialized (the identity a continuation needs).
        items: The full input items.
    """

    def __init__(self, body: dict[str, Any], headers: dict[str, str]) -> None:
        """Wrap a built payload (see :func:`prepare_request`)."""
        self.body = body
        self.headers = headers
        self.system = str(body.get("instructions") or "")
        self.tools = json.dumps(body.get("tools") or [])
        self.items: list[Any] = list(body.get("input") or [])
        self._hasher = hashlib.sha256()
        self._hashed = 0

    def digest_at(self, count: int) -> str:
        """sha256 over the first ``count`` items (each as compact JSON), hashed once.

        Raises:
            ValueError: ``count`` is behind a point already hashed past, or past the end.
        """
        if count < self._hashed or count > len(self.items):
            raise ValueError(f"digest at {count}: hashed {self._hashed} of {len(self.items)}")
        for item in self.items[self._hashed : count]:
            self._hasher.update(
                json.dumps(item, separators=(",", ":"), ensure_ascii=False).encode()
            )
            self._hasher.update(b"\n")
        self._hashed = count
        return self._hasher.copy().hexdigest()


def prepare_request(wire: Any, request: Request) -> PreparedRequest:
    """Build ``request``'s wire payload -- the ONE build of a call."""
    wire_request = wire.build_request(request, stream=True)
    return PreparedRequest(json.loads(wire_request.body), dict(wire_request.headers))


def _from_assistant(item: Any) -> bool:
    """Whether an input item renders an assistant message (reply text, reasoning, call)."""
    if not isinstance(item, dict):
        return False
    return item.get("type") in {"reasoning", "function_call"} or item.get("role") == "assistant"


@dataclass
class _Conversation:
    socket: Any
    system: str
    tools: str
    held: int  # messages the provider was sent
    sent_items: int  # input items the provider holds from what it was sent
    digest: str  # running sha256 over those items
    response_id: str
    opened_at: float = field(default_factory=time.monotonic)
    used_at: float = field(default_factory=time.monotonic)

    def expired(self, now: float) -> bool:
        return (now - self.opened_at) >= c.WS_MAX_AGE_S or (now - self.used_at) >= c.WS_IDLE_CLOSE_S


def continuation(live: _Conversation, request: Request, prepared: PreparedRequest) -> int | None:
    """The index of the first input item to send on ``live``, or ``None`` (send in full).

    ``request`` continues the kept conversation when the system prompt and tools are the
    same, its messages repeat the ``live.held`` sent ones plus the provider's own reply
    and then add only non-assistant messages, and its first ``live.sent_items`` input
    items hash to what was sent. The reply's items follow; the new items are the rest.
    """
    messages = request.messages
    if prepared.system != live.system or prepared.tools != live.tools:
        return None
    if len(messages) <= live.held + 1 or messages[live.held].role != "assistant":
        return None
    if any(m.role == "assistant" for m in messages[live.held + 1 :]):
        return None
    items = prepared.items
    if len(items) <= live.sent_items or prepared.digest_at(live.sent_items) != live.digest:
        return None
    first_new = live.sent_items
    while first_new < len(items) and _from_assistant(items[first_new]):
        first_new += 1
    if first_new == live.sent_items or first_new == len(items):
        return None  # no reply items, or nothing new after them
    if any(_from_assistant(item) for item in items[first_new:]):
        return None
    return first_new


class _Owner:
    """The daemon event loop every socket lives on."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or not self._loop.is_running():
                loop = asyncio.new_event_loop()
                threading.Thread(target=loop.run_forever, name="codex-direct", daemon=True).start()
                self._loop = loop
            return self._loop


_OWNER = _Owner()
_CONVERSATIONS: dict[tuple[str, ...], _Conversation] = {}
_CONVERSATIONS_LOCK = threading.Lock()


class AsyncCodexDirectEngine:
    """Async Codex direct engine (see the module docstring)."""

    supports_function_calling = True
    supports_reasoning = True

    def __init__(self, model: str, *, wire: Any | None = None, http: bool = False) -> None:
        self.model = model
        self.wire = wire or default_wire()
        # ``http``: the operator chose stateless HTTP (``CLIO_CODEX_TRANSPORT=sse``,
        # e.g. behind a proxy that blocks WebSocket upgrades) -- no kept socket.
        self.http = http

    async def complete(self, request: Request) -> Response:
        """Run one call and return the assembled response."""
        return materialize_response(iter([e async for e in self.stream(request)]), request)

    async def stream(self, request: Request) -> AsyncGenerator[Any, None]:
        """Run one call on the owner loop, yielding lm15 stream events."""
        events: queue.SimpleQueue[Any] = queue.SimpleQueue()
        key = _driven_key(conversation_key(self.model))
        future = asyncio.run_coroutine_threadsafe(self._call(request, key, events), _OWNER.loop())
        loop = asyncio.get_running_loop()
        try:
            while True:
                item = await loop.run_in_executor(None, events.get)
                if item is _END:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            if not future.done():
                future.cancel()

    async def aclose(self) -> None:
        """Nothing to release per engine: sockets belong to their conversations."""

    async def _call(
        self, request: Request, key: tuple[str, ...] | None, out: queue.SimpleQueue[Any]
    ) -> None:
        try:
            _check_media(request)  # typed refusal of oversized attachments before any send
            request = _with_reasoning_summary(request)
            if self.http:
                await asyncio.to_thread(self._http, request, out)
                return
            await self._run(request, key, out)
        except BaseException as exc:  # noqa: BLE001 - surfaced on the caller's loop
            if key is not None:
                await _drop(key)
            out.put(exc)
        finally:
            out.put(_END)

    async def _run(
        self, request: Request, key: tuple[str, ...] | None, out: queue.SimpleQueue[Any]
    ) -> None:
        # The backend takes the bare model id, whatever the dspy.LM model string is.
        request = dataclasses.replace(request, model=self.model)
        prepared = prepare_request(self.wire, request)
        body = prepared.body
        items = prepared.items
        live, first_new, reason = _plan(key, request, prepared)
        frame = body
        if live is not None and first_new is not None:
            frame = {**body, "input": items[first_new:]}
            frame["previous_response_id"] = live.response_id
        socket = live.socket if live is not None else await _connect(prepared.headers, key)
        _audit(key, self.model, request, live is not None, reason, len(frame.get("input") or []))
        try:
            response_id = await _exchange(self.wire, request, socket, frame, out)
        except (_ContinuationLost, _ConnectionLost):
            # The backend no longer holds the previous response, or the connection
            # dropped (a service restart, an idle-closed socket) -- nothing was streamed:
            # resend in full on a fresh socket, typed.
            _close_soon(socket)
            socket = await _connect(prepared.headers, key)
            _audit(key, self.model, request, False, "session_evicted", len(items))
            response_id = await _exchange(self.wire, request, socket, body, out)
        if key is None:
            await socket.close()
            return
        with _CONVERSATIONS_LOCK:
            _CONVERSATIONS[key] = _Conversation(
                socket=socket,
                system=prepared.system,
                tools=prepared.tools,
                held=len(request.messages),
                sent_items=len(items),
                digest=prepared.digest_at(len(items)),
                response_id=response_id,
                opened_at=live.opened_at if live is not None else time.monotonic(),
            )

    def _http(self, request: Request, out: queue.SimpleQueue[Any]) -> None:
        """One stateless HTTP call (lm15's own transport), events forwarded as they come."""
        from dspy.lm15 import RateLimitError  # noqa: PLC0415

        try:
            for event in self.wire.stream(dataclasses.replace(request, model=self.model)):
                out.put(event)
        except RateLimitError as exc:
            if is_usage_limit_text(str(exc)):
                raise CodexPlanLimitError(str(exc)) from exc
            raise


class CodexDirectEngine(AsyncCodexDirectEngine):
    """Sync twin: the same owner-loop call, consumed on the calling thread."""

    def complete(self, request: Request) -> Response:  # type: ignore[override]
        return materialize_response(self.stream(request), request)

    def stream(self, request: Request) -> Iterator[Any]:  # type: ignore[override]
        events: queue.SimpleQueue[Any] = queue.SimpleQueue()
        asyncio.run_coroutine_threadsafe(
            self._call(request, _driven_key(conversation_key(self.model)), events), _OWNER.loop()
        )
        while True:
            item = events.get()
            if item is _END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    def close(self) -> None:
        """Nothing to release per engine."""


def _check_media(request: Request) -> None:
    """Every inline image / document of the request within the native ceilings.

    Raises:
        NativeAttachmentTooLargeError: One attachment, or all of them together, too large.
    """
    from dspy.lm15 import DocumentPart, ImagePart  # noqa: PLC0415

    from clio_agent.providers.native_attachment_bounds import (  # noqa: PLC0415
        base64_byte_length,
        check_block_bytes,
        check_total_bytes,
    )

    total = 0
    for message in request.messages:
        for part in message.parts:
            for media in (part, *getattr(part, "content", ())):
                data = getattr(media, "data", None)
                if isinstance(media, ImagePart | DocumentPart) and data:
                    size = base64_byte_length(data)
                    if isinstance(media, ImagePart):
                        check_block_bytes("image", size, label=media.media_type)
                    else:
                        check_block_bytes("document", size, label=media.media_type)
                    total += size
    check_total_bytes(total)


def default_wire() -> Any:
    """lm15's Codex LM on clio's sign-in (a fresh token per call), else the CLI login.

    The CLI login is ``$CODEX_HOME/auth.json`` (default ``~/.codex``), resolved by
    :func:`~clio_agent.providers.codex.credentials.codex_cli_auth_path` -- lm15's own
    default path ignores ``CODEX_HOME``, so the path is always passed explicitly.
    """
    from dspy.lm15 import OpenAICodexLM  # noqa: PLC0415

    from clio_agent.providers.codex.credentials import (  # noqa: PLC0415
        CodexCredentialStore,
        codex_cli_auth_path,
    )

    store = CodexCredentialStore()
    credential = store.load()
    if credential is None:
        return OpenAICodexLM.from_codex_cli(
            auth_path=codex_cli_auth_path(), originator=c.ORIGINATOR
        )
    return OpenAICodexLM(
        api_key=lambda: store.get_valid_credential().access_token,
        account_id=credential.account_id,
        originator=c.ORIGINATOR,
    )


def close_all() -> int:
    """Close every kept socket (server shutdown); returns how many were open."""
    with _CONVERSATIONS_LOCK:
        conversations = list(_CONVERSATIONS.values())
        _CONVERSATIONS.clear()
    if conversations:
        loop = _OWNER.loop()
        for conversation in conversations:
            asyncio.run_coroutine_threadsafe(conversation.socket.close(), loop).result(timeout=5)
    return len(conversations)


def _plan(
    key: tuple[str, ...] | None, request: Request, prepared: PreparedRequest
) -> tuple[_Conversation | None, int | None, str | None]:
    """``(kept conversation, first new item, None)`` to continue, or ``(None, None, why)``."""
    if key is None:
        return None, None, None
    with _CONVERSATIONS_LOCK:
        live = _CONVERSATIONS.pop(key, None)
        pending = _RESETS.pop(key, None)
    if pending is not None:
        if live is not None:
            _close_soon(live.socket)
        return None, None, pending
    if live is None:
        return None, None, "first_call"
    if live.expired(time.monotonic()):
        _close_soon(live.socket)
        return None, None, "session_evicted"
    first_new = continuation(live, request, prepared)
    if first_new is None:
        _close_soon(live.socket)
        return None, None, "prefix_mismatch"
    live.used_at = time.monotonic()
    return live, first_new, None


# Which conversations each per-forward stateful scope drove, and the typed reason the
# next call of a conversation an ARC op reset must report.
_FORWARDS: dict[str, set[tuple[str, ...]]] = {}
_RESETS: dict[tuple[str, ...], str] = {}


def _driven_key(key: tuple[str, ...] | None) -> tuple[str, ...] | None:
    """Note that the active forward drives ``key`` (on the caller's thread: the owner
    loop does not see the forward's context)."""
    forward = active_stateful_scope()
    if key is not None and forward is not None:
        with _CONVERSATIONS_LOCK:
            _FORWARDS.setdefault(forward, set()).add(key)
    return key


class _ScopeParticipant:
    """An ARC op on a forward resets every conversation that forward drove."""

    def mark_reset(self, scope_token: str, reason: str = "ops_reset") -> None:
        stateful_reset_payload(reason)  # a typo is a ValueError, never a silent reason
        with _CONVERSATIONS_LOCK:
            keys = set(_FORWARDS.get(scope_token, set()))
            dropped = [_CONVERSATIONS.pop(key, None) for key in keys]
            for key in keys:
                _RESETS[key] = reason
        loop = _OWNER.loop()
        for conversation in dropped:
            if conversation is not None:
                asyncio.run_coroutine_threadsafe(conversation.socket.close(), loop)

    def release(self, scope_token: str) -> None:
        with _CONVERSATIONS_LOCK:
            _FORWARDS.pop(scope_token, None)


register_scope_registry(_ScopeParticipant())


async def _connect(headers: dict[str, str], key: tuple[str, ...] | None) -> Any:
    session = "::".join(key[:2]) if key is not None else uuid.uuid4().hex
    ws_headers = {
        k: v for k, v in headers.items() if k.lower() not in {"content-type", "openai-beta"}
    }
    ws_headers["OpenAI-Beta"] = c.OPENAI_BETA_WEBSOCKETS
    ws_headers["session-id"] = session
    try:
        return await websockets.connect(
            c.CODEX_WS_URL, additional_headers=ws_headers, open_timeout=c.WS_CONNECT_TIMEOUT_S
        )
    except websockets.InvalidStatus as exc:
        status = exc.response.status_code
        body = bytes(exc.response.body or b"").decode("utf-8", "replace")
        if status == 401:
            raise AuthError(f"Codex rejected the sign-in (HTTP 401): {body[:300]}") from exc
        if is_usage_limit_text(body):
            raise CodexPlanLimitError(body[:500], status_code=status) from exc
        raise ServerError(f"Codex WebSocket handshake failed (HTTP {status})") from exc
    # Typed so DSPy retries them; a lasting failure reaches the user in plain words.
    except TimeoutError as exc:
        raise ProviderTimeoutError(
            f"Codex did not answer the connection within {c.WS_CONNECT_TIMEOUT_S:.0f} s"
        ) from exc
    except OSError as exc:
        raise TransportError(f"Could not connect to Codex: {exc}") from exc


async def _exchange(
    wire: Any, request: Request, socket: Any, frame: dict[str, Any], out: queue.SimpleQueue[Any]
) -> str:
    """Send one ``response.create`` frame; forward parsed events; return the response id.

    Writes the same per-call audit rows as Claude Code (``provider.call_started``,
    the first streamed event, ``provider.call_usage`` with cached input).
    """
    call_id, call_index = uuid.uuid4().hex, _next_call_index()
    emit_call_started(
        call_id=call_id,
        call_index=call_index,
        model=str(frame.get("model") or ""),
        prompt=json.dumps(frame.get("input") or []),
    )
    streamed: list[bool] = []  # set once any event of the reply reached the caller
    try:
        return await _stream(wire, request, socket, frame, out, call_id, call_index, streamed)
    except websockets.ConnectionClosed as exc:
        if streamed:
            reason = f"code {exc.rcvd.code}: {exc.rcvd.reason}" if exc.rcvd else "no close frame"
            raise ServerError(
                f"Codex closed the connection during the reply ({reason}); please try again"
            ) from exc
        raise _ConnectionLost from exc


async def _stream(
    wire: Any,
    request: Request,
    socket: Any,
    frame: dict[str, Any],
    out: queue.SimpleQueue[Any],
    call_id: str,
    call_index: int,
    streamed: list[bool],
) -> str:
    """The body of :func:`_exchange`: send the frame and forward the reply's events."""
    first = True
    usage: Any = None
    await socket.send(json.dumps({"type": "response.create", **frame}))
    async for raw in socket:
        payload = json.loads(raw)
        kind = str(payload.get("type") or "")
        if kind in {"error", "response.failed"} and _error_code(payload) == (
            c.PREVIOUS_RESPONSE_NOT_FOUND_CODE
        ):
            raise _ContinuationLost
        if kind in {"error", "response.failed"}:
            message = _error_message(payload)
            if is_usage_limit_text(message):
                # The account's plan window is exhausted: terminal, never retried.
                raise CodexPlanLimitError(message, code=_error_code(payload))
        for event in wire.parse_stream_events(request, _WireEvent(event=kind, data=raw)):
            if first and event.type == "delta":
                first = False
                emit_raw_event(
                    call_index=call_index,
                    event_index=1,
                    source_channel=str(getattr(event.delta, "type", "")),
                    text="",
                    raw_event_type=kind,
                )
            if event.type == "end":
                usage = event.usage
            streamed[:] = [True]
            out.put(event)
        if kind in _TERMINAL:
            emit_call_usage(
                call_id=call_id,
                call_index=call_index,
                model=str(frame.get("model") or ""),
                usage=_usage_row(usage),
                output_chars=0,
            )
            response = payload.get("response") or {}
            return str(response.get("id") or "")
    raise ServerError("codex direct: the WebSocket closed before the response completed")


_CALL_INDEX_LOCK = threading.Lock()
_CALL_INDEX = 0


def _next_call_index() -> int:
    global _CALL_INDEX  # noqa: PLW0603
    with _CALL_INDEX_LOCK:
        _CALL_INDEX += 1
        return _CALL_INDEX


def _usage_row(usage: Any) -> dict[str, int]:
    """lm15 usage in the audit's ``usage_*`` keys (the Claude Code engine's names)."""
    if usage is None:
        return {}
    fields = {
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cache_read_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_output_tokens": usage.reasoning_tokens,
    }
    return {k: int(v) for k, v in fields.items() if v is not None}


def _with_reasoning_summary(request: Request) -> Request:
    """Ask Codex for its reasoning summary: the model's thinking is shown to the user.

    With no effort set, only the summary is asked for (the backend keeps its own
    default effort) -- through ``extensions``, since lm15's ``Reasoning`` needs one.
    """
    config = request.config
    if config.reasoning is not None:
        if config.reasoning.summary is not None:
            return request
        reasoning = dataclasses.replace(config.reasoning, summary="auto")
        return dataclasses.replace(request, config=dataclasses.replace(config, reasoning=reasoning))
    extensions = dict(config.extensions or {})
    extensions.setdefault("reasoning", {"summary": "auto"})
    return dataclasses.replace(request, config=dataclasses.replace(config, extensions=extensions))


class _ContinuationLost(Exception):
    """The backend no longer holds ``previous_response_id`` (before any event)."""


class _ConnectionLost(Exception):
    """The WebSocket closed before any event of the reply was forwarded."""


def _error_message(payload: dict[str, Any]) -> str:
    error = payload.get("error") or (payload.get("response") or {}).get("error") or {}
    return str(error.get("message") or "") if isinstance(error, dict) else ""


def _error_code(payload: dict[str, Any]) -> str:
    error = payload.get("error") or (payload.get("response") or {}).get("error") or {}
    return str(error.get("code") or "") if isinstance(error, dict) else ""


async def _drop(key: tuple[str, ...]) -> None:
    with _CONVERSATIONS_LOCK:
        live = _CONVERSATIONS.pop(key, None)
    if live is not None:
        await live.socket.close()


def _close_soon(socket: Any) -> None:
    asyncio.get_running_loop().create_task(socket.close())


def _audit(
    key: tuple[str, ...] | None,
    model: str,
    request: Request,
    delta: bool,
    reason: str | None,
    sent_items: int,
) -> None:
    """One ``provider.stateful`` audit row per call inside an agent loop."""
    from clio_agent.runtime.stream_audit import stream_audit, stream_audit_enabled  # noqa: PLC0415

    if key is None or not stream_audit_enabled():
        return
    row: dict[str, Any] = {
        "provider": "codex_direct",
        "transport": "websocket",
        "model": f"codex_direct/{model}",
        "stateful_mode": "delta" if delta else "full",
        "total_messages": len(request.messages),
        "sent_items": sent_items,
        "conversation": "::".join(key[:2]),
    }
    if reason is not None:
        row.update(stateful_reset_payload(reason))
    stream_audit("provider.stateful", **row)
