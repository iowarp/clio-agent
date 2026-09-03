"""Non-streaming thread-backed Claude Agent SDK session pool (#715/#818).

Owner module for the *blocking* ``completion`` path's persistent SDK sessions,
carved out of :mod:`clio_agent.providers.claude_code_sessions` so that file stays
focused on the #891 per-expert *streaming* session/delta transport (#775
no-accretion). Moved verbatim from the litellm god-file; re-exported by
``claude_code_sessions`` (and thus ``claude_code_litellm``) for the historical
import seams and tests.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import threading
import uuid
from typing import Any

from clio_agent.providers.claude_code_multimodal import sdk_prompt
from clio_agent.providers.claude_code_options import build_sdk_options, thinking_key

logger = logging.getLogger(__name__)

__all__ = [
    "_SDK_SESSION_POOL",
    "_SdkSession",
    "_SdkSessionPool",
    "_run_sdk",
]


class _SdkSession:
    """Process-wide persistent Claude Agent SDK session (#715).

    One ``ClaudeSDKClient`` CLI connection is opened once and reused across every LM
    call, so calls after the first avoid the ~10-15s cold start. All SDK I/O runs on a
    single dedicated asyncio loop in a daemon thread; worker threads submit coroutines
    via ``run_coroutine_threadsafe`` and block, serializing concurrent calls onto the
    one connection. The connection is opened lazily (see :func:`build_sdk_options` for
    the bare-model transport options) and rebuilt when model/cwd/thinking change.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: Any = None
        self._thread: threading.Thread | None = None
        self._client: Any = None
        self._model: str | None = None
        self._cwd: str | None = None
        self._thinking_key: str | None = None

    def _ensure_loop(self) -> None:
        if self._loop is not None:
            return
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, name="claude-sdk-loop", daemon=True)
        thread.start()
        self._loop, self._thread = loop, thread
        atexit.register(self.close)

    def _submit(self, coro: Any, timeout: float | None) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)

    async def _aconnect(self, model: str, cwd: str | None, thinking: dict[str, Any] | None) -> Any:
        from claude_agent_sdk import ClaudeSDKClient  # noqa: PLC0415

        client = ClaudeSDKClient(
            options=build_sdk_options(model=model, cwd=cwd, stream=False, thinking=thinking)
        )
        await client.connect()
        return client

    async def _aquery(
        self,
        prompt: str,
        *,
        native_blocks: list[dict[str, Any]] | None = None,
        model: str,
    ) -> tuple[str, dict[str, Any]]:
        from claude_agent_sdk import (  # noqa: PLC0415
            AssistantMessage,
            ResultMessage,
            TextBlock,
        )

        from clio_agent.providers._cli_provider import raise_model_rejected  # noqa: PLC0415
        from clio_agent.providers.claude_code_litellm import (  # noqa: PLC0415
            CLAUDE_CODE_REJECTION_STATUS,
        )

        query_input: Any = sdk_prompt(prompt, native_blocks) if native_blocks else prompt
        await self._client.query(query_input, session_id=uuid.uuid4().hex)
        parts: list[str] = []
        usage: dict[str, Any] = {}
        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                parts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
            elif isinstance(msg, ResultMessage):
                u = getattr(msg, "usage", None)
                if isinstance(u, dict):
                    usage = u
                if getattr(msg, "is_error", False):
                    status = getattr(msg, "api_error_status", None)
                    if status == CLAUDE_CODE_REJECTION_STATUS:
                        # #1184 / #1211 review A3/D3: the blocking path silently
                        # degraded a definitive model rejection to a generic
                        # "empty content" error, discarding the status AND the
                        # CLI's own explanatory text. Raise the typed,
                        # non-retryable rejection here so `complete()`'s
                        # empty-content fallback is never reached for this case.
                        raise_model_rejected(
                            message=(
                                f"claude_code rejected model {model!r} "
                                f"(api_error_status={status}): "
                                f"{getattr(msg, 'result', '') or 'model not available'}"
                            ),
                            model=f"claude_code/{model}",
                            llm_provider="claude_code",
                        )
        return "".join(parts).strip(), usage

    def _reset_client(self) -> None:
        if self._client is None:
            return
        try:
            self._submit(self._client.disconnect(), timeout=15.0)
        except Exception:  # noqa: BLE001 - best-effort teardown; never block the caller
            logger.warning("claude sdk client disconnect failed", exc_info=True)
        self._client = self._model = self._cwd = self._thinking_key = None

    def complete(
        self,
        *,
        prompt: str,
        native_blocks: list[dict[str, Any]] | None = None,
        model: str,
        timeout: float | None,
        cwd: str | None,
        thinking: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        from clio_agent.providers.claude_code_litellm import ClaudeCodeExecError  # noqa: PLC0415
        from clio_agent.providers.claude_code_options import (  # noqa: PLC0415
            require_claude_agent_sdk,
        )

        # Single typed seam (finding #2): raises a structured mcp-2 unavailability
        # error rather than a raw ImportError when the SDK is absent/uninstallable.
        require_claude_agent_sdk()

        tkey = thinking_key(thinking)
        with self._lock:
            self._ensure_loop()
            if (
                self._client is None
                or self._model != model
                or self._cwd != cwd
                or self._thinking_key != tkey
            ):
                self._reset_client()
                self._client = self._submit(self._aconnect(model, cwd, thinking), timeout=60.0)
                self._model, self._cwd, self._thinking_key = model, cwd, tkey
            try:
                text, usage = self._submit(
                    self._aquery(prompt, native_blocks=list(native_blocks or []), model=model),
                    timeout=timeout,
                )
            except TimeoutError as exc:
                # A timed-out call leaves the connection mid-cycle; drop it so the
                # next call reconnects cleanly.
                self._reset_client()
                raise ClaudeCodeExecError(
                    f"claude agent sdk timed out after {timeout}s (model={model})"
                ) from exc
        if not text:
            raise ClaudeCodeExecError(f"claude agent sdk returned empty content (model={model})")
        return text, usage

    def close(self) -> None:
        with self._lock:
            self._reset_client()
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._loop = None


class _SdkSessionPool:
    """Keyed pool of persistent Claude Agent SDK sessions (#818).

    Maps ``(model, cwd, thinking)`` to a dedicated :class:`_SdkSession`, so experts
    that want *different* ``claude_code`` models or thinking levels run concurrently —
    each holds its own CLI connection instead of thrashing one shared session that
    reconnects on every flip. Same-key calls share one session and serialize onto its
    single connection; distinct-key calls never contend. The pool lock is held only
    for the O(1) session lookup/creation, never across a completion.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[tuple[str, str | None, str | None], _SdkSession] = {}

    def _session_for(
        self, model: str, cwd: str | None, thinking_id: str | None = None
    ) -> _SdkSession:
        """Return (creating if needed) the session bound to ``(model, cwd, thinking)``."""

        key = (model, cwd, thinking_id)
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                session = _SdkSession()
                self._sessions[key] = session
            return session

    def complete(
        self,
        *,
        prompt: str,
        native_blocks: list[dict[str, Any]] | None = None,
        model: str,
        timeout: float | None,
        cwd: str | None,
        thinking: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Complete one turn on the session keyed by ``(model, cwd, thinking)``."""

        return self._session_for(model, cwd, thinking_key(thinking)).complete(
            prompt=prompt,
            native_blocks=native_blocks,
            model=model,
            timeout=timeout,
            cwd=cwd,
            thinking=thinking,
        )

    def close(self) -> None:
        """Tear down every pooled session and drop the pool."""

        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()


_SDK_SESSION_POOL = _SdkSessionPool()
atexit.register(_SDK_SESSION_POOL.close)


def _run_sdk(
    *,
    prompt: str,
    native_blocks: list[dict[str, Any]] | None = None,
    model: str,
    timeout: float | None = 180.0,
    cwd: str | None = None,
    thinking: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Run one completion via the keyed Claude Agent SDK session pool (#715, #818).

    Delegates to the process-wide :data:`_SDK_SESSION_POOL`, which keeps one CLI
    connection per ``(model, cwd, thinking)`` so distinct-model/thinking experts run
    concurrently without reconnect thrash. Returns ``(text, usage)`` in the same
    shape as the exec transport.
    """
    return _SDK_SESSION_POOL.complete(
        prompt=prompt,
        native_blocks=list(native_blocks or []),
        model=model,
        timeout=timeout,
        cwd=cwd,
        thinking=thinking,
    )
