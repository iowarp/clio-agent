"""One Copilot runtime per GACT session, hosting the session's model conversations.

Each pooled :class:`CopilotEntry` owns one ``CopilotClient`` (one runtime process) on
a private daemon-thread event loop, so it is independent of whichever loop the caller
runs the LM call on. The client is created with CLIO's GitHub credential and never the
machine's logged-in user (:mod:`.options`). A Copilot *session* is one conversation:

* a **full send** creates a new Copilot session (the previous one is deleted first);
* a **delta send** continues the entry's current session, whose id the conversation
  plan (:mod:`clio_agent.providers.claude_code_conversation`) carries as its handle.

A query yields the session's events until ``session.idle``. A ``session.error`` raises
its typed CLIO error (:func:`.errors.classify_session_error`); any failure burns the
entry, so the next call starts a fresh runtime with a freshly read credential.

Idle entries are reaped after ``providers.copilot.session_idle_ttl_s`` (default 300 s);
a session's terminal status releases its entry through
:mod:`clio_agent.providers.session_lifecycle`.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import threading
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any

from clio_agent.providers.claude_code_cancel import register_sdk_stream, unregister_sdk_stream
from clio_agent.providers.claude_code_clear import side_key
from clio_agent.providers.copilot.auth import github_token, token_provider
from clio_agent.providers.copilot.errors import (
    COPILOT_NOT_INSTALLED_MESSAGE,
    CopilotNotInstalledError,
    CopilotSessionError,
    classify_session_error,
    teardown_errors,
)
from clio_agent.providers.copilot.options import client_kwargs, session_kwargs
from clio_agent.providers.copilot.runtime import runtime_path
from clio_agent.providers.usage.copilot import premium_resets_at, read_copilot_quota

__all__ = [
    "DEAD_ENTRY_MARKER",
    "SESSION_POOL",
    "CopilotEntry",
    "CopilotSessionPool",
    "active_gact_session_id",
    "require_copilot_sdk",
]

logger = logging.getLogger(__name__)

#: Marks the typed error a call gets from an entry the pool already released.
DEAD_ENTRY_MARKER = "copilot session entry was released"
_STREAM_END = object()
_IDLE = "session.idle"
_ERROR = "session.error"


def require_copilot_sdk() -> Any:
    """The ``copilot`` package, or a typed not-installed error (never a bare ImportError)."""
    try:
        import copilot  # noqa: PLC0415 - optional extra
    except ImportError as exc:
        raise CopilotNotInstalledError(COPILOT_NOT_INSTALLED_MESSAGE, detail=str(exc)) from exc
    return copilot


def active_gact_session_id() -> str:
    """The GACT session that owns the current LM call, or ``""`` off-turn."""
    from clio_agent.gact.context import active_session_id  # noqa: PLC0415

    return active_session_id() or ""


def session_idle_ttl_s() -> float:
    """Seconds an entry may sit idle before the reaper closes its runtime."""
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return float(
        conf.resolve(
            "providers.copilot.session_idle_ttl_s",
            env="CLIO_COPILOT_SESSION_IDLE_TTL_S",
            default=300.0,
            cast=conf.as_float,
        )
    )


def event_type(event: Any) -> str:
    """An SDK event's wire type (``assistant.message_delta``, ``session.idle``, ...)."""
    kind = getattr(event, "type", "")
    return str(getattr(kind, "value", kind) or "")


