"""Bind reviewer capture evidence to real parent-transcript identities.

Reviewers supply captured response/step identities, never guessed message IDs.
Every token must map back to exactly one generated field and round-trip to the
same decode steps before the client receives an enabled inspection action.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from clio_schemas.attention import AttentionProfile
from clio_schemas.attention_evidence import AttentionEvidenceInspection
from clio_schemas.connected_resources import ContentSelection
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from clio_agent.gact.attention.capture import load_capture
from clio_agent.gact.attention.chat_render import ChatRenderer
from clio_agent.gact.attention.lm_calls import LmCall
from clio_agent.gact.attention.lookup import lookup_attention
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.selection import output_steps
from clio_agent.gact.attention.store import AttentionStore
from clio_agent.gact.attention.textmap import locate_all
from clio_agent.gact.attention.transcript_map import transcript_texts


class FindingAttentionRequest(BaseModel):
    """Exact capture receipt returned by the reviewer's attention inspection."""

    model_config = ConfigDict(extra="forbid", strict=True)
    response_id: str = Field(min_length=1, max_length=256)
    capture_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    steps: list[Annotated[int, Field(ge=0)]] = Field(min_length=1, max_length=4096)
    profile: AttentionProfile
    profile_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    uncertainty: str = Field(min_length=1, max_length=2000)


def bind_finding_evidence(
    *,
    session_id: str,
    messages: list[Any],
    calls: list[LmCall],
    store: AttentionStore,
    renderer_for: Callable[[str], ChatRenderer],
    request: FindingAttentionRequest,
) -> AttentionEvidenceInspection:
    """Validate capture and exact selected-token coverage on the owning CLIO."""
    if request.profile.revision != request.profile_revision:
        raise AttentionUnavailable("profile_revision_changed", "reviewer profile hash differs")
    matches = [
        call
        for call in calls
        if call.session_id == session_id and call.response_id == request.response_id
    ]
    if len(matches) != 1:
        raise AttentionUnavailable("lm_call_ambiguous", "no unique parent-session response")
    call = matches[0]
    capture = load_capture(call, store, renderer_for)
    if capture.summary.record.sha256 != request.capture_sha256:
        raise AttentionUnavailable("capture_changed", "reviewer and CLIO capture hashes differ")
    content = call.content or ""
    span = output_steps(capture.renderer, content, 0, len(content), capture.summary.record)
    selected = sorted(set(request.steps))
    if not set(selected).issubset(span.steps):
        raise AttentionUnavailable(
            "content_coordinates_unavailable", "steps include no-text tokens"
        )
    encoded = capture.renderer.encode(content)
    fields = [
        text
        for message in messages
        if message.role == "assistant" and message.turn_id == call.turn_id
        for text in transcript_texts([message])
        if text.field in {"text", "thought", "input"}
    ]
    mapped: dict[tuple[str, str, str], tuple[Any, list[tuple[int, int]]]] = {}
    anchored = [
        (text, hits[0]) for text in fields if len(hits := locate_all(content, text.text)) == 1
    ]
    for step in selected:
        lo, hi = encoded.offsets[step]
        candidates = []
        for text, hit in anchored:
            chars = hit.from_haystack(lo, hi)
            if chars is not None and chars[0] < chars[1]:
                candidates.append((text, chars))
        if len(candidates) != 1:
            raise AttentionUnavailable(
                "content_coordinates_unavailable", "a selected token has no unique transcript field"
            )
        text, chars = candidates[0]
        key = (text.message_id, text.part_id, text.field)
        mapped.setdefault(key, (text, []))[1].append(chars)
    references: list[ContentSelection] = []
    for text, intervals in mapped.values():
        merged: list[tuple[int, int]] = []
        for lo, hi in sorted(intervals):
            if merged and lo <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
            else:
                merged.append((lo, hi))
        references.extend(
            ContentSelection.model_validate(
                {
                    "session_id": session_id,
                    "message_id": text.message_id,
                    "part_id": text.part_id,
                    "field": text.field,
                    "content_revision": text.content_revision,
                    "call_id": text.call_id or None,
                    "selection": {"kind": "text", "start": lo, "end": hi},
                }
            )
            for lo, hi in merged
        )
    if len(references) > 32:
        raise AttentionUnavailable("selection_too_large", "evidence exceeds 32 transcript ranges")
    result = lookup_attention(
        session_id=session_id,
        messages=messages,
        calls=[call],
        store=store,
        renderer_for=renderer_for,
        selections=references,
        direction="generated_to_source",
        profile=request.profile,
        lm_call_id=call.event_id,
    )
    if (
        result["unavailable"]
        or len(result["views"]) != 1
        or result["views"][0]["selected_steps"] != selected
        or result["views"][0]["capture_sha256"] != request.capture_sha256
        or result["views"][0]["profile_revision"] != request.profile_revision
    ):
        raise AttentionUnavailable(
            "content_coordinates_unavailable",
            "transcript ranges do not preserve the reviewed steps",
        )
    return AttentionEvidenceInspection(
        schema_version=1,
        direction="generated_to_source",
        selections=references,
        profile=request.profile,
        profile_revision=request.profile_revision,
        lm_call_id=call.event_id,
        capture_sha256=request.capture_sha256,
    )


def finding_attention_action(
    app: FastAPI, session_id: str, handle_id: str, raw: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """Return an enabled verified action, or a disabled action with an honest reason."""
    from clio_agent.gact.attention.routes import _renderer, attention_store, session_lm_calls

    action: dict[str, Any] = {
        "id": "inspect_evidence",
        "label": "Inspect evidence",
        "enabled": False,
        "behavior": {"kind": "inspect_attention", "handle_id": handle_id},
    }
    try:
        request = FindingAttentionRequest.model_validate(raw)
        inspection = bind_finding_evidence(
            session_id=session_id,
            messages=list(app.state.messages.get(session_id, [])),
            calls=session_lm_calls(app, session_id),
            store=attention_store(app),
            renderer_for=_renderer,
            request=request,
        )
    except ValidationError:
        action["behavior"]["reason"] = "The finding has no valid capture/profile receipt."
        return action, ""
    except AttentionUnavailable as exc:
        action["behavior"]["reason"] = str(exc.to_wire()["detail"] or exc.to_wire()["message"])
        return action, request.uncertainty
    action["enabled"] = True
    action["behavior"]["inspection"] = inspection.model_dump()
    return action, request.uncertainty
