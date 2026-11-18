"""Load and align one local capture before either direction of attention lookup."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from clio_agent.gact.attention import aggregate as agg
from clio_agent.gact.attention.chat_render import ChatRenderer, Encoded
from clio_agent.gact.attention.contract import AttentionSummary
from clio_agent.gact.attention.lm_calls import LmCall
from clio_agent.gact.attention.ranges import (
    DeclaredRange,
    declare_ranges,
    render_without_tools,
)
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.store import AttentionStore


@dataclass(frozen=True)
class AlignedCapture:
    """Verified captured tokens and the renderer/sections that match them exactly."""

    call: LmCall
    summary: AttentionSummary
    renderer: ChatRenderer
    encoded: Encoded
    sections: list[agg.Section]
    sections_source: str
    #: The tokenizer identity the prompt was re-rendered with (HF id or local path).
    tokenizer: str = ""


def _ranges_from_declaration(declaration: dict[str, Any]) -> list[DeclaredRange]:
    return [
        DeclaredRange(
            lo=int(r["lo"]),
            hi=int(r["hi"]),
            domain=str(r["domain"]),
            label=str(r["label"]),
            message_index=None if r.get("message_index") is None else int(r["message_index"]),
            char_lo=int(r["char_lo"]),
            char_hi=int(r["char_hi"]),
        )
        for r in declaration.get("ranges") or []
    ]


def _check_prompt(encoded: Encoded, summary: Any, tokenizer: str = "") -> None:
    """The re-rendered prompt must be the captured one, token for token."""
    ids = np.asarray(encoded.ids, dtype=np.int64)
    captured = np.asarray(summary.prompt_token_ids, dtype=np.int64)
    if len(ids) == summary.prompt_tokens and np.array_equal(ids, captured):
        return
    common = min(len(ids), len(captured))
    differ = np.flatnonzero(ids[:common] != captured[:common])
    first = int(differ[0]) if differ.size else common
    detail = (
        f"re-rendered prompt has {len(ids)} tokens, capture scored {summary.prompt_tokens}"
        if len(ids) != summary.prompt_tokens
        else "re-rendered prompt token ids differ from the capture"
    )
    raise AttentionUnavailable(
        "range_alignment_mismatch",
        f"{detail} (first difference at token {first}); the tokenizer or chat template "
        "used to re-render the prompt is likely not the one the model used",
        {
            "prompt_tokens": len(ids),
            "captured_prompt_tokens": int(summary.prompt_tokens),
            "first_difference": first,
            "tokenizer": tokenizer,
        },
    )


def _declared_tools(declaration: dict[str, Any]) -> list[Any]:
    """The tool schemas the declaration rendered with, in their original key order.

    ``tools_json`` keeps the order the template saw; a legacy ``tools`` list may
    have been key-sorted by the trace writer, which then fails the token check
    below with a typed mismatch instead of a wrong alignment.
    """
    if text := declaration.get("tools_json"):
        return list(json.loads(text))
    return list(declaration.get("tools") or [])


def load_capture(
    call: LmCall, store: AttentionStore, renderer_for: Callable[[str], ChatRenderer]
) -> AlignedCapture:
    """Read local capture bytes and refuse mismatched token or section coordinates."""
    if not call.model.startswith("hosted_vllm/"):
        raise AttentionUnavailable("provider_not_vllm", call.model)
    if call.messages is None:
        raise AttentionUnavailable("lm_call_payload_unavailable", "input messages not recorded")
    declaration = call.declaration or {}
    if declaration.get("status") == "not_declared" and declaration.get("reason") == (
        "provider_not_vllm"
    ):
        raise AttentionUnavailable("provider_not_vllm", call.model)

    summary = store.summary_for(call.response_id)
    identity = str(declaration.get("tokenizer") or "") or store.workflow_tokenizer(
        summary.record.workflow_id
    )
    if not identity:
        raise AttentionUnavailable(
            "attention_tokenizer_unavailable", "no tokenizer recorded on the call or workflow"
        )
    renderer = renderer_for(identity)
    template_kwargs = dict(declaration.get("template_kwargs") or {})
    tools = _declared_tools(declaration)
    if tools:
        template_kwargs["tools"] = tools
    encoded = renderer.render_encoded(call.messages, template_kwargs)
    used = str(getattr(renderer, "identity", "") or identity)
    _check_prompt(encoded, summary, used if used == identity else f"{used} (recorded: {identity})")

    if declaration.get("status") == "declared":
        ranges = _ranges_from_declaration(declaration)
        if not agg.declared_ranges_match(ranges, summary):
            raise AttentionUnavailable(
                "range_alignment_mismatch", "declared ranges are not verbatim captured segments"
            )
        sections_source = "declared"
    else:
        without_tools = render_without_tools(renderer, call.messages, template_kwargs)
        ranges = declare_ranges(call.messages, encoded, tools, without_tools).ranges
        sections_source = "derived"
    sections = agg.sections_with_gaps(ranges, summary.prompt_tokens)

    return AlignedCapture(
        call, summary, renderer, encoded, sections, sections_source, tokenizer=identity
    )