class CopilotEntry:
    """One pooled ``CopilotClient`` and the Copilot session it currently holds."""

    def __init__(self, on_idle: Callable[[], None] | None = None) -> None:
        self._on_idle = on_idle
        self._lock = threading.Lock()
        self._loop: Any = None
        self._thread: threading.Thread | None = None
        self._client: Any = None
        self._token = ""
        self._quota_task: asyncio.Task[None] | None = None
        self._session: Any = None
        self._session_id: str | None = None
        self._query_lock: asyncio.Lock | None = None
        self._activity_lock = threading.Lock()
        self._in_flight = False
        self._idle_since = time.monotonic()
        self._dead = False
        self._model = ""
        #: The conversation the current Copilot session holds, and the typed reason
        #: the next kept call must start over with (``claude_code_conversation``).
        self.conversation: Any = None
        self.pending_reset: str | None = None

    @property
    def dead(self) -> bool:
        return self._dead

    def forget_conversation(self) -> None:
        """The session is gone or going: its conversation goes with it (typed)."""
        if self.conversation is not None:
            self.conversation = None
            self.pending_reset = "session_evicted"

    def idle_for(self) -> float | None:
        """Seconds since the last call finished (``None`` while one is in flight)."""
        with self._activity_lock:
            return None if self._in_flight else time.monotonic() - self._idle_since

    def _mark(self, busy: bool) -> None:
        with self._activity_lock:
            self._in_flight = busy
            if not busy:
                self._idle_since = time.monotonic()
        if not busy and self._on_idle is not None:
            self._on_idle()

    def _ensure_loop(self) -> None:
        with self._lock:
            if self._loop is not None:
                return
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, name="copilot-loop", daemon=True)
            thread.start()
            self._loop, self._thread = loop, thread

    async def _ensure_client(self, cwd: str, on_construct: Callable[[], None]) -> Any:
        if self._client is not None:
            return self._client
        copilot = require_copilot_sdk()
        self._token = github_token()
        kwargs = client_kwargs(
            github_token=self._token, runtime_path=runtime_path(), working_directory=cwd
        )
        client = copilot.CopilotClient(**kwargs)
        await client.start()
        on_construct()
        self._client = client
        return client

    async def _open_session(self, client: Any, handle: str, settings: dict[str, Any]) -> Any:
        await self._drop_session()
        session = await client.create_session(
            session_id=handle,
            **session_kwargs(
                model=settings["model"],
                system_prompt=settings["system_prompt"],
                working_directory=settings["cwd"],
                on_event=lambda _event: None,
                github_token_provider=token_provider,
                reasoning_effort=settings["reasoning_effort"],
            ),
        )
        self._session, self._session_id, self._model = session, handle, settings["model"]
        return session

    async def _drop_session(self) -> None:
        session, session_id = self._session, self._session_id
        self._session = self._session_id = None
        if session is None:
            return
        try:
            await session.disconnect()
            if self._client is not None and session_id:
                # Copilot keeps a disconnected session on disk; CLIO holds the history.
                await self._client.delete_session(session_id)
        except teardown_errors():
            logger.warning("copilot session teardown failed session=%s", session_id, exc_info=True)

    async def _areset(self) -> None:
        """Drop the session and stop the runtime (owner loop)."""
        self.forget_conversation()
        await self._drop_session()
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.stop()
        except teardown_errors():
            logger.warning("copilot client stop failed; forcing", exc_info=True)
            try:
                await client.force_stop()
            except teardown_errors():
                logger.warning("copilot client force-stop failed", exc_info=True)

    async def _aabort(self) -> None:
        session = self._session
        if session is None:
            return
        try:
            await session.abort()
        except teardown_errors():
            logger.warning("copilot session abort failed", exc_info=True)

    def request_interrupt(self, abandon: threading.Event) -> None:
        """Cancel-registry callback: abort the live session, else abandon the queued call."""
        loop = self._loop
        if self._session is not None and loop is not None and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._aabort(), loop)
        else:
            abandon.set()

    async def _relay(
        self,
        session: Any,
        payload: str,
        attachments: list[dict[str, Any]],
        idle_s: float | None,
        put: Callable[[Any], None],
        abandon: threading.Event,
    ) -> None:
        events: asyncio.Queue[Any] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        unsubscribe = session.on(lambda event: loop.call_soon_threadsafe(events.put_nowait, event))
        try:
            await session.send(payload, attachments=attachments or None)
            while True:
                try:
                    event = await asyncio.wait_for(events.get(), idle_s)
                except TimeoutError as exc:
                    raise TimeoutError(
                        f"copilot stream idle for {idle_s:.0f}s without an event"
                    ) from exc
                kind = event_type(event)
                if kind == _ERROR:
                    raise classify_session_error(
                        getattr(event, "data", None), resets_at=premium_resets_at()
                    )
                if kind == _IDLE:
                    return
                put(event)
                if abandon.is_set():
                    return
        finally:
            unsubscribe()

    async def stream(
        self,
        *,
        payload: str,
        attachments: list[dict[str, Any]],
        handle: str,
        fresh: bool,
        model: str,
        system_prompt: str,
        cwd: str,
        reasoning_effort: str | None,
        idle_timeout: float | None,
        on_construct: Callable[[], None],
    ) -> AsyncGenerator[Any, None]:
        """Yield SDK events for one send (cross-loop) until the session goes idle."""
        if self._dead:
            raise RuntimeError(DEAD_ENTRY_MARKER)
        self._ensure_loop()
        self._mark(True)
        from clio_agent.providers.stream_queue import (  # noqa: PLC0415
            AsyncStreamQueue,
            StreamProducer,
        )

        chunks: AsyncStreamQueue[tuple[Any, Any]] = AsyncStreamQueue()
        abandon = threading.Event()
        registration = register_sdk_stream(
            active_gact_session_id(), lambda: self.request_interrupt(abandon)
        )
        settings = {
            "model": model,
            "system_prompt": system_prompt,
            "cwd": cwd,
            "reasoning_effort": reasoning_effort,
        }

        async def _pump() -> None:
            clean = False
            try:
                if self._query_lock is None:
                    self._query_lock = asyncio.Lock()
                async with self._query_lock:
                    client = await self._ensure_client(cwd, on_construct)
                    if abandon.is_set():
                        clean = True
                        return
                    if fresh or self._session is None:
                        if not fresh:
                            raise CopilotSessionError(
                                "GitHub Copilot lost the conversation it was continuing",
                                detail=f"session={handle}",
                            )
                        session = await self._open_session(client, handle, settings)
                    elif self._session_id != handle:
                        raise CopilotSessionError(
                            "GitHub Copilot holds a different conversation",
                            detail=f"held={self._session_id} asked={handle}",
                        )
                    else:
                        session = self._session
                    await self._relay(
                        session,
                        payload,
                        attachments,
                        idle_timeout,
                        lambda event: chunks.put(("event", event)),
                        abandon,
                    )
                clean = True
                # Read after the call, off the caller's path (at most once per interval).
                self._quota_task = asyncio.get_running_loop().create_task(
                    read_copilot_quota(client, self._token)
                )
            except BaseException as exc:  # noqa: BLE001 - forwards: the caller loop re-raises it
                chunks.put(("exc", exc))
            finally:
                unregister_sdk_stream(registration)
                chunks.put((_STREAM_END, None))
                if not clean:
                    self._dead = True
                    await self._areset()

        producer = StreamProducer(self._loop, _pump)
        kind: Any = None
        try:
            while True:
                kind, value = await producer.receive(chunks.get())
                if kind is _STREAM_END:
                    break
                if kind == "exc":
                    raise value
                yield value
        finally:
            if kind is not _STREAM_END and kind != "exc" and not producer.done():
                # The caller left mid-stream: the session's state is unknown, so the
                # entry is burned and its runtime stopped; the next call starts fresh.
                abandon.set()
                self._dead = True
                self.forget_conversation()
                loop = self._loop
                if loop is not None and not loop.is_closed():
                    asyncio.run_coroutine_threadsafe(self._areset(), loop)
            await producer.retire()
            self._mark(False)

    def close_blocking(self) -> None:
        """Stop the runtime and the loop-thread (atexit / session end / tests)."""
        with self._lock:
            loop, self._loop, self._thread = self._loop, None, None
        if loop is None:
            return
        try:
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(self._areset(), loop).result(timeout=15)
        except teardown_errors():
            logger.warning("copilot entry teardown failed", exc_info=True)
        finally:
            loop.call_soon_threadsafe(loop.stop)

    def close_nonblocking(self) -> None:
        """Stop the runtime and the loop-thread without blocking the caller."""
        with self._lock:
            loop, self._loop, self._thread = self._loop, None, None
        if loop is None or not loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(self._areset(), loop)
        future.add_done_callback(lambda _f: loop.call_soon_threadsafe(loop.stop))


