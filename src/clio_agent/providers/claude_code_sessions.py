"""One ``ClaudeSDKClient`` per GACT session for ``claude_code`` (S2 Claude SDK tuning).

Owner module for the ``claude_code`` SDK transport's connection pool. Replaces
the pre-S2 design (one process-wide pool keyed by ``(model, cwd, thinking)``,
plus a SEPARATE per-``forward()``-scope-keyed connection for the #901 stateful
delta transport) with one invariant:

**B1.** The pool key is the GACT session id (the contextvar
:func:`_active_gact_session_id` reads, which ``claude_code_cancel`` already
keys cancellation on) — not a react-loop scope token. Since #948 every tier
is a REAL, distinct GACT session, so session-id keying already isolates every
concurrently-active agent (the old scope-keying's job) while ALSO satisfying
B1: the client survives a whole session's turns, not just one ``forward()``.
No active GACT session (off-turn/test/CLI) falls back to the empty-string key
— never individually cancellable, matching ``register_sdk_stream``'s contract.

**B14.** A cancel calls :meth:`_StreamClientEntry.request_interrupt`, sending
``interrupt()`` on the entry's owner loop. Per the installed 0.2.156 SDK's own
contract, an interrupted turn still yields a normal terminal ``ResultMessage``
and ``receive_response()`` completes cleanly, so the client is never
disconnected — the NEXT turn reuses it warm. The abort handle is registered
for the WHOLE call (before the connect region, not just the query lock): a
call still queued for a slot is abortable via its ``abandon`` event instead.

**B17.** Any ABNORMAL end (a transport error, a timed-out query, or a caller
abandoning the stream) marks the entry ``dead`` (F6b — never reconnected in
place). The next ``entry_for`` for that session evicts it and hands out a
warm entry when one exists, else mints fresh — typed and logged
(``dead_client_replaced``), and the crash message carries the dead client's
stderr tail (:mod:`claude_code_stderr_ring`).

**B2.** A small warm pool of pre-connected idle clients, connected with
"standard options" (no ``system_prompt``/``cwd``/``thinking`` override, the
CLI's own default model). A claim needing only a different MODEL is free
(``set_model``, B13); a claim needing a different ``system_prompt``/``cwd``/
``thinking`` — the common case for a real turn, since those are CLI STARTUP
flags with no live-update control request in this SDK version (verified:
``--system-prompt`` at ``subprocess_cli.py:569-582``, ``cwd=`` at its
``subprocess.Popen`` call; the only mutating control requests this SDK
exposes are ``set_model``/``set_permission_mode``/``interrupt``/
``rewind_files``/``reconnect_mcp_server``) — reconnects once, exactly as a
cold connect would, never worse than no warm pool. See the PR body for the
follow-up this implies (pre-warming with a session's REAL system_prompt needs
a hook earlier than this module owns, in the gact turn/session pipeline).

**History (do not rebuild): #COPPER12 scope-keyed connections.** The prior
design gave every ACTIVE ``stateful_scope()`` its own connection because a
spawned child was not yet a first-class GACT session; #948 made every
declared child a real session, so session-id keying gives the same isolation
without a second bookkeeping layer. The #901 stateful-delta layer is
UNCHANGED and orthogonal: it tracks its own SDK-level conversation
continuation independently of which physical client a query rides on. This
module still tells that layer when a connection dies out from under an
ACTIVE scope (the reap/dead-replace paths) — the one place the two concerns
still touch — so a delta is never shipped to a fresh subprocess with no
memory of the dropped prefix.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import queue
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

from clio_agent.providers.claude_code_cancel import (
    register_sdk_stream,
    unregister_sdk_stream,
)
from clio_agent.providers.claude_code_multimodal import sdk_prompt
from clio_agent.providers.claude_code_options import build_sdk_options, thinking_key
from clio_agent.providers.claude_code_stderr_ring import StderrRing
from clio_agent.providers.claude_code_stream_bounds import (
    await_connect_slot,
    log_config_change_reconnect,
    log_dead_client_replaced,
    max_concurrent_claude_processes,
    reap_idle_session_entry,
    sweep_idle_session_entries,
    warm_pool_size,
)
from clio_agent.runtime.stream_audit import stream_audit, stream_audit_enabled

logger = logging.getLogger(__name__)

__all__ = [
    "TRANSPORT_FAILURE_REASONS",
    "TRANSIENT_TRANSPORT_MARKER",
    "ClaudeStreamClientPool",
    "max_concurrent_claude_processes",
    "transient_transport_error_message",
    "transient_transport_error_types",
    "transport_failure_payload",
    "_streaming_chunk",
    "_STREAM_CLIENT_POOL",
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
    "idle_reaped": {
        "category": "session_idle_reap",
        "description": (
            "A session's pooled connection sat connected but unused past the idle "
            "TTL. Proactively dropped to bound resident claude-sdk-cli subprocess "
            "count; the session's next call reconnects (or claims a warm entry)."
        ),
    },
    "config_change_requires_restart": {
        "category": "session_reconnect",
        "description": (
            "A thinking/system_prompt/cwd change on an existing session's connection "
            "cannot be applied live in this SDK version (no set_effort/set_thinking/"
            "set_cwd control request exists — only set_model and set_permission_mode "
            "mutate a connected client). The client reconnected with the new config."
        ),
    },
    "dead_client_replaced": {
        "category": "session_health",
        "description": (
            "A session's connection ended abnormally (a transport error, a timed-out "
            "query, or the caller abandoning the stream mid-flight). The dead client "
            "was replaced — from the warm pool when one was available, else a fresh "
            "connect — so the session's next call is never handed a poisoned client."
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
# --------------------------------------------------------------------------- #
TRANSIENT_TRANSPORT_MARKER = "claude agent sdk transport failed mid-stream"


def transient_transport_error_types() -> tuple[type[BaseException], ...]:
    """SDK transport/process-death exception types (empty tuple if unavailable)."""
    try:
        from claude_agent_sdk import ClaudeSDKError  # noqa: PLC0415

        return (ClaudeSDKError,)
    except Exception:  # noqa: BLE001 - SDK missing / older / test fake module
        return ()


def transient_transport_error_message(
    model: str, exc: BaseException, *, call_index: int, stderr_tail: str = ""
) -> str:
    """Audit a mid-stream SDK transport death and return the transient message.

    B17: ``stderr_tail`` rides both the audit row and the returned message --
    a crash error carries the CLI's own last words, not just its exception text.
    """
    if stream_audit_enabled():
        stream_audit(
            "provider.transport_error",
            provider="claude_code_sdk",
            call_index=call_index,
            transport="sdk",
            model=model,
            stderr_tail=stderr_tail,
            **transport_failure_payload("send_failed", str(exc)[:300]),
        )
    message = f"{TRANSIENT_TRANSPORT_MARKER} (model={model}): {str(exc)[:300]}"
    if stderr_tail:
        message = f"{message}\nclaude CLI stderr:\n{stderr_tail}"
    return message


def _active_gact_session_id() -> str:
    """The GACT session that owns the current LM call, or ``""`` off-turn (#993)."""
    try:
        from clio_agent.gact.context import active_session_id  # noqa: PLC0415

        return active_session_id() or ""
    except Exception:  # noqa: BLE001 - context unavailable off-turn -> not cancellable
        return ""


def _active_stateful_scope() -> str | None:
    """The active per-forward stateful-delta scope token, or ``None`` off the V2 loop.

    Deferred import: the same cross-module boundary :mod:`claude_code_stateful`
    itself documents (this module must never import it at module load — the
    delta layer imports THIS module's sibling, not the other way).
    """
    try:
        from clio_agent.providers.stateful_common import active_stateful_scope  # noqa: PLC0415

        return active_stateful_scope()
    except Exception:  # noqa: BLE001 - never let the delta layer break a plain send
        return None


def _note_scope_provider_error(
    scope: str | None, *, model: str, cwd: str | None, thinking_key_: str | None
) -> None:
    """Flag the stateful-delta registry when a LIVE connection dies out from under it.

    A no-op unless ``scope`` is set (the entry's last call was an ENGAGED,
    delta-capable send) — the common case (a classic ReAct call, or a scope
    that already cleanly released via its own ``stateful_scope()`` teardown)
    has nothing to flag. See this module's docstring for why the two layers
    still touch here.
    """
    if not scope:
        return
    try:
        from clio_agent.providers.claude_code_stateful import stateful_registry  # noqa: PLC0415

        stateful_registry().note_provider_error((scope, model, cwd, thinking_key_), scope)
    except Exception:  # noqa: BLE001 - a bookkeeping miss must never break teardown
        logger.debug("claude_code stateful registry notification failed", exc_info=True)


# --------------------------------------------------------------------------- #
# One pooled ``ClaudeSDKClient`` per GACT session, hosted on a private
# daemon-thread event loop so it survives the per-call ``asyncio.run()`` loops
# the token-liveness driver spins up (lm.io_logging._clio_streamed_call).
# --------------------------------------------------------------------------- #
_STREAM_END = object()  # queue sentinel: the pump's message stream is exhausted


class _StreamClientEntry:
    """One pooled ``ClaudeSDKClient``, bound to a GACT session id (or unclaimed/warm).

    Configuration (``model``/``cwd``/``thinking``/``system_prompt``) is resolved
    lazily, per call, by :meth:`_ensure_client`: the FIRST call connects with
    whatever it asks for; a LATER call on an already-connected entry either
    reuses the connection as-is (identical config — the hot path), swaps the
    model live (B13, :meth:`ClaudeSDKClient.set_model`, no reconnect), or
    reconnects once (a ``cwd``/``thinking``/``system_prompt`` change — none of
    which this SDK version can apply to a live connection).
    """

    def __init__(
        self,
        connect_slots: threading.Semaphore | None = None,
        reclaim_idle_slot: Any | None = None,
    ) -> None:
        self._lock = threading.Lock()  # guards loop/thread construction
        self._loop: Any = None
        self._thread: threading.Thread | None = None
        self._client: Any = None
        self._connect_lock = asyncio.Lock()  # owner-loop bound (lazy, first await)
        self._query_lock = asyncio.Lock()  # owner-loop bound (lazy, first await)
        self._connect_slots = connect_slots
        self._reclaim_idle_slot = reclaim_idle_slot
        self._activity_lock = threading.Lock()
        self._in_flight = False
        self._idle_since = time.monotonic()
        # F6b (historical #1305 finding, still load-bearing): once popped by the
        # pool (idle reap, dead-client replacement, or the abnormal-termination
        # backstop), this entry refuses a LATE connect from a caller still
        # holding it from an earlier entry_for() — closing the orphaned-entry
        # window rather than silently resurrecting a connection outside the pool.
        self._dead = False
        self.stderr_ring = StderrRing()
        # Connected configuration (None until the first successful connect).
        self._model: str | None = None
        self._cwd: str | None = None
        self._thinking: dict[str, Any] | None = None
        self._thinking_key: str | None = None
        self._system_prompt: str | None = None
        # The stateful-delta scope active on the MOST RECENT call, if any — see
        # ``_note_scope_provider_error``.
        self._last_scope: str | None = None

    @property
    def dead(self) -> bool:
        return self._dead

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

    def _mark_busy(self) -> None:
        with self._activity_lock:
            self._in_flight = True

    def _mark_idle(self) -> None:
        with self._activity_lock:
            self._in_flight = False
            self._idle_since = time.monotonic()

    def idle_for(self) -> float | None:
        """Seconds since the last call finished (``None`` while one is in flight)."""
        with self._activity_lock:
            if self._in_flight:
                return None
            return time.monotonic() - self._idle_since

    async def _acquire_connect_slot(
        self, *, gact_session_id: str = "", abandon: "threading.Event | None" = None
    ) -> bool:
        if self._connect_slots is None:
            return True
        return await await_connect_slot(
            self._connect_slots,
            session_id=gact_session_id,
            reclaim_idle_slot=self._reclaim_idle_slot,
            abandon=abandon,
        )

    async def _connect_locked(
        self,
        on_construct: Any,
        *,
        gact_session_id: str,
        timeout: float | None,
        abandon: "threading.Event | None",
        model: str | None,
        cwd: str | None,
        thinking: dict[str, Any] | None,
        thinking_key_: str | None,
        system_prompt: str | None,
    ) -> None:
        """Connect a FRESH client (no prior one held). Caller holds ``_connect_lock``."""
        from claude_agent_sdk import ClaudeSDKClient  # noqa: PLC0415

        acquired = await self._acquire_connect_slot(
            gact_session_id=gact_session_id, abandon=abandon
        )
        if not acquired:
            from clio_agent.providers.claude_code_lifecycle import (  # noqa: PLC0415
                StreamAbandonedError,
            )

            raise StreamAbandonedError
        self.stderr_ring.clear()  # a fresh subprocess starts with an empty ring
        try:
            async with asyncio.timeout(timeout):
                # ``model=None`` (the warm pool's "standard options" connect)
                # omits the field entirely so the CLI's own default governs.
                options = build_sdk_options(
                    model=model,
                    cwd=cwd,
                    stream=True,
                    thinking=thinking,
                    system_prompt=system_prompt,
                    stderr=self.stderr_ring.append,
                )
                client = ClaudeSDKClient(options=options)
                on_construct()
                await client.connect()
        except BaseException:
            if self._connect_slots is not None:
                self._connect_slots.release()
            raise
        self._client = client
        self._model, self._cwd = model, cwd
        self._thinking, self._thinking_key = thinking, thinking_key_
        self._system_prompt = system_prompt

    async def _reconnect_locked(
        self,
        on_construct: Any,
        *,
        gact_session_id: str,
        timeout: float | None,
        abandon: "threading.Event | None",
        model: str | None,
        cwd: str | None,
        thinking: dict[str, Any] | None,
        thinking_key_: str | None,
        system_prompt: str | None,
    ) -> None:
        """Drop the current client and connect a new one with the new config (B13/B4)."""
        changed = [
            name
            for name, is_changed in (
                ("thinking", thinking_key_ != self._thinking_key),
                ("system_prompt", system_prompt != self._system_prompt),
                ("cwd", cwd != self._cwd),
            )
            if is_changed
        ]
        await self._areset_client()
        await self._connect_locked(
            on_construct,
            gact_session_id=gact_session_id,
            timeout=timeout,
            abandon=abandon,
            model=model,
            cwd=cwd,
            thinking=thinking,
            thinking_key_=thinking_key_,
            system_prompt=system_prompt,
        )
        log_config_change_reconnect(model, changed)

    async def _ensure_client(
        self,
        on_construct: Any,
        *,
        gact_session_id: str = "",
        timeout: float | None = None,
        abandon: "threading.Event | None" = None,
        model: str | None = None,
        cwd: str | None = None,
        thinking: dict[str, Any] | None = None,
        system_prompt: str | None = None,
    ) -> Any:
        """Return a connected, correctly-configured client (connect/reconcile once).

        F6b: refuses (typed, retryable) when :attr:`_dead` — a lifecycle
        release popped this entry while a caller already held it from an
        earlier ``entry_for()``.
        """
        if self._dead:
            from clio_agent.providers.claude_code_lifecycle import (  # noqa: PLC0415
                dead_entry_error_message,
            )

            raise RuntimeError(dead_entry_error_message())
        thinking_key_ = thinking_key(thinking)
        system_prompt = system_prompt or None
        # Fast path: already connected and configured identically — every call
        # after the first on a settled session/model pays nothing here.
        if (
            self._client is not None
            and self._thinking_key == thinking_key_
            and self._system_prompt == system_prompt
            and self._cwd == cwd
            and self._model == model
        ):
            return self._client
        async with self._connect_lock:
            if self._dead:
                from clio_agent.providers.claude_code_lifecycle import (  # noqa: PLC0415
                    dead_entry_error_message,
                )

                raise RuntimeError(dead_entry_error_message())
            if self._client is None:
                await self._connect_locked(
                    on_construct,
                    gact_session_id=gact_session_id,
                    timeout=timeout,
                    abandon=abandon,
                    model=model,
                    cwd=cwd,
                    thinking=thinking,
                    thinking_key_=thinking_key_,
                    system_prompt=system_prompt,
                )
            elif (
                self._thinking_key != thinking_key_
                or self._system_prompt != system_prompt
                or self._cwd != cwd
            ):
                await self._reconnect_locked(
                    on_construct,
                    gact_session_id=gact_session_id,
                    timeout=timeout,
                    abandon=abandon,
                    model=model,
                    cwd=cwd,
                    thinking=thinking,
                    thinking_key_=thinking_key_,
                    system_prompt=system_prompt,
                )
            elif model and self._model != model:
                # B13: a model-only change on an existing, otherwise-identical
                # connection is a live control request — never a reconnect.
                await self._client.set_model(model)
                self._model = model
            return self._client

    async def _areset_client(self) -> None:
        """Disconnect + drop the client (owner loop) so the next call reconnects."""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 - best-effort teardown; never block the caller
            logger.warning("claude stream client disconnect failed", exc_info=True)
        finally:
            if self._connect_slots is not None:
                self._connect_slots.release()

    async def _ainterrupt(self) -> None:
        """B14: send the SDK interrupt control request (never disconnects)."""
        client = self._client
        if client is None:
            return
        try:
            await client.interrupt()
        except Exception:  # noqa: BLE001 - a failed interrupt must never raise into the cancel path
            logger.warning("claude stream client interrupt failed", exc_info=True)

    async def _mark_dead_and_reset(self) -> None:
        """B17: burn this entry (F6b) and disconnect — the pool replaces it next use."""
        self._dead = True
        _note_scope_provider_error(
            self._last_scope,
            model=self._model or "",
            cwd=self._cwd,
            thinking_key_=self._thinking_key,
        )
        await self._areset_client()

    def request_interrupt(self, abandon: "threading.Event") -> None:
        """The cancel-registry abort callback for one call (B14, cross-thread, sync).

        Interrupts a LIVE connected client on its owner loop; if this entry has
        no client yet (still queued for a connect slot, or never reached
        ``client.query()``), sets ``abandon`` instead so the queued wait gives
        up cleanly — the same mechanism a caller's own teardown already uses.
        """
        if self._client is not None and self._loop is not None:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._ainterrupt(), self._loop)
        else:
            abandon.set()

    async def stream(
        self,
        *,
        payload: str,
        native_blocks: list[dict[str, Any]],
        session_id: str,
        timeout: float | None,
        on_construct: Any,
        model: str | None = None,
        cwd: str | None = None,
        thinking: dict[str, Any] | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[Any]:
        """Yield SDK messages for one ``query`` on the pooled client (cross-loop)."""
        if self._dead:
            from clio_agent.providers.claude_code_lifecycle import (  # noqa: PLC0415
                dead_entry_error_message,
            )

            raise RuntimeError(dead_entry_error_message())
        self._ensure_loop()
        self._mark_busy()
        caller_loop = asyncio.get_running_loop()
        chunks: queue.SimpleQueue[tuple[Any, Any]] = queue.SimpleQueue()
        # Captured in the CALLER's context — the owner-loop pump has no access
        # to either contextvar once scheduled cross-thread (#993 / #901).
        gact_sid = _active_gact_session_id()
        self._last_scope = _active_stateful_scope()
        abandon = threading.Event()
        # B14: registered for the WHOLE call (connect-slot wait, config
        # reconciliation, AND the query itself) — not just while the query
        # lock is held — so a cancel arriving before this call ever sends a
        # query still stops it (via `abandon`) instead of racing in unseen.
        handle = register_sdk_stream(gact_sid, lambda: self.request_interrupt(abandon))

        async def _pump() -> None:
            clean = False
            try:
                client = await self._ensure_client(
                    on_construct,
                    gact_session_id=gact_sid,
                    timeout=timeout,
                    abandon=abandon,
                    model=model,
                    cwd=cwd,
                    thinking=thinking,
                    system_prompt=system_prompt,
                )
                async with asyncio.timeout(timeout):
                    async with self._query_lock:
                        query_input: Any = (
                            sdk_prompt(payload, native_blocks) if native_blocks else payload
                        )
                        await client.query(query_input, session_id=session_id)
                        async for msg in client.receive_response():
                            chunks.put(("msg", msg))
                clean = True
            except BaseException as exc:  # noqa: BLE001 - surfaced onto the caller loop
                chunks.put(("exc", exc))
            finally:
                unregister_sdk_stream(handle)
                # END queued BEFORE any reset below — the caller blocks in an
                # UNBOUNDED chunks.get() on a worker thread; a cross-thread
                # lifecycle release stopping this owner loop out from under the
                # reset's await must never strand that worker (#1305 F2).
                chunks.put((_STREAM_END, None))
                if not clean:
                    await self._mark_dead_and_reset()

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
                # Never cancel `fut` outright — it could land inside `_pump`'s
                # own in-progress teardown await, interrupting a disconnect
                # mid-flight (slot released, CLI never actually gone). Only
                # ask the queue wait to give up.
                abandon.set()
                with contextlib.suppress(Exception):
                    asyncio.run_coroutine_threadsafe(self._mark_dead_and_reset(), self._loop)
            self._mark_idle()

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

    def close_nonblocking(self) -> None:
        """Disconnect the client and stop the loop-thread WITHOUT blocking the caller."""
        with self._lock:
            loop, self._loop, self._thread = self._loop, None, None
        if loop is None or not loop.is_running():
            return
        fut = asyncio.run_coroutine_threadsafe(self._areset_client(), loop)
        fut.add_done_callback(lambda _f: loop.call_soon_threadsafe(loop.stop))


class ClaudeStreamClientPool:
    """Process-wide pool of persistent streaming clients, keyed by GACT session id.

    Plus a small warm sub-pool of pre-connected, unclaimed entries (B2) handed
    out to a session's FIRST ``entry_for`` — see the module docstring for the
    warm pool's actual TTFT contract given this SDK's connect-time-only
    ``system_prompt``/``cwd``.
    """

    def __init__(self, *, max_concurrent: int | None = None, warm_size: int | None = None) -> None:
        self._entries: dict[str, _StreamClientEntry] = {}
        self._warm: list[_StreamClientEntry] = []
        self._guard = threading.Lock()
        self._construct_count = 0
        n = max_concurrent if max_concurrent is not None else max_concurrent_claude_processes()
        self._connect_slots = threading.Semaphore(n)
        self._warm_size = warm_size if warm_size is not None else warm_pool_size()

    def entry_for(self, *, session_id: str, gact_session_id: str = "") -> _StreamClientEntry:
        """Return (creating/claiming/replacing as needed) the entry for ``session_id``.

        ``session_id`` IS the pool key (B1) — pass the GACT session id; an
        empty string is the shared off-turn fallback. ``gact_session_id`` is
        accepted for symmetry with the connect-wait surfacing's liveness feed
        and defaults to ``session_id`` when omitted.
        """
        key = session_id or ""
        for evicted_key, evicted_entry in sweep_idle_session_entries(self):
            reap_idle_session_entry(evicted_key, evicted_entry)
        replaced_dead_entry = False
        replaced_from_warm = False
        with self._guard:
            entry = self._entries.get(key)
            if entry is not None and entry.dead:
                del self._entries[key]
                entry = None
                replaced_dead_entry = True
            if entry is None:
                entry = self._warm.pop() if self._warm else None
                replaced_from_warm = entry is not None
                if entry is None:
                    entry = _StreamClientEntry(
                        connect_slots=self._connect_slots,
                        reclaim_idle_slot=self._reclaim_idle_for_slot,
                    )
                self._entries[key] = entry
        if replaced_from_warm:
            self._spawn_warm_refill()
        if replaced_dead_entry:
            log_dead_client_replaced(key, from_warm_pool=replaced_from_warm)
        return entry

    def _reclaim_idle_for_slot(self) -> int:
        """Reap idle session entries when a connect is queued behind the cap."""
        evicted = sweep_idle_session_entries(self, ttl_s=0.0)
        for key, entry in evicted:
            reap_idle_session_entry(key, entry)
        return len(evicted)

    def release(self, session_id: str) -> None:
        """Close and drop the entry keyed to ``session_id`` (session-end teardown).

        BLOCKS (``close_blocking``, up to 15s) — callers must run this off the
        server's own event loop. See :meth:`release_session_resources` for the
        non-blocking abnormal-path backstop.
        """
        if not session_id:
            return
        with self._guard:
            entry = self._entries.pop(session_id, None)
        if entry is not None:
            with contextlib.suppress(Exception):
                entry.close_blocking()

    def release_session_resources(self, session_id: str) -> None:
        """Provider-agnostic per-session release (#1305) — the non-blocking,
        ABNORMAL-termination backstop. Delegates to
        :mod:`clio_agent.providers.claude_code_lifecycle` (runs on the server's
        own event loop; must never block or evict an in-flight entry).
        """
        from clio_agent.providers.claude_code_lifecycle import (  # noqa: PLC0415
            release_session_resources_nonblocking,
        )

        release_session_resources_nonblocking(self, session_id)

    def prewarm(self, n: int | None = None) -> None:
        """Top up the warm pool to ``n`` (default the configured size) in the background."""
        target = self._warm_size if n is None else n
        with self._guard:
            deficit = max(0, target - len(self._warm))
        for _ in range(deficit):
            self._spawn_warm_refill()

    def _spawn_warm_refill(self) -> None:
        threading.Thread(
            target=self._prewarm_one_blocking, daemon=True, name="claude-warm-prewarm"
        ).start()

    def _prewarm_one_blocking(self) -> None:
        entry = _StreamClientEntry(
            connect_slots=self._connect_slots, reclaim_idle_slot=self._reclaim_idle_for_slot
        )
        entry._ensure_loop()  # noqa: SLF001 - this module owns _StreamClientEntry
        try:
            fut = asyncio.run_coroutine_threadsafe(
                entry._ensure_client(  # noqa: SLF001
                    self.bump_construct, model=None, cwd=None, thinking=None, system_prompt=None
                ),
                entry._loop,  # noqa: SLF001
            )
            fut.result(timeout=60.0)
        except Exception:  # noqa: BLE001 - a failed prewarm must never break the caller
            logger.warning("claude_code warm pool prewarm failed", exc_info=True)
            entry.close_nonblocking()
            return
        with self._guard:
            if len(self._warm) < self._warm_size:
                self._warm.append(entry)
                keep = True
            else:
                keep = False
        if not keep:
            entry.close_nonblocking()

    def bump_construct(self) -> None:
        """Increment the connect counter (called once per real client connect)."""
        with self._guard:
            self._construct_count += 1

    @property
    def construction_count(self) -> int:
        with self._guard:
            return self._construct_count

    def close_blocking(self) -> None:
        """Disconnect every pooled + warm client and stop its loop-thread."""
        with self._guard:
            entries = list(self._entries.values()) + self._warm
            self._entries.clear()
            self._warm.clear()
        for entry in entries:
            entry.close_blocking()

    def reset_for_tests(self) -> None:
        """Tear down pooled clients + counter IN PLACE (never rebind the singleton)."""
        self.close_blocking()
        with self._guard:
            self._construct_count = 0


_STREAM_CLIENT_POOL = ClaudeStreamClientPool()
atexit.register(_STREAM_CLIENT_POOL.close_blocking)

# #1305 generic per-subagent lifecycle seam: a child task's terminal status
# releases its own session's connection deterministically (the primary path;
# the idle-TTL sweep is the backstop for anything that races or never runs).
from clio_agent.providers.session_lifecycle import (  # noqa: E402
    register_session_lifecycle_provider as _register_session_lifecycle,
)

_register_session_lifecycle(_STREAM_CLIENT_POOL.release_session_resources)


def _reset_sessions_for_tests() -> None:
    """Drop all pooled streaming-client state (test isolation).

    Mutates the singleton IN PLACE so importers that bound it by value
    (``claude_code_litellm``) keep pointing at the reset object.
    """
    _STREAM_CLIENT_POOL.reset_for_tests()
