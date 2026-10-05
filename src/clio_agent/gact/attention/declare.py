"""Write path: declare labelled attention ranges on vLLM requests.

On a vLLM call (LiteLLM prefix ``hosted_vllm/``) with ``provenance.attention``
on, the request is rendered with the model's own tokenizer + chat template, one
token range per transcript section is sent as ``kv_transfer_params.ranges``
(vllm-attn-connector's per-request channel, a top-level chat-completions field
that LiteLLM forwards via ``extra_body``), and the labelled ranges are recorded
on the call's ``lm.call`` provenance record (see :func:`current_declaration`).

Never for non-vLLM providers; a skipped declaration records a typed reason on
the ``lm.call`` record instead of disappearing. Known tokenizer/render failures
send the request undeclared (the connector then uses fixed chunks) and record
``attention_tokenizer_unavailable``. Unexpected programming errors propagate.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from jinja2 import TemplateError

from clio_agent.gact.attention.ranges import declare_ranges
from clio_agent.gact.attention.reasons import AttentionUnavailable

DECLARATION_SCHEMA = "clio.attention.declaration.v1"
VLLM_PREFIX = "hosted_vllm/"

_current: ContextVar[dict[str, Any] | None] = ContextVar("clio_attention_declaration", default=None)
_messages: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "clio_attention_wire_messages", default=None
)


def current_wire_messages() -> list[dict[str, Any]] | None:
    """Return the exact text-only wire messages for the active declaration."""
    return _messages.get()


def current_declaration() -> dict[str, Any] | None:
    """The declaration record of the LM call running in this context, if any."""
    return _current.get()


def _template_kwargs(
    merged: dict[str, Any], lm_kwargs: dict[str, Any], call_kwargs: dict[str, Any]
) -> dict[str, Any]:
    body = {**(lm_kwargs.get("extra_body") or {}), **(call_kwargs.get("extra_body") or {})}
    kwargs = dict(body.get("chat_template_kwargs") or {}) if isinstance(body, dict) else {}
    if merged.get("tools"):
        kwargs["tools"] = merged["tools"]
    return kwargs


def _not_declared(reason: str, detail: str = "") -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema": DECLARATION_SCHEMA,
        "status": "not_declared",
        "reason": reason,
    }
    if detail:
        record["detail"] = detail
    return record


def build_declaration(
    *,
    model: str,
    messages: list[dict[str, Any]],
    lm_kwargs: dict[str, Any],
    call_kwargs: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(call_kwargs', record)`` for one LM call (feature already enabled).

    ``record`` is always returned: the declared ranges, or the typed reason the
    call went out undeclared.
    """
    if not model.startswith(VLLM_PREFIX):
        return call_kwargs, _not_declared("provider_not_vllm")
    if any(isinstance(message.get("content"), list) for message in messages):
        # A text tokenizer cannot establish image patch/token coordinates. Never
        # claim alignment or persist inline media bytes as declaration metadata.
        return call_kwargs, _not_declared("attention_multimodal_mapping_unavailable")
    merged = {**lm_kwargs, **call_kwargs}
    served_model = model[len(VLLM_PREFIX) :]
    template_kwargs = _template_kwargs(merged, lm_kwargs, call_kwargs)
    try:
        from clio_agent.gact.attention.tokenizer_source import (  # noqa: PLC0415
            resolve_renderer,
        )

        renderer = resolve_renderer(str(merged.get("api_base") or ""), served_model)
        encoded = renderer.render_encoded(messages, template_kwargs)
    except AttentionUnavailable as exc:
        return call_kwargs, _not_declared(exc.reason, exc.detail)
    except (OSError, ValueError, TypeError, RuntimeError, LookupError, TemplateError) as exc:
        return call_kwargs, _not_declared(
            "attention_tokenizer_unavailable", f"render failed: {type(exc).__name__}"
        )
    declaration = declare_ranges(messages, encoded)
    # DSPy merges the LM's kwargs under the call's, so a call-level extra_body would
    # replace the LM-level one wholesale: carry both forward explicitly.
    body = {**(lm_kwargs.get("extra_body") or {}), **(call_kwargs.get("extra_body") or {})}
    kv_params = dict(body.get("kv_transfer_params") or {})
    kv_params["ranges"] = declaration.wire_ranges()
    body["kv_transfer_params"] = kv_params
    record = {
        "schema": DECLARATION_SCHEMA,
        "status": "declared",
        "tokenizer": renderer.identity,
        "template_sha": renderer.template_sha,
        "template_kwargs": {k: v for k, v in template_kwargs.items() if k != "tools"},
        "prompt_token_count": declaration.prompt_token_count,
        "ranges": [r.to_record() for r in declaration.ranges],
        "unlocated_messages": declaration.unlocated_messages,
    }
    return {**call_kwargs, "extra_body": body}, record


@contextmanager
def declared_request(
    *,
    model: str,
    messages: Any,
    lm_kwargs: dict[str, Any],
    call_kwargs: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """Yield the call kwargs to use; expose the declaration for the ``lm.call`` record.

    A no-op (kwargs unchanged, no record) when ``provenance.attention`` is off.
    """
    from clio_agent.provenance_config import attention_capture_enabled  # noqa: PLC0415

    if not attention_capture_enabled() or not isinstance(messages, list):
        yield call_kwargs
        return
    new_kwargs, record = build_declaration(
        model=str(model or ""),
        messages=messages,
        lm_kwargs=lm_kwargs,
        call_kwargs=call_kwargs,
    )
    token = _current.set(record)
    message_token = _messages.set(messages if record["status"] == "declared" else None)
    try:
        yield new_kwargs
    finally:
        _messages.reset(message_token)
        _current.reset(token)
