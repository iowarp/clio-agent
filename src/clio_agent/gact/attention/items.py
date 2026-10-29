"""One attention vector per answer item, laid over the whole rendered prompt by line.

This is the SPOTTER view (spotter-ai-genesis/experiments
``bench/opal/analytics``: ``attn_analytics.py`` + ``render_heatmap.py``). An
answer is read as items -- explicit spans, or one selection split at a
delimiter the person chose (``NIL 373; DIVERGES`` at ``;`` is two items). Each
item is reduced on its own (never unioned with another), with the profile's
weights counted from the item's first decode step. The prompt is shown line by
line: a line's value is the max over its tokens, and each item's column is
scaled to that item's own max on a 10-level ramp, so a column shows where the
item looked, not how much attention it drew compared with another item.

The export carries ``attn_analytics.py``'s CSV columns, so ``render_heatmap.py``
reads it as-is.
"""

from __future__ import annotations

import csv
import io
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
from clio_schemas.attention import AttentionProfile

from clio_agent.gact.attention.capture import AlignedCapture, load_capture
from clio_agent.gact.attention.chat_render import ChatRenderer
from clio_agent.gact.attention.lm_calls import LmCall
from clio_agent.gact.attention.profiles import DEFAULT_PROFILE, reduce_steps
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.selection import output_steps, prompt_opens_thinking
from clio_agent.gact.attention.service import (
    GeneratedSelection,
    SelectionRequest,
    resolve_generated,
)
from clio_agent.gact.attention.store import AttentionStore

ITEMS_SCHEMA = "clio.attention.items.v1"
#: ``render_heatmap.py``'s ramp: level 0 (blank) .. 9.
RAMP = " .:-=+*#%@"
LEVELS = len(RAMP) - 1
#: ``attn_analytics.py``'s CSV columns, in order.
CSV_COLUMNS = (
    "chain_id",
    "round_no",
    "task_type",
    "model",
    "segment_index",
    "segment_text",
    "decode_steps",
    "n_prompt_tokens",
    "prompt_token_ids",
    "scores",
)
MAX_ITEMS = 32


@dataclass(frozen=True)
class ItemVector:
    """One item's decode steps and its score per prompt token."""

    selection: GeneratedSelection
    steps: list[int]
    scores: np.ndarray


@dataclass(frozen=True)
class ItemsView:
    """Every item of one model call, reduced against one aligned capture."""

    capture: AlignedCapture
    profile: AttentionProfile
    items: list[ItemVector]


def published_scores(scores: Any) -> np.ndarray:
    """Scores at the CSV's 6 significant digits, so the view and the export agree.

    ``render_heatmap.py`` draws from the CSV, and a line level sits exactly on a
    rounding tie whenever saturated ``val_all_max`` sums meet the column max
    (``9 * 1.0 / 2.0``): computing levels from the published digits keeps every
    cell equal to the reference drawing.
    """
    return np.array([float(f"{x:.6g}") for x in scores], dtype=np.float64)


def split_spans(text: str, start: int, end: int, delimiter: str) -> list[tuple[int, int]]:
    """``[start, end)`` cut at ``delimiter``; each piece trimmed, empty pieces dropped."""
    if not delimiter:
        return [(start, end)]
    spans: list[tuple[int, int]] = []
    cursor = start
    while cursor <= end:
        cut = text.find(delimiter, cursor, end)
        stop = end if cut < 0 else cut
        lo, hi = cursor, stop
        while lo < hi and text[lo].isspace():
            lo += 1
        while hi > lo and text[hi - 1].isspace():
            hi -= 1
        if hi > lo:
            spans.append((lo, hi))
        if cut < 0:
            break
        cursor = cut + len(delimiter)
    return spans


def _resolve_items(
    messages: list[Any], calls: list[LmCall], requests: list[SelectionRequest], delimiter: str
) -> list[GeneratedSelection]:
    out: list[GeneratedSelection] = []
    for request in requests:
        whole = resolve_generated(messages, calls, request)
        for lo, hi in split_spans(whole.text, whole.start, whole.end, delimiter):
            piece = replace(
                whole.request, part_id=whole.part.id, start=lo, end=hi, text="", content_revision=""
            )
            out.append(resolve_generated(messages, calls, piece))
    if not out:
        raise AttentionUnavailable("selection_not_located", "the selection holds no items")
    if len(out) > MAX_ITEMS:
        raise AttentionUnavailable("selection_too_large", f"select at most {MAX_ITEMS} items")
    if any(item.span.call.event_id != out[0].span.call.event_id for item in out):
        raise AttentionUnavailable(
            "selection_ambiguous", "the items were produced by different model calls"
        )
    return out


def reduce_items(
    *,
    messages: list[Any],
    calls: list[LmCall],
    store: AttentionStore,
    renderer_for: Callable[[str], ChatRenderer],
    requests: list[SelectionRequest],
    delimiter: str = "",
    profile: AttentionProfile = DEFAULT_PROFILE,
) -> ItemsView:
    """Resolve, split and reduce every item, each on its own decode steps."""
    selected = _resolve_items(messages, calls, requests, delimiter)
    call = selected[0].span.call
    capture = load_capture(call, store, renderer_for)
    summary = capture.summary
    opened = prompt_opens_thinking(capture.encoded.text)
    items: list[ItemVector] = []
    for item in selected:
        span = output_steps(
            capture.renderer,
            call.content or "",
            item.span.out_lo,
            item.span.out_hi,
            summary.record,
            reasoning=call.reasoning,
            opened=opened,
            source=item.span.source,
        ).steps
        rows = store.steps(summary, span[0], span[-1])
        reduction = reduce_steps(rows, summary.prompt_tokens, profile)
        items.append(ItemVector(item, span, published_scores(reduction.scores)))
    return ItemsView(capture, profile, items)


