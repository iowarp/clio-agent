"""Revision-bound, bidirectional attention lookup over shared content references."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any, Literal

from clio_schemas.attention import AttentionProfile, display_intensities
from clio_schemas.connected_resources import ContentSelection

from clio_agent.gact.attention.capture import load_capture
from clio_agent.gact.attention.chat_render import ChatRenderer
from clio_agent.gact.attention.lm_calls import LmCall
from clio_agent.gact.attention.profiles import reduce_steps
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.selection import locate_output, output_steps
from clio_agent.gact.attention.service import SelectionRequest, explain_selection, resolve_generated
from clio_agent.gact.attention.store import AttentionStore
from clio_agent.gact.attention.transcript_map import TranscriptText, anchor_texts, transcript_texts


def _resolve_reference(
    messages: list[Any], session_id: str, ref: ContentSelection
) -> tuple[TranscriptText, int, int]:
    if ref.session_id != session_id:
        raise AttentionUnavailable("message_not_found", "reference belongs to another session")
    if ref.selection.kind not in {"text", "whole"} or ref.artifact_ref:
        raise AttentionUnavailable(
            "content_coordinates_unavailable",
            "image patches, structured rows and artifact bytes need recorded coordinate mappings",
        )
    text = next(
        (
            item
            for item in transcript_texts(messages)
            if (item.message_id, item.part_id, item.field)
            == (ref.message_id, ref.part_id, ref.field)
        ),
        None,
    )
    if text is None:
        raise AttentionUnavailable("content_coordinates_unavailable", "no captured text field")
    if text.content_revision != ref.content_revision:
        raise AttentionUnavailable("content_revision_changed", "source revision differs")
    if ref.call_id and ref.call_id != text.call_id:
        raise AttentionUnavailable("selection_not_located", "tool call identity differs")
    start, end = (
        (ref.selection.start, ref.selection.end)
        if ref.selection.kind == "text"
        else (0, len(text.text))
    )
    if end > len(text.text):
        raise AttentionUnavailable("selection_not_located", "selection exceeds the source revision")
    return text, start, end


def _request(
    ref: ContentSelection, start: int, end: int, profile: AttentionProfile
) -> SelectionRequest:
    return SelectionRequest(
        message_id=ref.message_id,
        part_id=ref.part_id,
        field=ref.field,
        content_revision=ref.content_revision,
        start=start,
        end=end,
        profile=profile,
    )


def _before_call(messages: list[Any], call: LmCall) -> list[TranscriptText]:
    """Exclude later transcript content, including later parts in this same turn."""
    texts: list[TranscriptText] = []
    for message in messages:
        if message.turn_id != call.turn_id or message.role != "assistant":
            texts.extend(transcript_texts([message]))
            continue
        # Earlier tool results/thoughts in this turn are usable only when the
        # producing output establishes a unique boundary in its ordered parts.
        for part in message.parts:
            generated = [
                item
                for item in transcript_texts([message])
                if item.part_id == part.id and item.field in {"text", "thought", "input"}
            ]
            for item in generated:
                try:
                    locate_output([call], item.text, 0, len(item.text))
                except AttentionUnavailable:
                    continue
                prior_ids = {p.id for p in message.parts[: message.parts.index(part)]}
                texts.extend(
                    item for item in transcript_texts([message]) if item.part_id in prior_ids
                )
                return texts
        return texts
    # A call with no matching transcript turn cannot prove temporal ownership.
    return []


def _source_view(
    *,
    messages: list[Any],
    call: LmCall,
    selections: list[tuple[ContentSelection, TranscriptText, int, int]],
    store: AttentionStore,
    renderer_for: Callable[[str], ChatRenderer],
    profile: AttentionProfile,
) -> dict[str, Any] | None:
    eligible = _before_call(messages, call)
    keys = {(text.message_id, text.part_id, text.field) for text in eligible}
    if not any((text.message_id, text.part_id, text.field) in keys for _, text, _, _ in selections):
        return None
    capture = load_capture(call, store, renderer_for)
    anchors = anchor_texts(eligible, capture.encoded, capture.sections)
    positions: set[int] = set()
    matched: list[dict[str, Any]] = []
    for ref, text, start, end in selections:
        anchor = next((a for a in anchors if a.text == text), None)
        if anchor is None:
            continue
        lo, hi = capture.encoded.token_span(*anchor.hit.to_haystack(start, end))
        positions.update(range(lo, hi))
        matched.append(ref.model_dump())
    if not positions:
        return None
    span = output_steps(
        capture.renderer, call.content or "", 0, len(call.content or ""), capture.summary.record
    )
    if len(span.steps) > 4096:
        raise AttentionUnavailable("selection_too_large", "model output exceeds 4096 tokens")
    steps = store.steps(capture.summary, span.steps[0], span.steps[-1])
    reduction = reduce_steps(steps, capture.summary.prompt_tokens, profile)
    output = capture.renderer.encode(call.content or "")
    rows: list[dict[str, Any]] = []
    for step, weight in zip(steps, reduction.weights, strict=True):
        values = reduce_steps([step], capture.summary.prompt_tokens, profile)
        lo, hi = output.offsets[step.token_index]
        rows.append(
            {
                "step": step.step,
                "start": lo,
                "end": hi,
                "text": (call.content or "")[lo:hi],
                "score": values.block_score(sorted(positions)) * weight,
                "aggregation_weight": weight,
                "mass": sum(values.mean_mass[p] for p in positions),
                "retained_tokens": sum(values.retained_steps[p] > 0 for p in positions),
            }
        )
    intensity = display_intensities([row["score"] for row in rows], profile)
    # Omitted rows are counted explicitly. Ranking is for inspection only; full
    # call aggregation above retains every generated token exactly once.
    ranked = sorted(zip(rows, intensity, strict=True), key=lambda pair: -pair[0]["score"])
    generated = []
    for message in messages:
        if message.turn_id != call.turn_id or message.role != "assistant":
            continue
        for text in transcript_texts([message]):
            if text.field not in {"text", "thought", "input"}:
                continue
            try:
                locate_output([call], text.text, 0, len(text.text))
            except AttentionUnavailable:
                continue
            generated.append(
                ContentSelection.model_validate(
                    {
                        "session_id": call.session_id,
                        "message_id": text.message_id,
                        "part_id": text.part_id,
                        "field": text.field,
                        "content_revision": text.content_revision,
                        "call_id": text.call_id or None,
                        "selection": {"kind": "whole"},
                    }
                ).model_dump()
            )
    return {
        "kind": "source",
        "available": True,
        "lm_call_id": call.event_id,
        "turn_id": call.turn_id,
        "response_id": call.response_id,
        "request_id": capture.summary.request_id,
        "capture_sha256": capture.summary.record.sha256,
        "profile": profile.model_dump(),
        "profile_revision": profile.revision,
        "sources": matched,
        "generated_references": generated,
        "prompt_positions": sorted(positions),
        "score": reduction.block_score(sorted(positions)),
        "mass": sum(reduction.mean_mass[p] for p in positions),
        "residual": reduction.residual,
        "step_count": len(steps),
        "tokens": [{**row, "intensity": scale} for row, scale in ranked[:128]],
        "omitted_tokens": max(0, len(rows) - 128),
        "mass_semantics": "uniform mean over unique generated steps",
        "display_semantics": "intensity scaled within this call; missing sparse entries are unretained",
    }


def lookup_attention(
    *,
    session_id: str,
    messages: list[Any],
    calls: list[LmCall],
    store: AttentionStore,
    renderer_for: Callable[[str], ChatRenderer],
    selections: list[ContentSelection],
    direction: Literal["generated_to_source", "source_to_generation"],
    profile: AttentionProfile,
    cursor: int = 0,
    limit: int = 16,
) -> dict[str, Any]:
    """Deduplicate references and aggregate once per capture, with explicit gaps.

    Reverse lookup pages recorded calls rather than truncating them silently.
    Captures remain local to the configured CLIO host; this reader never SSHs.
    """
    unique = {ref.model_dump_json(): ref for ref in selections}
    resolved = []
    unavailable = []
    for ref in unique.values():
        try:
            text, start, end = _resolve_reference(messages, session_id, ref)
            resolved.append((ref, text, start, end))
        except AttentionUnavailable as exc:
            unavailable.append({"selection": ref.model_dump(), **exc.to_wire()})
    views = []
    next_cursor: int | None = None
    if direction == "generated_to_source":
        groups: dict[str, list[SelectionRequest]] = defaultdict(list)
        for ref, _, start, end in resolved:
            request = _request(ref, start, end, profile)
            try:
                selected = resolve_generated(messages, calls, request)
                groups[selected.span.call.event_id].append(request)
            except AttentionUnavailable as exc:
                unavailable.append({"selection": ref.model_dump(), **exc.to_wire()})
        for requests in groups.values():
            try:
                views.append(
                    {
                        "kind": "generated",
                        **explain_selection(
                            messages=messages,
                            calls=calls,
                            store=store,
                            renderer_for=renderer_for,
                            request=requests[0],
                            additional_requests=tuple(requests[1:]),
                        ),
                    }
                )
            except AttentionUnavailable as exc:
                unavailable.append(exc.to_wire())
    else:
        ordered = sorted(calls, key=lambda call: (call.occurred_at, call.event_id))
        for call in ordered[cursor : cursor + limit]:
            try:
                view = _source_view(
                    messages=messages,
                    call=call,
                    selections=resolved,
                    store=store,
                    renderer_for=renderer_for,
                    profile=profile,
                )
                if view is not None:
                    views.append(view)
            except AttentionUnavailable as exc:
                unavailable.append({"lm_call_id": call.event_id, **exc.to_wire()})
        next_cursor = cursor + limit if cursor + limit < len(ordered) else None
        matched_refs = {
            ContentSelection.model_validate(ref).model_dump_json()
            for view in views
            for ref in view["sources"]
        }
        for ref, _, _, _ in resolved:
            if ref.model_dump_json() not in matched_refs:
                unavailable.append(
                    {
                        "selection": ref.model_dump(),
                        **AttentionUnavailable(
                            "content_coordinates_unavailable",
                            "no unique compatible source passage in the calls checked on this page",
                        ).to_wire(),
                    }
                )
    return {
        "schema": "clio.attention.lookup.v1",
        "direction": direction,
        "profile": profile.model_dump(),
        "profile_revision": profile.revision,
        "selection_count": len(unique),
        "views": views,
        "unavailable": unavailable,
        "next_cursor": next_cursor,
    }
