"""Official Python Codex SDK transport client (S1b restore).

CLIO imports :mod:`openai_codex` and consumes its typed turn stream. The SDK
owns the pinned runtime and JSON-RPC lifecycle; CLIO never shells out to
``codex``, speaks app-server JSON-RPC itself, or falls back to a CLI transport.

**Credentials stay the SDK/runtime's own.** Earlier revisions of this module
copied ``~/.codex/auth.json`` into a private, CLIO-managed temp home and wrote
rotated tokens back. Per the owner ruling that restored this transport, CLIO
must never read or write that file: the client below passes NO ``env``
override to :class:`~openai_codex.CodexConfig`, so the spawned ``codex``
runtime inherits this process's real environment verbatim (the user's own
``CODEX_HOME``, or the SDK's own ``~/.codex`` default when unset) and owns its
own login/refresh end to end. ``config_overrides`` still zeroes the bare-LM
feature surface (mcp servers, plugins, apps, ...) so this stays a pure
completion backend -- that mechanism does not touch credentials or the home
directory at all.

Provider-exposed reasoning text and reasoning summaries remain distinct. A
summary is never relabelled as full provider reasoning.

The official SDK owns its pinned runtime, subprocess, and JSON-RPC lifecycle,
which gives CLIO one typed cancellation path. Threads are kept open per
conversation and continued with only the new messages
(:mod:`clio_agent.providers.codex.sdk_stateful`); a thread the runtime no longer
holds raises :class:`CodexThreadLostError` so the caller resets typed. Progress is
bounded per SDK exchange, never by a composite turn deadline that could kill a
healthy long-running stream.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import queue
import tempfile
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from openai_codex import (
    ApprovalMode,
    AsyncCodex,
    CodexConfig,
    ImageInput,
    Sandbox,
    TextInput,
)
from openai_codex.types import ReasoningEffort, ReasoningSummary

from clio_agent.providers.claude_code_cancel import (
    register_sdk_stream,
    unregister_sdk_stream,
)
from clio_agent.providers.codex.errors import (
    CODEX_AUTHENTICATION_ERROR_MESSAGE,
    CodexSDKError,
    contains_codex_authentication_error,
)
from clio_agent.providers.components.client_binary import codex_client

logger = logging.getLogger(__name__)

DEFAULT_SDK_PROGRESS_TIMEOUT_S = 120.0

# Desktop Quit must not leave the hidden CLIO process chain resident while an
# already-cancelled Codex transport takes its ordinary request timeout to
# notice that the child has gone away. The owner loop is a daemon thread and
# the desktop supervisor still owns the process tree, so shutdown gets a short
# best-effort disconnect window followed by an immediate loop stop.
_SDK_SHUTDOWN_TIMEOUT_S = 2.0

#: The requested per-turn ceiling every LiteLLM entry point passes in when the
#: caller supplies no ``timeout``. It is a CEILING, not the operative deadline:
#: :func:`_sdk_progress_timeout_s` clamps it with the configured
#: ``limits.codex_sdk_progress_timeout_s``, which is what actually governs how
#: long one exchange may go without progress.
DEFAULT_TURN_TIMEOUT_S = 180.0


async def _cleanup_sdk_action(action: str, awaitable: Any) -> None:
    """Await one SDK teardown action and report failure without masking the turn."""
    try:
        await awaitable
    except Exception as exc:  # noqa: BLE001 - cleanup is typed and observable
        logger.warning(
            "Codex SDK cleanup failed reason=codex_sdk_cleanup_failed action=%s error=%r",
            action,
            exc,
        )


BARE_LM_BASE_INSTRUCTIONS = """You are a language-model completion backend inside Clio.
Answer only the serialized prompt supplied by Clio. Do not inspect the workspace,
invoke Codex tools, delegate to agents, browse, use plugins, or perform work outside
the prompt. Clio owns the agent loop and all tool execution. Follow the response
contract in the prompt and return its requested assistant content directly."""

BARE_LM_FEATURES: dict[str, bool] = {
    "apps": False,
    "browser_use": False,
    "computer_use": False,
    "image_generation": False,
    "memories": False,
    "multi_agent": False,
    "shell_tool": False,
    "view_image": False,
    "view_pdf": False,
    "workspace_dependencies": False,
}
BARE_LM_CONFIG_OVERRIDES = (
    "mcp_servers={}",
    "plugins={}",
    'web_search="disabled"',
    *(f"features.{name}=false" for name in BARE_LM_FEATURES),
)
#: Codex auto-compaction threshold for clio's threads. clio-core is the context
#: system: Codex must never summarize a thread behind it, so the threshold is set
#: beyond any context window. A compaction that happens anyway is detected
#: (``thread/compacted`` / a ``contextCompaction`` item) and resets the thread typed.
NO_AUTO_COMPACT_TOKEN_LIMIT = 2**62

BARE_LM_THREAD_CONFIG: dict[str, Any] = {
    "mcp_servers": {},
    "plugins": {},
    # Codex's own web search is a hidden action: clio owns tools (web search too).
    "web_search": "disabled",
    "features": BARE_LM_FEATURES,
    "model_auto_compact_token_limit": NO_AUTO_COMPACT_TOKEN_LIMIT,
}

_ALLOWED_ITEM_TYPES = frozenset({"agentMessage", "reasoning", "userMessage"})
_ACTION_ITEM_TYPES = frozenset(
    {
        "collabAgentToolCall",
        "commandExecution",
        "dynamicToolCall",
        "fileChange",
        "imageGeneration",
        "mcpToolCall",
        "subAgentActivity",
        "webSearch",
    }
)
_MODEL_ACTIVITY_METHODS = frozenset(
    {
        "item/agentMessage/delta",
        "item/reasoning/textDelta",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/summaryPartAdded",
    }
)
_STREAM_END = object()


def _sdk_progress_timeout_s(requested_timeout: float) -> float:
    """Resolve the maximum silence allowed for one SDK exchange or event."""
    from clio_agent import conf  # noqa: PLC0415

    configured = conf.resolve(
        "limits.codex_sdk_progress_timeout_s",
        env="CLIO_CODEX_SDK_PROGRESS_TIMEOUT_S",
        default=DEFAULT_SDK_PROGRESS_TIMEOUT_S,
        cast=conf.as_float,
    )
    return max(0.01, min(float(requested_timeout), float(configured)))


def _normalize_sdk_turn_error(message: str) -> str:
    """Replace a missing-authentication SDK failure with an actionable message."""
    return (
        CODEX_AUTHENTICATION_ERROR_MESSAGE
        if contains_codex_authentication_error(message)
        else message
    )


def _item_root(payload: Any) -> Any:
    item = getattr(payload, "item", None)
    return getattr(item, "root", item)


def _item_type(payload: Any) -> str:
    return str(getattr(_item_root(payload), "type", "") or "")


def _validate_bare_lm_event(event: Any) -> None:
    """Reject any SDK item proving Codex started an invisible inner agent action."""
    method = str(getattr(event, "method", ""))
    if method not in {"item/started", "item/completed"}:
        return
    item_type = _item_type(event.payload) or "unknown"
    if item_type in _ACTION_ITEM_TYPES or any(
        marker in item_type.lower()
        for marker in ("toolcall", "commandexecution", "filechange", "subagent")
    ):
        raise CodexSDKError(
            "bare Codex SDK LM attempted a hidden internal action "
            f"({item_type}); Clio owns tools and orchestration"
        )
    if item_type not in _ALLOWED_ITEM_TYPES:
        logger.info(
            "Codex SDK informational item skipped "
            "reason=codex_sdk_informational_item_skipped item_type=%s",
            item_type,
        )


def _normalize_usage(payload: Any) -> dict[str, int]:
    last = getattr(getattr(payload, "token_usage", None), "last", None)
    if last is None:
        return {}
    return {
        "input_tokens": int(getattr(last, "input_tokens", 0) or 0),
        "cached_input_tokens": int(getattr(last, "cached_input_tokens", 0) or 0),
        "cache_write_input_tokens": int(getattr(last, "cache_write_input_tokens", 0) or 0),
        "output_tokens": int(getattr(last, "output_tokens", 0) or 0),
        "reasoning_output_tokens": int(getattr(last, "reasoning_output_tokens", 0) or 0),
        "total_tokens": int(getattr(last, "total_tokens", 0) or 0),
    }


def _raise_failed_turn(event: Any) -> None:
    if str(getattr(event, "method", "")) != "turn/completed":
        return
    turn = getattr(event.payload, "turn", None)
    status = getattr(getattr(turn, "status", None), "value", getattr(turn, "status", ""))
    if str(status) != "failed":
        return
    error = getattr(turn, "error", None)
    message = _normalize_sdk_turn_error(
        str(getattr(error, "message", "") or "Codex SDK turn failed")
    )
    raise CodexSDKError(message)


class CodexThreadLostError(CodexSDKError):
    """A thread the caller asked to continue no longer exists on the SDK runtime.

    Raised before any input is sent (e.g. the runtime restarted since the thread was
    opened), so the caller can reset its conversation typed and send in full.
    """

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(
            f"Codex SDK thread {thread_id!r} is no longer available reason=codex_sdk_thread_lost"
        )


def _is_compaction_event(event: Any) -> bool:
    """Whether ``event`` shows Codex compacted the thread's own history."""
    method = str(getattr(event, "method", ""))
    if method == "thread/compacted":
        return True
    return method in {"item/started", "item/completed"} and (
        _item_type(event.payload) == "contextCompaction"
    )


