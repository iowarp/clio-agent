"""Declare attention ranges at the current DSPy public call boundary.

The request context encloses DSPy's trace callbacks and its streaming/retry path.
No legacy IOLoggingLM transport or second provider implementation is restored.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

from clio_agent.gact.attention.declare import declared_request


@contextmanager
def attention_request(
    lm: Any, prompt: Any, messages: Any, kwargs: dict[str, Any]
) -> Iterator[tuple[Any, Any, dict[str, Any]]]:
    """Prepare one legacy or typed request without mutating the caller's inputs."""
    from dspy.clients.lm15_boundary import request_kwargs  # noqa: PLC0415
    from dspy.lm15 import Request  # noqa: PLC0415

    from clio_agent.provenance_config import attention_capture_enabled  # noqa: PLC0415

    if not attention_capture_enabled():
        yield prompt, messages, kwargs
        return
    typed = prompt if isinstance(prompt, Request) else kwargs.get("request")
    if isinstance(typed, Request):
        # Use the same dialect serializer as DSPy, including native tool calls.
        wire = request_kwargs(typed, "chat")
        extensions = dict(typed.config.extensions or {})
        with declared_request(
            model=lm.model,
            messages=wire["messages"],
            lm_kwargs=lm.kwargs,
            call_kwargs={"tools": wire.get("tools"), "extra_body": extensions},
        ) as declared:
            updated = replace(
                typed,
                config=replace(typed.config, extensions=declared.get("extra_body", extensions)),
            )
            if isinstance(prompt, Request):
                yield updated, messages, kwargs
            else:
                yield prompt, messages, {**kwargs, "request": updated}
        return
    with declared_request(
        model=lm.model,
        messages=messages if messages is not None else [{"role": "user", "content": prompt}],
        lm_kwargs=lm.kwargs,
        call_kwargs=kwargs,
    ) as declared:
        yield prompt, messages, declared


def attention_lm_class() -> Any:
    """Return the lazy DSPy subclass that preserves declarations through callbacks."""
    from clio_agent.lm.attention_dspy import AttentionLM  # noqa: PLC0415

    return AttentionLM