class CopilotSessionPool:
    """Process-wide pool of :class:`CopilotEntry`, keyed by GACT session id."""

    def __init__(self, *, reap_on_timer: bool = True) -> None:
        self._entries: dict[str, CopilotEntry] = {}
        self._guard = threading.Lock()
        self._construct_count = 0
        self._reap_on_timer = reap_on_timer
        self._wake = threading.Event()
        self._stopping = False
        self._reaper: threading.Thread | None = None

    def entry_for(self, session_id: str) -> CopilotEntry:
        """The live entry for ``session_id`` (a dead one is replaced, its reason kept)."""
        self.reap_idle()
        key = session_id or ""
        with self._guard:
            entry = self._entries.get(key)
            if entry is not None and entry.dead:
                reason = entry.pending_reset or "session_evicted"
                entry = None
                logger.info("copilot pool: reason=dead_entry_replaced session=%s", key)
            else:
                reason = None
            if entry is None:
                entry = CopilotEntry(on_idle=self._wake_reaper)
                entry.pending_reset = reason
                self._entries[key] = entry
        self._wake_reaper()
        return entry

    def bump_construct(self) -> None:
        with self._guard:
            self._construct_count += 1

    @property
    def construction_count(self) -> int:
        with self._guard:
            return self._construct_count

    def reap_idle(self, ttl_s: float | None = None) -> list[str]:
        """Close every entry idle for ``ttl_s`` whose session has no turn running."""
        from clio_agent.providers import turn_activity  # noqa: PLC0415

        ttl = session_idle_ttl_s() if ttl_s is None else ttl_s
        with self._guard:
            evicted = [
                (key, entry)
                for key, entry in self._entries.items()
                if (idle := entry.idle_for()) is not None
                and idle >= ttl
                and not turn_activity.active(key)
            ]
            for key, _entry in evicted:
                del self._entries[key]
        for key, entry in evicted:
            entry._dead = True  # noqa: SLF001 - refuse a late send outside the pool
            entry.forget_conversation()
            entry.close_nonblocking()
            logger.info("copilot pool: reason=idle_reaped session=%s ttl_s=%.1f", key, ttl)
        return [key for key, _ in evicted]

    def _wake_reaper(self) -> None:
        if not self._reap_on_timer or self._stopping:
            return
        self._wake.set()
        with self._guard:
            if self._reaper is not None and self._reaper.is_alive():
                return
            self._reaper = threading.Thread(target=self._reap_loop, name="copilot-reaper")
            self._reaper.daemon = True
            self._reaper.start()

    def _reap_loop(self) -> None:
        reap_errors: tuple[type[BaseException], ...] = (*teardown_errors(), ValueError)
        while not self._stopping:
            self._wake.clear()
            try:
                self.reap_idle()
            except reap_errors:
                logger.warning("copilot pool: reason=idle_reap_failed", exc_info=True)
            with self._guard:
                if not self._entries:
                    self._reaper = None
                    return
            self._wake.wait(max(1.0, session_idle_ttl_s() / 2))

    def release(self, session_id: str) -> None:
        """Close the entries of ``session_id`` (blocking; run off the server loop)."""
        if not session_id:
            return
        with self._guard:
            entries = [self._entries.pop(k, None) for k in (session_id, side_key(session_id))]
        for entry in filter(None, entries):
            entry._dead = True  # noqa: SLF001
            entry.forget_conversation()
            entry.close_blocking()

    def release_session_resources(self, session_id: str) -> None:
        """Lifecycle backstop: release idle entries of ``session_id`` without blocking."""
        with self._guard:
            entries = []
            for key in (session_id, side_key(session_id)):
                entry = self._entries.get(key)
                if entry is not None and entry.idle_for() is not None:
                    entries.append(self._entries.pop(key))
        for entry in entries:
            entry._dead = True  # noqa: SLF001
            entry.forget_conversation()
            entry.close_nonblocking()

    def release_all(self) -> int:
        """Close every idle entry (GitHub signed out); returns how many were closed."""
        with self._guard:
            keys = [k for k, e in self._entries.items() if e.idle_for() is not None]
            entries = [self._entries.pop(k) for k in keys]
        for entry in entries:
            entry._dead = True  # noqa: SLF001
            entry.forget_conversation()
            entry.close_nonblocking()
        return len(entries)

    def close_blocking(self) -> None:
        """Stop the reaper, then every runtime (atexit / tests)."""
        self._stopping = True
        self._wake.set()
        with self._guard:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            entry.close_blocking()

    def reset_for_tests(self) -> None:
        """Tear everything down in place (importers keep the same singleton)."""
        self.close_blocking()
        with self._guard:
            self._construct_count = 0
            self._reaper = None
        self._stopping = False


SESSION_POOL = CopilotSessionPool()
atexit.register(SESSION_POOL.close_blocking)

from clio_agent.providers.session_lifecycle import (  # noqa: E402
    register_session_lifecycle_provider as _register_session_lifecycle,
)

_register_session_lifecycle(SESSION_POOL.release_session_resources)
