"""Codex SDK turn stream -> LiteLLM chunks + CLIO thinking lanes.

Split out of :mod:`clio_agent.providers.codex.sdk_client` (no-accretion): the client
module owns the SDK runtime, its thread lifecycle and the per-exchange progress
bounds; this module owns turning ONE SDK turn's typed event stream into LiteLLM
streaming chunks, the provider-thinking lane, usage (including cached input tokens),
and the stateful send-plan hooks (:class:`~clio_agent.providers.codex.sdk_stateful.CodexSend`).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
import threading
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

from openai_codex import CodexError
from openai_codex.types import ReasoningEffort

from clio_agent.providers._cli_provider import raise_model_rejected
from clio_agent.providers.codex.errors import CodexSDKError
from clio_agent.providers.codex.sdk_audit import (
    emit_call_started,
    emit_call_usage,
    emit_normalized,
    emit_raw_event,
)
from clio_agent.providers.codex.sdk_client import (
    _SDK_CLIENT,
    DEFAULT_TURN_TIMEOUT_S,
    CodexThreadLostError,
    _item_root,
    _normalize_usage,
)
from clio_agent.providers.codex.sdk_stateful import CodexSend, take_threads_to_archive

_CALL_COUNTER_LOCK = threading.Lock()
_CALL_COUNTER = 0
_CODEX_MODEL_REJECTION_PATTERN = re.compile(
    r"is not supported when using codex with (a|an)\b[^.]{0,40}account",
    re.IGNORECASE,
)


def _next_call_index() -> int:
    """Return a process-local Codex SDK provider call index for audit correlation."""
    global _CALL_COUNTER  # noqa: PLW0603
    with _CALL_COUNTER_LOCK:
        _CALL_COUNTER += 1
        return _CALL_COUNTER


def _is_codex_model_rejection(text: str, *, model: str) -> bool:
    """Return whether ``text`` is the verified account/model rejection shape."""
    return bool(text and model and model in text and _CODEX_MODEL_REJECTION_PATTERN.search(text))


def usage_chunk(usage: dict[str, int] | None) -> dict[str, Any] | None:
    """Map normalized SDK usage to the LiteLLM streaming usage shape."""
    if not usage:
        return None
    prompt_tokens = int(usage.get("input_tokens", 0) or 0)
    completion_tokens = int(usage.get("output_tokens", 0) or 0)
    total = int(usage.get("total_tokens", 0) or 0) or prompt_tokens + completion_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_output_tokens": int(usage.get("reasoning_output_tokens", 0) or 0),
        "total_tokens": total,
        "prompt_tokens_details": {
            "cached_tokens": int(usage.get("cached_input_tokens", 0) or 0),
        },
    }


def _stream_chunk(
    *,
    text: str,
    is_finished: bool,
    finish_reason: str | None = None,
    usage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one LiteLLM-compatible streaming chunk."""
    return {
        "text": text,
        "is_finished": is_finished,
        "finish_reason": finish_reason or ("stop" if is_finished else None),
        "index": 0,
        "tool_use": None,
        "usage": usage,
    }


def _note_provider_thinking(text: str, *, summary: bool) -> None:
    if not text:
        return
    try:
        from clio_agent.runtime.lm_activity import note_lm_provider_thinking_delta

        provider = "codex_sdk_summary" if summary else "codex_sdk_reasoning"
        note_lm_provider_thinking_delta(text, provider=provider)
    except Exception:  # noqa: BLE001,S110 - observability must not break the turn
        pass