async def _await_progress(awaitable: Any, *, timeout: float, phase: str) -> Any:
    """Await one SDK exchange, failing typed when it makes no progress in ``timeout``."""
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except TimeoutError as exc:
        raise CodexSDKError(
            f"Codex SDK made no progress for {timeout:g}s during {phase} "
            "reason=codex_sdk_progress_timeout"
        ) from exc


@dataclass(frozen=True)
class _TurnRequest:
    """One SDK turn: its input, model settings and thread continuation."""

    prompt: str
    images: list[str] | None
    model: str
    cwd: str | None
    effort: ReasoningEffort | None
    timeout: float
    thread_id: str | None
    keep_thread: bool
    on_thread: Callable[[str], None] | None
    on_compacted: Callable[[], None] | None

    def turn_input(self) -> Any:
        """The SDK turn input: the prompt text, plus native images when present."""
        if not self.images:
            return self.prompt
        return [TextInput(self.prompt), *(ImageInput(url) for url in self.images)]


class _EventRecorder:
    """Validate each SDK event, report a thread compaction once, and deliver it."""

    def __init__(
        self,
        chunks: queue.SimpleQueue[tuple[Any, Any]],
        on_compacted: Callable[[], None] | None,
        thread_id: str,
    ) -> None:
        self._chunks = chunks
        self._on_compacted = on_compacted
        self._thread_id = thread_id
        self._compaction_reported = False

    def __call__(self, event: Any) -> None:
        """Record one event (raises typed on a hidden action or a failed turn)."""
        _validate_bare_lm_event(event)
        _raise_failed_turn(event)
        if not self._compaction_reported and _is_compaction_event(event):
            self._compaction_reported = True
            logger.warning(
                "Codex SDK compacted a clio thread reason=codex_sdk_thread_compacted thread=%s",
                self._thread_id,
            )
            if self._on_compacted is not None:
                self._on_compacted()
        self._chunks.put(("event", event))


