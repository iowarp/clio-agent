"""Load and align one local capture before either direction of attention lookup."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from clio_agent.gact.attention import aggregate as agg
from clio_agent.gact.attention.chat_render import ChatRenderer, Encoded
from clio_agent.gact.attention.contract import AttentionSummary
from clio_agent.gact.attention.lm_calls import LmCall
from clio_agent.gact.attention.ranges import DeclaredRange, declare_ranges
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


def _ranges_from_declaration(declaration: dict[str, Any]) -> list[DeclaredRange]:
    return [
        DeclaredRange(
            lo=int(r["lo"]),
            hi=int(r["hi"]),
            domain=str(r["domain"]),
            label=str(r["label"]),
            message_index=int(r["message_index"]),
            char_lo=int(r["char_lo"]),
            char_hi=int(r["char_hi"]),
        )
        for r in declaration.get("ranges") or []
    ]


def _check_prompt(encoded: Encoded, summary: Any) -> None:
    if len(encoded.ids) != summary.prompt_tokens:
        raise AttentionUnavailable(
            "range_alignment_mismatch",
            f"re-rendered prompt has {len(encoded.ids)} tokens, capture scored "
            f"{summary.prompt_tokens}",
        )
    if not np.array_equal(np.asarray(encoded.ids, dtype=np.int64), summary.prompt_token_ids):
        raise AttentionUnavailable(
            "range_alignment_mismatch", "re-rendered prompt token ids differ from the capture"
        )


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
    encoded = renderer.render_encoded(call.messages, declaration.get("template_kwargs") or {})
    _check_prompt(encoded, summary)

    if declaration.get("status") == "declared":
        ranges = _ranges_from_declaration(declaration)
        if not agg.declared_ranges_match(ranges, summary):
            raise AttentionUnavailable(
                "range_alignment_mismatch", "declared ranges are not verbatim captured segments"
            )
        sections_source = "declared"
    else:
        ranges = declare_ranges(call.messages, encoded).ranges
        sections_source = "derived"
    sections = agg.sections_with_gaps(ranges, summary.prompt_tokens)

    return AlignedCapture(call, summary, renderer, encoded, sections, sections_source)