class _TurnAccumulator:
    """Fold one SDK turn's events into text deltas, thinking lanes, usage and a fallback."""

    def __init__(self, call_index: int) -> None:
        self.call_index = call_index
        self.final_text = ""
        self.fallback_text = ""
        self.usage: dict[str, int] = {}
        self._event_index = 0
        self._summary_parts = 0

    def handle(self, event: Any) -> str:
        """Record ``event``; return the answer-text delta it carries (``""`` if none)."""
        self._event_index += 1
        method = str(getattr(event, "method", ""))
        payload = event.payload
        if method == "item/agentMessage/delta":
            return self._text_delta(method, str(getattr(payload, "delta", "") or ""))
        if method == "item/reasoning/summaryPartAdded":
            self._summary_boundary(method)
        elif method in {"item/reasoning/textDelta", "item/reasoning/summaryTextDelta"}:
            self._thinking_delta(method, str(getattr(payload, "delta", "") or ""))
        elif method == "thread/tokenUsage/updated":
            self.usage = _normalize_usage(payload) or self.usage
        elif method == "item/completed":
            self._completed_item(_item_root(payload))
        return ""

    def _text_delta(self, method: str, text: str) -> str:
        if not text:
            return ""
        self.final_text += text
        emit_raw_event(
            call_index=self.call_index,
            event_index=self._event_index,
            source_channel="text_delta",
            text=text,
            raw_event_type=method,
        )
        emit_normalized(
            call_index=self.call_index,
            event_index=self._event_index,
            source_channel="text_delta",
            normalized_event="contract.content",
            text=text,
        )
        return text

    def _summary_boundary(self, method: str) -> None:
        if self._summary_parts:
            boundary = "\n\n"
            emit_raw_event(
                call_index=self.call_index,
                event_index=self._event_index,
                source_channel="reasoning_summary",
                text=boundary,
                raw_event_type=method,
            )
            _note_provider_thinking(boundary, summary=True)
        self._summary_parts += 1

    def _thinking_delta(self, method: str, text: str) -> None:
        is_summary = method.endswith("summaryTextDelta")
        emit_raw_event(
            call_index=self.call_index,
            event_index=self._event_index,
            source_channel="reasoning_summary" if is_summary else "reasoning_text",
            text=text,
            raw_event_type=method,
        )
        _note_provider_thinking(text, summary=is_summary)

    def _completed_item(self, item: Any) -> None:
        if str(getattr(item, "type", "")) != "agentMessage":
            return
        phase_value = getattr(item, "phase", None)
        phase = getattr(phase_value, "value", phase_value)
        if phase == "final_answer" or not self.fallback_text:
            self.fallback_text = str(getattr(item, "text", "") or "")


def _typed_stream_failure(exc: CodexError, *, model: str) -> Exception:
    """Map an SDK error to CLIO's typed model-rejection or stream-failure error."""
    message = str(exc)
    if _is_codex_model_rejection(message, model=model):
        raise_model_rejected(
            message=f"codex sdk rejected model {model!r}: {message}",
            model=f"codex_sdk/{model}",
            llm_provider="codex_sdk",
            cause=exc,
        )
    return CodexSDKError(f"Codex SDK stream failed (model={model}): {exc}")