class CodexSDKClient:
    """Persistent official SDK client hosted on a private event-loop thread."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client: AsyncCodex | None = None
        self._client_lock: asyncio.Lock | None = None
        # The SDK client is a process-wide singleton shared by every concurrent codex
        # turn. ``_generation`` identifies which client a pump is holding, ``_client_users``
        # counts the pumps still holding it, and ``_reset_pending`` records a teardown
        # requested by a failed turn but not yet safe to perform.
        self._generation = 0
        self._client_users = 0
        self._reset_pending = False
        # The binary the live client was started on (see ``_ensure_client``).
        self._codex_bin: str | None = None
        # Threads kept open for continuation: thread id -> (client generation,
        # AsyncThread). Only touched on the owner loop. A generation change (the
        # runtime restarted) invalidates every entry.
        self._threads: dict[str, tuple[int, Any]] = {}

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._guard:
            if self._loop is not None:
                return self._loop
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, name="codex-sdk-loop", daemon=True)
            thread.start()
            self._loop, self._thread = loop, thread
            return loop

    async def _ensure_client(self) -> AsyncCodex:
        if self._client_lock is None:
            self._client_lock = asyncio.Lock()
        async with self._client_lock:
            codex_bin = codex_client().path
            if self._client is not None and codex_bin != self._codex_bin and not self._client_users:
                # The selected binary changed (a newer Codex was installed, or a
                # component update replaced the bundled one) and no turn holds the
                # runtime: restart it on the new binary.
                logger.info(
                    "Codex SDK runtime restarting on a new binary reason=codex_sdk_binary_changed "
                    "old=%s new=%s",
                    self._codex_bin,
                    codex_bin,
                )
                await self._close_locked()
            if self._client is None:
                # No ``env=`` override: the runtime inherits THIS process's real
                # environment, so it sees the user's own CODEX_HOME (or its
                # ~/.codex default) and owns its own login/refresh. CLIO never
                # constructs, reads, or writes an auth.json path. ``codex_bin`` is
                # the user's installed Codex CLI when present, else the bundled one.
                self._codex_bin = codex_bin
                client = AsyncCodex(
                    CodexConfig(
                        codex_bin=codex_bin,
                        cwd=tempfile.gettempdir(),
                        config_overrides=BARE_LM_CONFIG_OVERRIDES,
                        client_name="clio_agent",
                        client_title="CLIO Agent",
                    )
                )
                await client.__aenter__()
                self._client = client
                self._generation += 1
                self._client_users = 0
                self._reset_pending = False
        assert self._client is not None
        return self._client

    async def _reset_client(self) -> None:
        """Close the shared SDK client unconditionally (process teardown only)."""
        if self._client_lock is None:
            self._client_lock = asyncio.Lock()
        async with self._client_lock:
            await self._close_locked()

    async def _close_locked(self) -> None:
        """Close and forget the current client. The caller must hold ``_client_lock``."""
        client, self._client = self._client, None
        self._client_users = 0
        self._reset_pending = False
        self._generation += 1
        self._threads.clear()
        if client is not None:
            await _cleanup_sdk_action("client_close", client.close())

    async def _release_client(self, generation: int, *, reset: bool) -> None:
        """Drop one turn's hold on the shared client, tearing it down only when safe.

        ``client.close()`` terminates the single ``codex`` runtime subprocess, which
        fails every other in-flight turn's stream with a transport error attributed to
        its own turn. A teardown requested by a failing turn is therefore recorded and
        executed once the LAST holder of that client generation has left.

        Args:
            generation: The client generation the releasing pump obtained.
            reset: Whether this pump's turn ended in a state that should discard the
                shared client.
        """

        if self._client_lock is None:
            self._client_lock = asyncio.Lock()
        async with self._client_lock:
            if generation != self._generation:
                return
            self._client_users = max(0, self._client_users - 1)
            if reset:
                self._reset_pending = True
            if self._reset_pending and self._client_users == 0:
                await self._close_locked()
            elif reset:
                logger.info(
                    "Codex SDK client teardown deferred while other turns hold it "
                    "reason=codex_sdk_reset_deferred_in_flight holders=%d",
                    self._client_users,
                )

    async def _open_thread(
        self, client: AsyncCodex, generation: int, request: _TurnRequest, progress_timeout: float
    ) -> Any:
        """Resume the kept thread ``request`` names, or open a new one."""
        if request.thread_id is not None:
            kept = self._threads.get(request.thread_id)
            if kept is None or kept[0] != generation:
                self._threads.pop(request.thread_id, None)
                raise CodexThreadLostError(request.thread_id)
            return kept[1]
        thread = await _await_progress(
            client.thread_start(
                approval_mode=ApprovalMode.deny_all,
                base_instructions=BARE_LM_BASE_INSTRUCTIONS,
                config=BARE_LM_THREAD_CONFIG,
                cwd=request.cwd or tempfile.gettempdir(),
                developer_instructions=BARE_LM_BASE_INSTRUCTIONS,
                ephemeral=True,
                model=request.model,
                sandbox=Sandbox.read_only,
            ),
            timeout=progress_timeout,
            phase="thread start",
        )
        new_id = str(getattr(thread, "id", "") or "")
        if new_id:
            if request.keep_thread:
                self._threads[new_id] = (generation, thread)
            if request.on_thread is not None:
                request.on_thread(new_id)
        return thread

    async def _pump_turn(
        self, request: _TurnRequest, chunks: queue.SimpleQueue[tuple[Any, Any]]
    ) -> None:
        """Run one SDK turn on the owner loop, delivering its events to ``chunks``."""
        turn = None
        stream = None
        clean = False
        keep_client = False
        generation = -1
        try:
            progress_timeout = _sdk_progress_timeout_s(request.timeout)
            client = await _await_progress(
                self._ensure_client(), timeout=progress_timeout, phase="client startup"
            )
            # Claim a hold on this client generation. Every pump runs on the single
            # owner loop, so recording the hold in the same synchronous block as the
            # await's return is atomic with respect to the other pumps.
            generation = self._generation
            self._client_users += 1
            thread = await self._open_thread(client, generation, request, progress_timeout)
            record = _EventRecorder(chunks, request.on_compacted, str(getattr(thread, "id", "")))
            turn = await _await_progress(
                thread.turn(
                    request.turn_input(),
                    effort=request.effort,
                    summary=ReasoningSummary.model_validate("detailed"),
                ),
                timeout=progress_timeout,
                phase="turn start",
            )
            stream = turn.stream()
            while True:
                try:
                    event = await _await_progress(
                        anext(stream), timeout=progress_timeout, phase="event stream"
                    )
                except StopAsyncIteration:
                    break
                record(event)
            clean = True
        except asyncio.CancelledError:
            keep_client = True
            if turn is not None:
                await _cleanup_sdk_action("turn_interrupt_cancel", turn.interrupt())
            raise
        except BaseException as exc:  # noqa: BLE001 - delivered to caller loop
            if turn is not None:
                await _cleanup_sdk_action("turn_interrupt_error", turn.interrupt())
            # A lost thread is the caller's typed reset, not a runtime fault.
            keep_client = isinstance(exc, CodexThreadLostError)
            chunks.put(("exc", exc))
        finally:
            if stream is not None:
                close_stream = getattr(stream, "aclose", None)
                if callable(close_stream):
                    await _cleanup_sdk_action("stream_close", close_stream())
            if generation >= 0:
                # Only a pump that actually obtained the client may ask for its
                # teardown; a failure BEFORE the handshake completed would otherwise
                # close a client that belongs entirely to other turns.
                await self._release_client(generation, reset=not clean and not keep_client)
            chunks.put((_STREAM_END, None))

    async def stream(
        self,
        *,
        prompt: str,
        images: list[str] | None = None,
        model: str,
        cwd: str | None,
        effort: ReasoningEffort | None,
        timeout: float,
        thread_id: str | None = None,
        keep_thread: bool = False,
        on_thread: Callable[[str], None] | None = None,
        on_compacted: Callable[[], None] | None = None,
    ) -> AsyncIterator[Any]:
        """Bridge one typed SDK turn stream from the owner loop to the caller loop.

        Args:
            prompt: The serialized input for this turn (full prompt or delta).
            images: Native image inputs for this turn.
            model: The Codex model id.
            cwd: The neutral working directory for a new thread.
            effort: Reasoning effort for this turn.
            timeout: Per-exchange progress ceiling.
            thread_id: Continue this kept thread instead of opening a new one.
                Raises :class:`CodexThreadLostError` if it is gone.
            keep_thread: Keep a newly opened thread for later continuation.
            on_thread: Called with the id of a newly opened thread.
            on_compacted: Called once if Codex compacts the thread during the turn.
        """
        request = _TurnRequest(
            prompt=prompt,
            images=images,
            model=model,
            cwd=cwd,
            effort=effort,
            timeout=timeout,
            thread_id=thread_id,
            keep_thread=keep_thread,
            on_thread=on_thread,
            on_compacted=on_compacted,
        )
        owner_loop = self._ensure_loop()
        caller_loop = asyncio.get_running_loop()
        chunks: queue.SimpleQueue[tuple[Any, Any]] = queue.SimpleQueue()
        future = asyncio.run_coroutine_threadsafe(self._pump_turn(request, chunks), owner_loop)
        try:
            from clio_agent.gact.context import active_session_id  # noqa: PLC0415

            gact_sid = active_session_id() or ""
        except Exception:  # noqa: BLE001 - off-turn SDK calls are not cancellable by session
            gact_sid = ""

        def _cancel_future() -> None:
            future.cancel()

        # The turn cancel contract (L1): registered here exactly as it was
        # before restoration, so CLIO's session cancel path interrupts the SDK
        # turn the same way it interrupts any other provider's stream.
        handle = register_sdk_stream(gact_sid, _cancel_future)
        try:
            while True:
                kind, value = await caller_loop.run_in_executor(None, chunks.get)
                if kind is _STREAM_END:
                    break
                if kind == "exc":
                    raise value
                yield value
        finally:
            unregister_sdk_stream(handle)
            if not future.done():
                future.cancel()

    async def _archive(self, thread_ids: list[str]) -> None:
        """Archive kept threads on the runtime; each failure is logged typed."""
        client = self._client
        for thread_id in thread_ids:
            self._threads.pop(thread_id, None)
            if client is None:
                continue
            try:
                await client.thread_archive(thread_id)
            except Exception as exc:  # noqa: BLE001 - archival is typed and observable
                logger.warning(
                    "Codex SDK thread archive failed reason=codex_sdk_thread_archive_failed "
                    "thread=%s error=%r",
                    thread_id,
                    exc,
                )

    def archive_threads(self, thread_ids: list[str]) -> None:
        """Schedule archival of ``thread_ids`` on the owner loop (non-blocking)."""
        if not thread_ids:
            return
        loop = self._loop
        if loop is None or not loop.is_running():
            logger.info(
                "Codex SDK threads not archived reason=codex_sdk_runtime_not_running count=%d",
                len(thread_ids),
            )
            return
        asyncio.run_coroutine_threadsafe(self._archive(list(thread_ids)), loop)

    def release_idle_runtime(self) -> bool:
        """Stop the SDK runtime if no turn holds it; ``False`` when a turn does.

        A component update must replace the bundled ``codex`` binary, which
        Windows refuses to overwrite while the runtime process runs it.
        """
        if self._client is None:
            return True
        if self._client_users:
            return False
        self.close_blocking()
        return True

    def close_blocking(self) -> None:
        """Close the SDK-owned runtime and stop the owner loop."""
        with self._guard:
            loop, thread = self._loop, self._thread
            self._loop, self._thread = None, None
        if loop is None:
            return
        try:
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(self._reset_client(), loop).result(
                    timeout=_SDK_SHUTDOWN_TIMEOUT_S
                )
        except Exception:  # noqa: BLE001 - teardown is best effort and logged
            logger.warning("Codex SDK client teardown failed", exc_info=True)
        finally:
            if loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join(timeout=_SDK_SHUTDOWN_TIMEOUT_S)
                if thread.is_alive():
                    logger.warning(
                        "Codex SDK owner loop did not stop within %.1fs; "
                        "continuing process teardown",
                        _SDK_SHUTDOWN_TIMEOUT_S,
                    )


_SDK_CLIENT = CodexSDKClient()
atexit.register(_SDK_CLIENT.close_blocking)


__all__ = [
    "BARE_LM_CONFIG_OVERRIDES",
    "CodexSDKClient",
    "CodexThreadLostError",
    "DEFAULT_SDK_PROGRESS_TIMEOUT_S",
    "DEFAULT_TURN_TIMEOUT_S",
    "_SDK_CLIENT",
]
