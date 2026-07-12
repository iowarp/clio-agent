"""App-server streaming glue for the Codex bridge (#896, #775 no-accretion).

Owner module for the pieces that translate a warm ``codex app-server`` turn
(:mod:`clio_agent.providers.codex_app_server`) into LiteLLM completion/streaming
outputs with instrumentation parity (:mod:`clio_agent.providers.codex_audit`).
Split out of :mod:`clio_agent.providers.codex_litellm` so that provider module
stays under its file-size ratchet as the app-server transport lands.

``_resolve_codex_binary`` and the LiteLLM-facing ``CodexExecError`` are imported
lazily from :mod:`clio_agent.providers.codex_litellm` to keep the import graph
acyclic (that module imports THIS one at load time).

**Executor note (known trade-off, not an accident).** :func:`astream_app_server`
bridges the blocking pool driver onto the caller's loop via
``loop.run_in_executor(None, ...)`` — the process-wide default
``ThreadPoolExecutor``. Codex turns queued behind the per-process turn lock hold
an executor worker each while they wait, so a burst of same-key codex calls can
transiently starve unrelated ``run_in_executor`` work. The lock wait itself is
BOUNDED (typed timeout in ``CodexAppServerProcess.run_turn``), which caps the
hold time. The clean fix is a dedicated executor / owner loop-thread per pooled
process (the ``claude_code_sessions`` dedicated-loop pattern); that is a
follow-up, not this pass.
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

from clio_agent.providers.codex_app_server import _APP_SERVER_POOL, CodexAppServerError
from clio_agent.providers.codex_audit import (
    emit_call_started,
    emit_call_usage,
    emit_normalized,
    emit_raw_event,
)

_CALL_COUNTER_LOCK = threading.Lock()
_CALL_COUNTER = 0


def _next_call_index() -> int:
    """Return a process-local Codex provider call index for the audit rows."""
    global _CALL_COUNTER  # noqa: PLW0603
    with _CALL_COUNTER_LOCK:
        _CALL_COUNTER += 1
        return _CALL_COUNTER


def usage_chunk(usage: dict[str, int] | None) -> dict[str, int] | None:
    """Map a normalized codex usage dict to the LiteLLM streaming-chunk usage."""
    if not usage:
        return None
    prompt_tokens = int(usage.get("input_tokens", 0) or 0)
    completion_tokens = int(usage.get("output_tokens", 0) or 0)
    total = int(usage.get("total_tokens", 0) or 0) or (prompt_tokens + completion_tokens)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total,
    }


def _stream_chunk(
    *, text: str, is_finished: bool, finish_reason: str | None = None, usage: dict | None = None
) -> dict[str, Any]:
    """Build a LiteLLM-compatible streaming chunk (same shape as claude_code)."""
    return {
        "text": text,
        "is_finished": is_finished,
        "finish_reason": finish_reason or ("stop" if is_finished else None),
        "index": 0,
        "tool_use": None,
        "usage": usage,
    }


def _app_server_events(
    *, prompt: str, model: str, cwd: str | None, effort: str | None, timeout: float
) -> Iterator[Any]:
    """Yield ``TurnEvent``s for one app-server turn on the warm pool."""
    from clio_agent.providers.codex_litellm import _resolve_codex_binary  # noqa: PLC0415

    binary = _resolve_codex_binary()
    process = _APP_SERVER_POOL.process_for(binary=binary, model=model, cwd=cwd)
    yield from process.run_turn(prompt=prompt, effort=effort, timeout=timeout)


def run_app_server(
    *,
    prompt: str,
    model: str,
    cwd: str | None = None,
    effort: str | None = None,
    timeout: float = 180.0,
    call_index: int = 0,
) -> tuple[str, dict[str, int]]:
    """Blocking app-server turn → ``(text, normalized_usage)`` (completion path)."""
    from clio_agent.providers.codex_litellm import CodexExecError  # noqa: PLC0415

    call_id = uuid.uuid4().hex
    emit_call_started(call_id=call_id, call_index=call_index, model=model, prompt=prompt)
    final_text = ""
    usage: dict[str, int] = {}
    try:
        # closing() guarantees the turn generator's GeneratorExit path runs
        # deterministically (turn lock released, sink invalidated) even when this
        # loop exits early via an exception raised in the body.
        with contextlib.closing(
            _app_server_events(prompt=prompt, model=model, cwd=cwd, effort=effort, timeout=timeout)
        ) as events:
            for event in events:
                if event.kind == "usage":
                    usage = event.usage
                elif event.kind == "final":
                    final_text = event.text
                    usage = event.usage or usage
    except CodexAppServerError as exc:
        raise CodexExecError(f"codex app-server turn failed (model={model}): {exc}") from exc
    finally:
        emit_call_usage(
            call_id=call_id,
            call_index=call_index,
            model=model,
            usage=usage,
            output_chars=len(final_text),
        )
    if not final_text:
        raise CodexExecError(f"codex app-server returned empty content (model={model})")
    return final_text, usage


async def astream_app_server(
    *,
    prompt: str,
    model: str,
    cwd: str | None,
    effort: str | None,
    timeout: float,
    call_index: int,
) -> AsyncIterator[dict[str, Any]]:
    """Stream one app-server turn as LiteLLM chunks (the #896 streaming lane).

    ``item/agentMessage/delta`` events flow verbatim into the frozen streamed-chunk
    pipeline (the contract arrives whole inside the agent message — no #877 marker
    split applies on this transport). Reasoning-summary deltas, when present, go to
    the provider-thinking lane ONLY (never promoted to contract content); they are
    typed-absent on the subscription backend.

    The blocking pool driver runs on ONE pump worker that owns the sync turn
    generator end-to-end (the claude_code_sessions pump shape): events bridge to
    the caller's loop over a thread-safe queue, and on caller abandonment the
    pump — not the caller's thread — closes the generator, which fires the
    ``GeneratorExit`` path in ``run_turn`` (best-effort ``turn/interrupt``, sink
    invalidated, turn lock released). A cross-thread ``gen.close()`` from the
    caller would race the executing frame ("generator already executing"), which
    is why abandonment is signalled via an event the pump checks between items.
    """
    from clio_agent.providers.codex_litellm import CodexExecError  # noqa: PLC0415

    call_id = uuid.uuid4().hex
    emit_call_started(call_id=call_id, call_index=call_index, model=model, prompt=prompt)
    loop = asyncio.get_running_loop()
    gen = _app_server_events(prompt=prompt, model=model, cwd=cwd, effort=effort, timeout=timeout)
    events_q: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
    abandoned = threading.Event()

    def _pump() -> None:
        """Drive the sync generator on this worker; it alone opens/closes it."""
        try:
            with contextlib.closing(gen) as source:
                for ev in source:
                    if abandoned.is_set():
                        # closing() fires GeneratorExit inside run_turn →
                        # turn/interrupt + sink invalidation + lock release.
                        break
                    events_q.put(("event", ev))
        except BaseException as exc:  # noqa: BLE001 - surfaced onto the caller loop
            events_q.put(("exc", exc))
        finally:
            events_q.put(("end", None))

    pump_future = loop.run_in_executor(None, _pump)

    final_text = ""
    final_usage: dict[str, int] = {}
    final_reason = "stop"
    event_index = 0
    try:
        while True:
            kind, payload = await loop.run_in_executor(None, events_q.get)
            if kind == "end":
                break
            if kind == "exc":
                if isinstance(payload, CodexAppServerError):
                    raise CodexExecError(
                        f"codex app-server stream failed (model={model}): {payload}"
                    ) from payload
                raise payload
            event = payload
            event_index += 1
            if event.kind == "text":
                emit_raw_event(
                    call_index=call_index,
                    event_index=event_index,
                    source_channel="text_delta",
                    text=event.text,
                    raw_event_type="item/agentMessage/delta",
                )
                emit_normalized(
                    call_index=call_index,
                    event_index=event_index,
                    source_channel="text_delta",
                    normalized_event="contract.content",
                    text=event.text,
                )
                yield _stream_chunk(text=event.text, is_finished=False)
            elif event.kind == "reasoning":
                emit_raw_event(
                    call_index=call_index,
                    event_index=event_index,
                    source_channel="thinking_delta",
                    text=event.text,
                    raw_event_type="item/reasoning/delta",
                )
                try:
                    from clio_agent.runtime.lm_activity import (  # noqa: PLC0415
                        note_lm_provider_thinking_delta,
                    )

                    note_lm_provider_thinking_delta(event.text, provider="codex_app_server")
                except Exception:  # noqa: BLE001,S110 - debug lane must not break the turn
                    pass
            elif event.kind == "usage":
                final_usage = event.usage or final_usage
            elif event.kind == "final":
                final_text = event.text
                final_usage = event.usage or final_usage
                final_reason = event.reason
    finally:
        # Abandonment (or any exit): tell the pump to close the generator on ITS
        # thread; it wakes on the next event or the bounded turn timeout.
        abandoned.set()
        pump_future.cancel()  # no-op once running; prevents a never-started pump
        emit_call_usage(
            call_id=call_id,
            call_index=call_index,
            model=model,
            usage=final_usage,
            output_chars=len(final_text),
        )
    yield _stream_chunk(
        text="",
        is_finished=True,
        finish_reason=final_reason,
        usage=usage_chunk(final_usage),
    )


__all__ = [
    "astream_app_server",
    "run_app_server",
    "usage_chunk",
    "_app_server_events",
    "_next_call_index",
]