def prompt_lines(view: ItemsView) -> list[dict[str, Any]]:
    """The rendered prompt by line, with each item's line max and 0..9 level."""
    encoded, sections = view.capture.encoded, view.capture.sections
    lines = encoded.text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    starts = [0]
    for line in lines[:-1]:
        starts.append(starts[-1] + len(line) + 1)
    total = len(encoded.ids)
    line_of = np.array(
        [min(bisect_right(starts, a) - 1, len(lines) - 1) for a, _ in encoded.offsets[:total]],
        dtype=np.int64,
    )
    columns = []
    for item in view.items:
        per = np.zeros(len(lines), dtype=np.float64)
        np.maximum.at(per, line_of, item.scores[:total])
        top = float(per.max()) if len(per) else 0.0
        columns.append((per, [min(LEVELS, round(LEVELS * x / (top or 1.0))) for x in per]))
    token_lo = np.searchsorted(line_of, np.arange(len(lines)), side="left")
    token_hi = np.searchsorted(line_of, np.arange(len(lines)), side="right")
    section_lo = [s.lo for s in sections]
    out = []
    for index, text in enumerate(lines):
        lo, hi = int(token_lo[index]), int(token_hi[index])
        section = bisect_right(section_lo, lo) - 1 if hi > lo else None
        out.append(
            {
                "index": index,
                "text": text,
                "token_lo": lo,
                "token_hi": hi,
                "section": section,
                "domain": sections[section].domain if section is not None else None,
                "values": [float(f"{per[index]:.6g}") for per, _ in columns],
                "levels": [levels[index] for _, levels in columns],
            }
        )
    return out


def items_payload(view: ItemsView, *, include_scores: bool = False) -> dict[str, Any]:
    """The wire view: items, the prompt by line, and the capture identity."""
    capture, summary = view.capture, view.capture.summary
    lines = prompt_lines(view)
    items = []
    for index, item in enumerate(view.items):
        sel = item.selection
        row: dict[str, Any] = {
            "index": index,
            "text": sel.text[sel.start : sel.end],
            "message_id": sel.message.id,
            "part_id": sel.part.id,
            "field": sel.request.field,
            "start": sel.start,
            "end": sel.end,
            "decode_steps": item.steps,
            "column_max": max((line["values"][index] for line in lines), default=0.0),
            "score_max": float(item.scores.max()) if len(item.scores) else 0.0,
        }
        if include_scores:
            row["scores"] = [float(f"{x:.6g}") for x in item.scores]
        items.append(row)
    return {
        "schema": ITEMS_SCHEMA,
        "available": True,
        "message_id": view.items[0].selection.message.id,
        "turn_id": view.items[0].selection.message.turn_id,
        "response_id": capture.call.response_id,
        "request_id": summary.request_id,
        "lm_call_id": capture.call.event_id,
        "capture_sha256": summary.record.sha256,
        "tokenizer": capture.tokenizer,
        "profile": view.profile.model_dump(),
        "profile_revision": view.profile.revision,
        "prompt_tokens": summary.prompt_tokens,
        "top_pct": summary.top_pct,
        "health": summary.health,
        "ramp": RAMP,
        "display_semantics": {
            "line_value": "max over the line's tokens",
            "scale": "each item to its own max",
            "missing": "unretained, not measured zero",
        },
        "sections": [
            {"lo": s.lo, "hi": s.hi, "domain": s.domain, "label": s.label} for s in capture.sections
        ],
        "items": items,
        "lines": lines,
        **(
            {"prompt_token_ids": [int(t) for t in summary.prompt_token_ids]}
            if include_scores
            else {}
        ),
    }


def round_number(messages: list[Any], message_id: str) -> int:
    """User messages up to and including ``message_id``'s turn (the answer's round)."""
    count = 0
    for message in messages:
        count += message.role == "user"
        if message.id == message_id:
            return count
    raise AttentionUnavailable("message_not_found", message_id)


def items_csv(
    view: ItemsView, *, chain_id: str, round_no: str, task_type: str = "", model: str = ""
) -> str:
    """``attn_analytics.py``'s CSV: one row per item, scores per prefill token."""
    summary = view.capture.summary
    ids = " ".join(str(int(t)) for t in summary.prompt_token_ids)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(CSV_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for index, item in enumerate(view.items):
        sel = item.selection
        writer.writerow(
            {
                "chain_id": chain_id,
                "round_no": round_no,
                "task_type": task_type,
                "model": model or view.capture.tokenizer,
                "segment_index": index,
                "segment_text": sel.text[sel.start : sel.end],
                "decode_steps": ",".join(map(str, item.steps)),
                "n_prompt_tokens": summary.prompt_tokens,
                "prompt_token_ids": ids,
                "scores": " ".join(f"{x:.6g}" for x in item.scores),
            }
        )
    return buffer.getvalue()
