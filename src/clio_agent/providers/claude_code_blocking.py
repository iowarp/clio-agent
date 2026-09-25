"""The blocking ``completion()`` entry point for ``claude_code`` (S2 B1).

Owner module (#775 no-accretion) for the ONE function that used to be the
standalone thread-backed SDK session pool (``claude_code_sdk_pool.py``,
deleted in the same change): ``_run_sdk`` now drives
:func:`clio_agent.providers.claude_code_litellm._astream_sdk` — the SAME
per-GACT-session pooled client the streaming path rides — to completion under
a fresh event loop, and collects the result into the ``(text, usage)`` shape
``ClaudeCodeLLM.completion()`` returns.

``_astream_sdk`` is imported lazily (inside the functions, not at module
load) to avoid a circular import: :mod:`clio_agent.providers.claude_code_litellm`
imports ``_run_sdk`` from here at module load, so this module cannot import
back from it at module load too.
"""

from __future__ import annotations

import asyncio
from typing import Any

__all__ = ["_run_sdk"]


async def _collect_sdk_response(
    *,
    prompt: str,
    native_blocks: list[dict[str, Any]] | None,
    model: str,
    timeout: float | None,
    cwd: str | None,
    thinking: dict[str, Any] | None,
    system_prompt: str | None,
    call_index: int,
) -> tuple[str, dict[str, Any]]:
    """Collect one claude_code SDK turn into ``(text, usage)``."""
    from clio_agent.providers.claude_code_litellm import _astream_sdk  # noqa: PLC0415

    usage_sink: dict[str, Any] = {}
    parts: list[str] = []
    async for chunk in _astream_sdk(
        prompt=prompt,
        model=model,
        timeout=timeout,
        cwd=cwd,
        call_index=call_index,
        thinking=thinking,
        system_prompt=system_prompt,
        native_blocks=native_blocks,
        usage_sink=usage_sink,
    ):
        text = chunk.get("text")
        if text:
            parts.append(str(text))
    return "".join(parts).strip(), usage_sink


def _run_sdk(
    *,
    prompt: str,
    native_blocks: list[dict[str, Any]] | None = None,
    model: str,
    timeout: float | None = 180.0,
    cwd: str | None = None,
    thinking: dict[str, Any] | None = None,
    system_prompt: str | None = None,
    call_index: int = 0,
) -> tuple[str, dict[str, Any]]:
    """Run one blocking claude_code completion (the ``completion()`` entry point).

    Drives ``_astream_sdk`` — the SAME per-GACT-session pooled client the
    streaming path uses — to completion under a fresh event loop (this is a
    SYNC entry point; ``acompletion`` already runs it on a worker thread via
    ``asyncio.to_thread`` so this never blocks a caller's own loop).
    """
    return asyncio.run(
        _collect_sdk_response(
            prompt=prompt,
            native_blocks=native_blocks,
            model=model,
            timeout=timeout,
            cwd=cwd,
            thinking=thinking,
            system_prompt=system_prompt,
            call_index=call_index,
        )
    )
