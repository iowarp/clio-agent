"""Pooled Claude Agent SDK streaming transport for ``claude_code`` (#891).

Owner module for the *streaming* connection half of the ``claude_code`` SDK
transport, carved out of :mod:`clio_agent.providers.claude_code_litellm`
(which kept regrowing — #775 no-accretion). The pre-existing *blocking*
``completion`` path's thread-backed pool (``_SdkSession`` / ``_SdkSessionPool`` /
``_run_sdk``) is a separate concern in the sibling
:mod:`clio_agent.providers.claude_code_sdk_pool`; it is re-exported here (and via
``claude_code_litellm``) for the historical import seams.

**The mechanism (measured, #891).** The pre-#891 fault was a *fresh*
``ClaudeSDKClient`` — a fresh ``claude`` CLI subprocess spawn + connect — on every
LM call (~7% of a turn's wall clock), and ``cache_read == 0`` everywhere. The fix
is two independent facts:

* **Connection reuse**: :class:`ClaudeStreamClientPool` keeps ONE connected
  ``ClaudeSDKClient`` per ``(model, cwd, thinking)``, hosted on a private
  daemon-thread event loop so it survives the per-call ``asyncio.run()`` loops the
  token-liveness driver spins up (``lm.io_logging._clio_streamed_call``). The
  connect is paid once, not per call; a per-loop query lock serialises the whole
  query→receive cycle so concurrent calls never interleave streams, and any
  abnormal end drops the client so a poisoned/half-drained connection is never
  reused.
* **Server-side prefix caching**: with the connection warm, each call sends its
  FULL prompt under a FRESH ``session_id`` — and the provider's content-prefix
  cache still hits on the stable leading bytes. Measured on the live 2-turn probe:
  ``cache_read`` grew 12K→184K across a turn's calls with zero session
  continuation. The ``session_id`` is a per-call conversation boundary
  (compartmentalisation: no cross-call, hence no cross-expert, context bleed),
  not a cache key.

**History (do not rebuild).** #891 first shipped a per-expert session registry
that appended byte-suffix *deltas* onto a stable ``session_id`` behind a strict
prefix-extension gate. On real dspy-rendered ReAct prompts the gate never passed
(the adapter re-renders the trailing instruction footer after the trajectory every
step), so the live audit showed 0 deltas / 22 typed resets — every call already
WAS full-prompt-on-fresh-session, and ``cache_read`` grew anyway (see above). The
delta layer was dead code carrying a stale-context risk surface and was stripped;
connection pooling + server-side prefix caching is the entire mechanism.

A process/env kill-switch (:func:`session_reuse_enabled`, default ON for
``claude_code``) restores the pre-#891 per-call transport byte-for-byte (fresh
client + connect per call). A mid-stream death of the pooled CLI subprocess is a
TYPED transient failure (:data:`TRANSPORT_FAILURE_REASONS`, audited as
``provider.transport_error``) that the LM retry layer re-issues on a fresh
connection — never a silent turn failure (#775 no-silent-fallback).
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import queue
import threading
from collections.abc import AsyncIterator
from typing import Any

from clio_agent.providers.claude_code_cancel import (
    register_sdk_stream,
    unregister_sdk_stream,
)
from clio_agent.providers.claude_code_options import build_sdk_options, thinking_key

# Re-export the sibling blocking-path pool for the historical import seams (tests
# and ``claude_code_litellm`` import these names from this module).
from clio_agent.providers.claude_code_sdk_pool import (
    _SDK_SESSION_POOL,
    _run_sdk,
    _SdkSession,
    _SdkSessionPool,
)
from clio_agent.runtime.stream_audit import stream_audit, stream_audit_enabled

logger = logging.getLogger(__name__)

__all__ = [
    "TRANSPORT_FAILURE_REASONS",
    "TRANSIENT_TRANSPORT_MARKER",
    "ClaudeStreamClientPool",
    "session_reuse_enabled",
    "transient_transport_error_message",
    "transient_transport_error_types",
    "stream_scope_for",
    "transport_failure_payload",
    "_streaming_chunk",
    # blocking-path pool (re-exported from claude_code_sdk_pool for the seams)
    "_SdkSession",
    "_SdkSessionPool",
    "_SDK_SESSION_POOL",
    "_run_sdk",
    "_STREAM_CLIENT_POOL",
    "_per_call_message_source",
    "_reset_sessions_for_tests",
]

# --------------------------------------------------------------------------- #
# Typed transport-failure reason catalog (no silent divergence — #775 ground
# rule). Same shape/discipline as providers.resolver.HANDSHAKE_FALLBACK_REASONS
# and gact.streaming._stream_fallback_payload: a connection drop is queryable
# structured data, never an invisible re-key.
# --------------------------------------------------------------------------- #
TRANSPORT_FAILURE_REASONS: dict[str, dict[str, Any]] = {
    "send_failed": {
        "category": "session_transport_error",
        "description": (
            "A query on the pooled Claude CLI connection failed mid-flight (the "
            "subprocess died or the stream broke). The poisoned client is dropped and "
            "the failure surfaces as a typed transient error so the LM retry layer "
            "re-issues the call on a fresh connection."
        ),
    },
}


def _streaming_chunk(
    *,
    text: str,
    is_finished: bool,
    finish_reason: str | None = None,
    usage_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a LiteLLM-compatible streaming chunk (streaming-transport helper)."""
    usage: dict[str, int] | None = None
    if usage_payload is not None:
        prompt_tokens = int(usage_payload.get("input_tokens", 0) or 0)
        prompt_tokens += int(usage_payload.get("cache_creation_input_tokens", 0) or 0)
        prompt_tokens += int(usage_payload.get("cache_read_input_tokens", 0) or 0)
        completion_tokens = int(usage_payload.get("output_tokens", 0) or 0)
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    return {
        "text": text,
        "is_finished": is_finished,
        "finish_reason": finish_reason or ("stop" if is_finished else None),
        "index": 0,
        "tool_use": None,
        "usage": usage,
    }


