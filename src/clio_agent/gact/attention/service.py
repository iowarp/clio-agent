"""Compose the attention answer for one selection (pure; no app state).

Pipeline -- every step either succeeds or raises a typed reason:

1. the selected text belongs to a generated part of an assistant message;
2. the turn's ``lm.call`` that produced it (:mod:`.selection`);
3. the connector's record for that call, by response id, and its SafeTensors
   file (:mod:`.store`);
4. the prompt re-rendered with the model's tokenizer and checked against the
   captured prompt length / token ids;
5. sections = the ranges CLIO declared on the request (checked verbatim against
   the captured segments), or -- for a call made before declaration existed --
   the same ranges derived now from the recorded messages, marked ``derived``;
6. the selected tokens' decode steps, their sparse rows reduced to per-token
   and per-section mass (:mod:`.aggregate`);
7. that mass placed on transcript characters (:mod:`.transcript_map`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
from clio_schemas.attention import UNIFORM_MEAN, AttentionProfile, display_intensities

from clio_agent.gact.attention import aggregate as agg
from clio_agent.gact.attention.chat_render import ChatRenderer, Encoded
from clio_agent.gact.attention.lm_calls import LmCall, turn_calls
from clio_agent.gact.attention.profiles import reduce_steps
from clio_agent.gact.attention.ranges import DeclaredRange, declare_ranges
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.rendered import find_rendered
from clio_agent.gact.attention.selection import locate_output, output_steps
from clio_agent.gact.attention.store import AttentionStore
from clio_agent.gact.attention.transcript_map import (
    anchor_texts,
    block_stats,
    heat_runs,
    transcript_texts,
)

RESPONSE_SCHEMA = "clio.attention.v1"
#: Selected tokens returned with a per-token drill-down (the rest are aggregated).
DRILLDOWN_TOKENS = 64
DRILLDOWN_TOP = 5
_CONTENT_DOMAINS = (
    "system",
    "user",
    "tool_definitions",
    "tool_call",
    "tool_result",
    "thinking",
    "assistant_output",
    "other",
)


@dataclass(frozen=True)
class SelectionRequest:
    """What the user selected: a char span over one generated text."""

    message_id: str
    part_id: str = ""
    field: str = "text"
    start: int | None = None
    end: int | None = None
    #: The rendered text the person selected; located in the message's parts when
    #: no explicit span is given (:mod:`.rendered`).
    text: str = ""
    profile: AttentionProfile = UNIFORM_MEAN
    content_revision: str = ""


def _selected_text(messages: list[Any], request: SelectionRequest) -> tuple[Any, int, Any, str]:
    index = next((i for i, m in enumerate(messages) if m.id == request.message_id), -1)
    if index < 0:
        raise AttentionUnavailable("message_not_found", request.message_id)
    message = messages[index]
    if message.role != "assistant":
        raise AttentionUnavailable(
            "message_not_generated", f"{message.role} messages have no attention rows"
        )
    for part in message.parts:
        if request.part_id and part.id != request.part_id:
            continue
        text = part.thought if request.field == "thought" else part.text
        if text:
            if (
                request.content_revision
                and hashlib.sha256(text.encode("utf-8")).hexdigest() != request.content_revision
            ):
                raise AttentionUnavailable(
                    "content_revision_changed", "the selected content changed"
                )
            return message, index, part, text
    raise AttentionUnavailable("message_not_generated", "no generated text in that part")


def _resolve_rendered(messages: list[Any], request: SelectionRequest) -> SelectionRequest:
    """Turn a rendered-text selection into a part/field/span over the source text."""
    if not request.text or request.start is not None or request.end is not None:
        return request
    message = next((m for m in messages if m.id == request.message_id), None)
    if message is None:
        raise AttentionUnavailable("message_not_found", request.message_id)
    # A supplied part identity is authoritative. Legacy text-only clients must
    # have a unique match; display order cannot establish which text was selected.
    parts = [p for p in reversed(message.parts) if not request.part_id or p.id == request.part_id]
    matches = []
    for field in ("text", "thought"):
        if request.part_id and field != request.field:
            continue
        for part in parts:
            source = part.text if field == "text" else part.thought
            span = find_rendered(source or "", request.text)
            if span is not None:
                matches.append(
                    replace(request, part_id=part.id, field=field, start=span[0], end=span[1])
                )
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AttentionUnavailable(
            "selection_ambiguous", "the selected text occurs in multiple parts"
        )
    raise AttentionUnavailable(
        "selection_not_located", "the selected text is not in this message's generated text"
    )


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


def explain_selection(
    *,
    messages: list[Any],
    calls: list[LmCall],
    store: AttentionStore,
    renderer_for: Callable[[str], ChatRenderer],
    request: SelectionRequest,
) -> dict[str, Any]:
    """The full attention payload for one selection (see module docstring)."""
    request = _resolve_rendered(messages, request)
    message, index, part, part_text = _selected_text(messages, request)
    start = 0 if request.start is None else max(0, request.start)
    end = len(part_text) if request.end is None else min(len(part_text), request.end)
    if end <= start:
        raise AttentionUnavailable("selection_not_located", "empty selection")
    candidates = turn_calls(calls, message.turn_id)
    if not candidates:
        raise AttentionUnavailable("lm_call_not_found", f"no lm.call for turn {message.turn_id}")
    span = locate_output(candidates, part_text, start, end)
    call = span.call
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

    step_span = output_steps(
        renderer,
        call.content or "",
        span.out_lo,
        span.out_hi,
        summary.record,
    )
    steps = store.steps(summary, step_span.steps[0], step_span.steps[-1])
    reduction = reduce_steps(steps, summary.prompt_tokens, request.profile)
    mass = agg.mass_from_steps(steps, summary.prompt_tokens, reduction=reduction)

    shares = agg.section_shares(sections, mass)
    peaks = agg.section_peaks(sections, mass)
    # Everything the prompt could hold: earlier messages plus this message's own
    # earlier parts (the turn's tool calls/results), minus the selected output.
    texts = [
        t
        for t in transcript_texts(messages[: index + 1])
        if not (t.message_id == message.id and t.part_id == part.id and t.field == request.field)
    ]
    anchors = anchor_texts(texts, encoded, sections)
    block_scores = [reduction.block_score(range(a.token_lo, a.token_hi)) for a in anchors]
    intensities = display_intensities(block_scores, request.profile)
    token_intensities = display_intensities(reduction.scores, request.profile)
    domains = agg.domain_shares(sections, shares)
    return {
        "schema": RESPONSE_SCHEMA,
        "available": True,
        "message_id": message.id,
        "turn_id": message.turn_id,
        "response_id": call.response_id,
        "request_id": summary.request_id,
        "lm_call_id": call.event_id,
        "profile": request.profile.model_dump(),
        "profile_revision": request.profile.revision,
        "profile_weights": list(reduction.weights),
        "mass_semantics": "uniform mean over unique selected captured steps",
        "display_semantics": {
            "metric": request.profile.metric,
            "block_scale": max(block_scores, default=0.0),
            "token_scale": max(reduction.scores, default=0.0),
            "scope": "this selected item",
            "missing": "unretained, not measured zero",
        },
        "selection": {
            "part_id": part.id,
            "content_revision": hashlib.sha256(part_text.encode("utf-8")).hexdigest(),
            "field": request.field,
            "start": start,
            "end": end,
            "text": part_text[start:end],
            "output_encoding": span.encoding,
            "matching_calls": span.candidates,
            "token_range": [step_span.token_lo, step_span.token_hi],
            "steps": [step_span.steps[0], step_span.steps[-1]],
            "step_count": len(steps),
            "output_tokens": step_span.output_tokens,
            "token_index_base": "produced",
        },
        "top_pct": summary.top_pct,
        "residual": mass.residual,
        "health": summary.health,
        "sections_source": sections_source,
        "sections": [
            {
                "lo": s.lo,
                "hi": s.hi,
                "domain": s.domain,
                "label": s.label,
                "share": share,
                "peak": peak,
            }
            for s, share, peak in zip(sections, shares, peaks, strict=True)
        ],
        "sources": [
            {"domain": d, "share": domains[d]}
            for d in _CONTENT_DOMAINS + ("template",)
            if d in domains
        ],
        "flags": _flags(domains),
        "unmapped_content": [
            {
                "message_id": text.message_id,
                "part_id": text.part_id,
                "field": text.field,
                "content_revision": text.content_revision,
                "reason": "No unique compatible passage in the captured prompt.",
            }
            for text in texts
            if not any(anchor.text is text for anchor in anchors)
        ],
        "blocks": [
            {
                "message_id": a.text.message_id,
                "part_id": a.text.part_id,
                "call_id": a.text.call_id,
                "content_revision": a.text.content_revision,
                "field": a.text.field,
                "kind": a.text.kind,
                "section": a.section_index,
                **block_stats(a, mass),
                "runs": heat_runs(a, encoded, mass),
                "score": score,
                "intensity": intensity,
                "retained_tokens": sum(
                    count > 0 for count in reduction.retained_steps[a.token_lo : a.token_hi]
                ),
                "display_runs": heat_runs(a, encoded, mass, values=token_intensities),
            }
            for a, score, intensity in zip(anchors, block_scores, intensities, strict=True)
        ],
        "tokens": _drilldown(renderer, call.content or "", steps, sections, encoded),
    }


def _flags(domains: dict[str, float]) -> list[dict[str, Any]]:
    content = {d: v for d, v in domains.items() if d in _CONTENT_DOMAINS}
    if content and max(content, key=content.__getitem__) == "tool_result":
        return [{"kind": "tool_result_dominant", "share": content["tool_result"]}]
    return []


def _drilldown(
    renderer: ChatRenderer,
    content: str,
    steps: list[Any],
    sections: list[agg.Section],
    prompt: Encoded,
) -> list[dict[str, Any]]:
    out_enc = renderer.encode(content)
    starts = [s.lo for s in sections]
    rows: list[dict[str, Any]] = []
    for step in steps[:DRILLDOWN_TOKENS]:
        # Ranked by the mean over (layer, head): the max saturates at ~1.0 on many
        # positions (some head always spikes), so it cannot order them.
        order = np.argsort(-step.mean, kind="stable")[:DRILLDOWN_TOP]
        top = []
        for k in order:
            pos = int(step.pos[k])
            sec = int(np.searchsorted(starts, pos, side="right") - 1)
            a, b = prompt.offsets[pos] if 0 <= pos < len(prompt.offsets) else (0, 0)
            top.append(
                {
                    "pos": pos,
                    "max": float(step.max[k]),
                    "mean": float(step.mean[k]),
                    "section": sec,
                    "text": prompt.text[a:b],
                }
            )
        tidx = step.token_index
        a, b = out_enc.offsets[tidx] if 0 <= tidx < len(out_enc.offsets) else (0, 0)
        rows.append(
            {
                "step": step.step,
                "token_index": tidx,
                "text": content[a:b],
                "residual": step.residual,
                "top": top,
            }
        )
    return rows