async def astream_sdk(
    *,
    prompt: str,
    images: list[str] | None = None,
    model: str,
    cwd: str | None,
    effort: ReasoningEffort | None,
    timeout: float,
    call_index: int,
    send: CodexSend | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream one official SDK turn into LiteLLM chunks and CLIO thinking lanes.

    ``send`` (from :func:`~clio_agent.providers.codex.sdk_stateful.resolve_codex_send`)
    selects continuation of a kept thread vs a new one; ``None`` is a plain full send
    on an ephemeral thread. A mid-flight failure drops the conversation's thread
    (typed ``provider_error`` on the next call); :class:`CodexThreadLostError` is
    re-raised untouched for the transport to reset and resend in full.
    """
    call_id = send.call_id if send is not None else uuid.uuid4().hex
    emit_call_started(call_id=call_id, call_index=call_index, model=model, prompt=prompt)
    turn = _TurnAccumulator(call_index)
    try:
        async for event in _SDK_CLIENT.stream(
            prompt=prompt,
            images=images,
            model=model,
            cwd=cwd,
            effort=effort,
            timeout=timeout,
            thread_id=send.thread_id if send is not None else None,
            keep_thread=bool(send is not None and send.engaged),
            on_thread=send.bind_thread if send is not None else None,
            on_compacted=send.note_provider_compacted if send is not None else None,
        ):
            if text := turn.handle(event):
                yield _stream_chunk(text=text, is_finished=False)
    except CodexThreadLostError:
        raise
    except (Exception, asyncio.CancelledError) as exc:
        # A failed or cancelled turn leaves the thread in an unknown state: drop it
        # so the conversation's next call is a typed ``provider_error`` full send.
        if send is not None:
            send.note_error()
        if isinstance(exc, CodexError):
            raise _typed_stream_failure(exc, model=model) from exc
        raise
    finally:
        _SDK_CLIENT.archive_threads(take_threads_to_archive())
        emit_call_usage(
            call_id=call_id,
            call_index=call_index,
            model=model,
            usage=turn.usage,
            output_chars=len(turn.final_text or turn.fallback_text),
        )
    if not turn.final_text and turn.fallback_text:
        turn.final_text = turn.fallback_text
        yield _stream_chunk(text=turn.fallback_text, is_finished=False)
    if not turn.final_text:
        raise CodexSDKError(f"Codex SDK returned empty content (model={model})")
    yield _stream_chunk(
        text="",
        is_finished=True,
        finish_reason="stop",
        usage=usage_chunk(turn.usage),
    )


def run_sdk(
    *,
    prompt: str,
    images: list[str] | None = None,
    model: str,
    cwd: str | None = None,
    effort: ReasoningEffort | None = None,
    timeout: float = DEFAULT_TURN_TIMEOUT_S,
    call_index: int = 0,
    send: CodexSend | None = None,
) -> tuple[str, dict[str, int]]:
    """Collect one official SDK stream for LiteLLM's blocking completion path.

    Provider-contract BACKSTOP (#1333): LiteLLM's sync ``completion`` must be callable
    from a thread that already owns a running loop (claude_code's pool is), where a bare
    ``asyncio.run()`` raises ``RuntimeError``. This uses the repository's
    run-or-threadpool bridge (``handshake.run_handshake_sync``,
    ``runtime.status._list_gateway_capabilities``): no loop on this thread -> run
    inline; a loop is running -> run the collection on a helper thread and BLOCK the
    caller. It turns a crash into a blocking call, it does not make the sync path
    loop-friendly: clio's own loop-side LM calls (the finalize goal judge) take the
    native async path (``acompletion``) and never reach here. Blocking is safe because
    the stream only depends on the helper's own loop and the dedicated
    ``codex-sdk-loop`` owner thread, never on the caller's loop.
    """

    async def _collect() -> tuple[str, dict[str, int]]:
        parts: list[str] = []
        final_usage: dict[str, int] = {}
        async for chunk in astream_sdk(
            prompt=prompt,
            images=images,
            model=model,
            cwd=cwd,
            effort=effort,
            timeout=timeout,
            call_index=call_index,
            send=send,
        ):
            parts.append(str(chunk.get("text") or ""))
            raw_usage = chunk.get("usage")
            if isinstance(raw_usage, dict):
                details = raw_usage.get("prompt_tokens_details") or {}
                final_usage = {
                    "cached_input_tokens": int(details.get("cached_tokens", 0) or 0),
                    "input_tokens": int(raw_usage.get("prompt_tokens", 0) or 0),
                    "output_tokens": int(raw_usage.get("completion_tokens", 0) or 0),
                    "reasoning_output_tokens": int(
                        raw_usage.get("reasoning_output_tokens", 0) or 0
                    ),
                    "total_tokens": int(raw_usage.get("total_tokens", 0) or 0),
                }
        return "".join(parts), final_usage

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_collect())
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="codex-sdk-sync"
    ) as pool:
        return pool.submit(lambda: asyncio.run(_collect())).result()


__all__ = [
    "_next_call_index",
    "astream_sdk",
    "run_sdk",
    "usage_chunk",
]