def transport_failure_payload(reason: str, message: str = "") -> dict[str, Any]:
    """Build a structured transport-failure reason payload (catalog style).

    Mirrors :func:`clio_agent.gact.streaming._stream_fallback_payload`: looks
    ``reason`` up in :data:`TRANSPORT_FAILURE_REASONS`, copies its audited
    metadata, and appends an optional free-text ``message``. Raises ``ValueError``
    on an unknown reason so a typo cannot silently produce an empty reason.
    """
    definition = TRANSPORT_FAILURE_REASONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown transport failure reason: {reason}")
    payload: dict[str, Any] = {"reason": reason, **definition}
    if message:
        payload["message"] = message
    return payload


# --------------------------------------------------------------------------- #
# Mid-stream transport-death translation (#891 live-crash fix).
#
# The persistent pooled ``claude`` CLI subprocess can die mid-conversation (exit
# 1 with no structured ``is_error`` result — observed live at call 11 of a turn,
# after 10 clean cache-reading calls). The SDK surfaces that as a
# ``ClaudeSDKError`` (``ProcessError`` / ``MessageParseError`` / connection drop),
# which the LM retry classifier (``lm.io_logging._TRANSIENT_PROVIDER_MARKERS``)
# did NOT recognise — so a genuinely transient infra fault failed the whole turn
# instead of re-issuing on a fresh connection (as the per-call transport did).
# The pooled entry already drops the poisoned client on the abnormal end, so a
# re-issue reconnects cleanly; this makes the fault LOUD (audited) and typed as
# transient so the existing retry layer heals it.
# --------------------------------------------------------------------------- #
TRANSIENT_TRANSPORT_MARKER = "claude agent sdk transport failed mid-stream"


def transient_transport_error_types() -> tuple[type[BaseException], ...]:
    """SDK transport/process-death exception types (empty tuple if unavailable).

    Returns the ``ClaudeSDKError`` base (covers ``ProcessError``,
    ``CLIConnectionError``, ``MessageParseError``, ``CLIJSONDecodeError``) or an
    empty tuple when the SDK is absent / a test stub omits it — ``except ():``
    then matches nothing, leaving stubbed paths untouched.
    """
    try:
        from claude_agent_sdk import ClaudeSDKError  # noqa: PLC0415

        return (ClaudeSDKError,)
    except Exception:  # noqa: BLE001 - SDK missing / older / test fake module
        return ()


def transient_transport_error_message(model: str, exc: BaseException, *, call_index: int) -> str:
    """Audit a mid-stream SDK transport death and return the transient message.

    Emits a structured, queryable ``provider.transport_error`` row carrying the
    typed :data:`TRANSPORT_FAILURE_REASONS` payload (no silent fallback) and
    returns a message carrying :data:`TRANSIENT_TRANSPORT_MARKER` so
    ``lm.io_logging`` classifies it transient and re-issues the call on a fresh
    pooled connection.
    """
    if stream_audit_enabled():
        stream_audit(
            "provider.transport_error",
            provider="claude_code_sdk",
            call_index=call_index,
            transport="sdk",
            model=model,
            **transport_failure_payload("send_failed", str(exc)[:300]),
        )
    return f"{TRANSIENT_TRANSPORT_MARKER} (model={model}): {str(exc)[:300]}"


# --------------------------------------------------------------------------- #
# Kill-switch.
# --------------------------------------------------------------------------- #
def session_reuse_enabled() -> bool:
    """Whether the pooled-connection SDK transport is on (default ON for claude_code).

    Resolved via ``providers.claude_code.session_reuse`` /
    ``CLIO_CLAUDE_CODE_SESSION_REUSE`` (file → env → default True). Set it false to
    restore the byte-identical pre-#891 per-call behaviour (a fresh client +
    connect + disconnect on every call). Either way each call sends its full
    prompt under a fresh ``session_id``.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return bool(
        conf.resolve(
            "providers.claude_code.session_reuse",
            env="CLIO_CLAUDE_CODE_SESSION_REUSE",
            default=True,
            cast=conf.as_bool,
        )
    )


# --------------------------------------------------------------------------- #
# Streaming client pool: ONE connected client per (model, cwd, thinking), hosted
# on a dedicated loop-thread so it survives the per-call ``asyncio.run()`` loops
# the token-liveness driver spins up (lm.io_logging._clio_streamed_call).
# --------------------------------------------------------------------------- #
_STREAM_END = object()  # queue sentinel: the pump's message stream is exhausted


def _active_gact_session_id() -> str:
    """The GACT session that owns the current LM call, or ``""`` off-turn (#993).

    Read from the turn/executor context var the GACT machinery binds around every
    expert/child forward (the same seam ``lm_activity`` uses to attribute an in-flight
    call). Deferred import: ``clio_agent.gact`` transitively imports the providers, so a
    module-level import would cycle; by the time a real stream runs during a turn, ``gact``
    is already loaded and this is a cheap ``sys.modules`` hit. Kill-on-cancel binds the
    in-flight stream to this id so cancelling the child terminates ONLY its subprocess.
    """

    try:
        from clio_agent.gact.context import active_session_id  # noqa: PLC0415

        return active_session_id() or ""
    except Exception:  # noqa: BLE001 - context unavailable off-turn -> not cancellable
        return ""


class _StreamClientEntry:
    """One pooled ``ClaudeSDKClient`` for a ``(model, cwd, thinking)`` key.

    The real SDK client spawns its CLI subprocess + anyio reader tasks on the loop
    that connects it, so a client connected under one ``asyncio.run()`` is dead once
    that loop closes. The live expert path drives EVERY LM call under its own
    ``asyncio.run()`` (token liveness), so a client cached without loop affinity
    would fail from the second call on and silently fall back to per-call transport.
    This entry therefore owns a private daemon-thread event loop (like the
    non-streaming :class:`_SdkSession`): all SDK I/O runs there, and :meth:`stream`
    bridges each message to the *caller's* loop over a thread-safe queue. The
    per-loop :attr:`_query_lock` serialises the whole query→receive cycle so
    concurrent experts sharing the connection never interleave streams; on any
    abnormal end the client is dropped so a mid-cycle connection never serves the
    next (possibly different-expert) call its leftover response.
    """

    def __init__(self, options_factory: Any) -> None:
        self._options_factory = options_factory
        self._lock = threading.Lock()  # guards loop/thread construction
        self._loop: Any = None
        self._thread: threading.Thread | None = None
        self._client: Any = None
        self._connect_lock = asyncio.Lock()  # owner-loop bound (lazy, first await)
        self._query_lock = asyncio.Lock()  # owner-loop bound (lazy, first await)

    def _ensure_loop(self) -> None:
        with self._lock:
            if self._loop is not None:
                return
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever, name="claude-stream-loop", daemon=True
            )
            thread.start()
            self._loop, self._thread = loop, thread

    async def _ensure_client(self, on_construct: Any) -> Any:
        """Connect the client once (runs on the owner loop, double-checked)."""
        if self._client is not None:
            return self._client
        async with self._connect_lock:
            if self._client is not None:
                return self._client
            from claude_agent_sdk import ClaudeSDKClient  # noqa: PLC0415

            client = ClaudeSDKClient(options=self._options_factory())
            on_construct()
            await client.connect()
            self._client = client
            return client

    async def _areset_client(self) -> None:
        """Disconnect + drop the client (owner loop) so the next call reconnects."""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 - best-effort teardown; never block the caller
            logger.warning("claude stream client disconnect failed", exc_info=True)

    def _abort_active_query(self) -> None:
        """Kill the currently-streaming query by resetting this entry's client on its
        owner loop (#993 kill-on-cancel).

        Scheduled cross-thread from the cancel path: disconnecting the pooled
        ``ClaudeSDKClient`` terminates its ``claude`` CLI subprocess, which ends the
        in-flight ``receive_response()`` and stops the late-op flood. Only THIS entry's
        connection is dropped (the next same-key caller reconnects on demand); other
        pooled entries and other sessions' streams are untouched. Best-effort — a closed
        owner loop (teardown) or a scheduling fault is swallowed, never raised into cancel.
        """

        loop = self._loop
        if loop is None:
            return
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(self._areset_client(), loop)

    async def stream(
        self, *, payload: str, session_id: str, timeout: float | None, on_construct: Any
    ) -> AsyncIterator[Any]:
        """Yield SDK messages for one ``query`` on the pooled client (cross-loop).

        The query→receive cycle runs on this entry's owner loop; each message is
        bridged to the caller's loop via a thread-safe queue. A timeout / transport
        error / caller-abandon drops the pooled client (no cross-call bleed).
        """
        self._ensure_loop()
        caller_loop = asyncio.get_running_loop()
        chunks: queue.SimpleQueue[tuple[Any, Any]] = queue.SimpleQueue()
        # Kill-on-cancel binding (#993): capture the GACT session that owns this call in
        # the CALLER's context (the executor where the turn's session contextvar is set) —
        # the owner-loop pump below has no access to it. The stream is registered as
        # cancellable only WHILE it holds the query and is actively generating, so
        # cancelling a session never kills a same-key sibling that is merely queued behind
        # the per-loop query lock (that sibling has not registered yet).
        gact_sid = _active_gact_session_id()

        async def _pump() -> None:
            clean = False
            handle = None
            try:
                async with asyncio.timeout(timeout):
                    client = await self._ensure_client(on_construct)
                    async with self._query_lock:
                        handle = register_sdk_stream(gact_sid, self._abort_active_query)
                        await client.query(payload, session_id=session_id)
                        async for msg in client.receive_response():
                            chunks.put(("msg", msg))
                clean = True
            except BaseException as exc:  # noqa: BLE001 - surfaced onto the caller loop
                chunks.put(("exc", exc))
            finally:
                if handle is not None:
                    unregister_sdk_stream(handle)
                if not clean:
                    # Mid-cycle end (timeout/error/cancel/kill-on-cancel): drop the
                    # connection so its leftover response can never bleed into the next
                    # call. Idempotent with a kill-on-cancel reset that already ran.
                    await self._areset_client()
                chunks.put((_STREAM_END, None))

        fut = asyncio.run_coroutine_threadsafe(_pump(), self._loop)
        try:
            while True:
                kind, val = await caller_loop.run_in_executor(None, chunks.get)
                if kind is _STREAM_END:
                    break
                if kind == "exc":
                    raise val
                yield val
        finally:
            if not fut.done():
                # Caller abandoned mid-stream: reset the client on the owner loop so
                # a half-drained connection is never reused.
                with contextlib.suppress(Exception):
                    asyncio.run_coroutine_threadsafe(self._areset_client(), self._loop)

    def close_blocking(self) -> None:
        """Disconnect the client and stop the loop-thread (atexit / test reset)."""
        with self._lock:
            loop, self._loop, self._thread = self._loop, None, None
        if loop is None:
            return
        try:
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(self._areset_client(), loop).result(timeout=15)
        except Exception:  # noqa: BLE001 - teardown must never raise
            logger.warning("claude stream client entry teardown failed", exc_info=True)
        finally:
            loop.call_soon_threadsafe(loop.stop)


class ClaudeStreamClientPool:
    """Process-wide pool of persistent streaming clients (#891, reconciles #715/#818).

    Keyed by ``(model, cwd, thinking)`` so distinct-model/thinking experts each
    hold their own connection while same-key experts share one — the connect is
    paid once, not per call (the ~7% per-call cold-connect the waterfall measured).
    Each call rides ``query()`` with a fresh per-call ``session_id``, so ONE client
    serves every expert on its key; compartmentalisation is the per-call
    conversation boundary, not the client.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str | None, str | None, str], _StreamClientEntry] = {}
        self._guard = threading.Lock()
        self._construct_count = 0

    def entry_for(
        self,
        *,
        model: str,
        cwd: str | None,
        thinking: dict[str, Any] | None,
        scope: str | None = None,
    ) -> _StreamClientEntry:
        """Return (creating if needed) the pooled entry for the transport params.

        ``scope`` is the per-forward stateful scope token and MUST be passed for
        every ENGAGED (delta-capable) send: a delta rides the conversation state of
        the connection it is sent on, so two concurrent expert loops multiplexing
        delta runs over ONE connection cross their conversations — the AGENT-COPPER12
        defect, where a child expert's delta send under its own ``session_id``
        returned the PARENT's continuation (the SDK connection, not the per-call
        ``session_id``, is the real conversation boundary for resumed sends).
        Scope-keyed entries give each stateful loop its own connection; the loop's
        many iterations still amortize the one connect (#891). Non-engaged sends
        (full prompt under a fresh ``session_id``) stay on the shared base entry —
        the per-call boundary IS proven for those.
        """
        key = (model, cwd, thinking_key(thinking), scope or "")
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _StreamClientEntry(
                    lambda: build_sdk_options(model=model, cwd=cwd, stream=True, thinking=thinking)
                )
                self._entries[key] = entry
            return entry

    def release(self, scope: str) -> None:
        """Close and drop every entry keyed to ``scope`` (stateful_scope teardown).

        Called by :func:`clio_agent.providers.stateful_common.stateful_scope` on
        loop end via ``register_scope_registry`` — a loop's connection never
        outlives the loop (#900). Best-effort: a teardown failure is logged by the
        entry, never raised into the forward's ``finally``.
        """
        if not scope:
            return
        with self._guard:
            keys = [key for key in self._entries if key[3] == scope]
            entries = [self._entries.pop(key) for key in keys]
        for entry in entries:
            with contextlib.suppress(Exception):
                entry.close_blocking()

    def mark_reset(self, scope: str, reason: str) -> None:
        """Scope-registry protocol no-op: a prefix reset keeps the connection.

        An ARC-op reset means the NEXT send is a full send under a fresh
        ``session_id`` — safe on the same connection; only scope END closes it.
        """

    def bump_construct(self) -> None:
        """Increment the connect counter (called once per real client connect)."""
        with self._guard:
            self._construct_count += 1

    @property
    def construction_count(self) -> int:
        """How many clients this pool has constructed (connect-reuse assertions)."""
        with self._guard:
            return self._construct_count

    def close_blocking(self) -> None:
        """Disconnect every pooled client + stop its loop-thread (#900: no unreaped
        children). Best-effort — a failure is logged, never raised."""
        with self._guard:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            entry.close_blocking()

    def reset_for_tests(self) -> None:
        """Tear down pooled clients + counter IN PLACE (never rebind the singleton).

        The litellm module binds ``_STREAM_CLIENT_POOL`` by value at import, so test
        isolation must mutate this object rather than replace it — otherwise the
        provider keeps using the pre-reset pool.
        """
        self.close_blocking()
        with self._guard:
            self._construct_count = 0


def stream_scope_for(send: Any) -> str | None:
    """The pool-entry scope for one send: its stateful scope IFF the send is engaged.

    An engaged send is delta-capable, so it must ride its own scope-keyed
    connection (see :meth:`ClaudeStreamClientPool.entry_for`); a non-engaged send
    (full prompt, fresh ``session_id``) shares the base entry.
    """
    if send is None or not getattr(send, "engaged", False):
        return None
    token = getattr(send, "scope_token", None)
    return str(token) if token is not None else None


_STREAM_CLIENT_POOL = ClaudeStreamClientPool()
atexit.register(_STREAM_CLIENT_POOL.close_blocking)

# Scope-end teardown seam: the pool implements the scope-registry protocol
# (``release`` / ``mark_reset``), so a react forward's scope exit closes the
# forward's own stateful connection (see ``entry_for``'s scope keying).
from clio_agent.providers.stateful_common import (
    register_scope_registry as _register_scope,  # noqa: E402
)

_register_scope(_STREAM_CLIENT_POOL)  # type: ignore[arg-type]  # duck-typed scope-registry protocol


def _reset_sessions_for_tests() -> None:
    """Drop all pooled streaming-client state (test isolation).

    Mutates the singleton IN PLACE so importers that bound it by value
    (``claude_code_litellm``) keep pointing at the reset object.
    """
    _STREAM_CLIENT_POOL.reset_for_tests()


async def _per_call_message_source(
    client: Any, *, prompt: str, session_id: str, timeout: float | None
) -> AsyncIterator[Any]:
    """Per-call (kill-switch off) SDK message source.

    A fresh client that connects, queries the FULL prompt under a fresh
    ``session_id``, streams every SDK message, and disconnects — byte-for-byte the
    pre-#891 per-call transport. The whole query→receive cycle is timeout-bounded so
    the caller sees a ``TimeoutError`` exactly as before.
    """
    await client.connect()
    try:
        async with asyncio.timeout(timeout):
            await client.query(prompt, session_id=session_id)
            async for msg in client.receive_response():
                yield msg
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 - best-effort teardown
            logger.warning("claude sdk streaming client disconnect failed", exc_info=True)
