"""Authoritative selection identities for every stored transcript content part."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from clio_schemas.connected_resources import ContentSelection

from clio_agent.gact.attention.transcript_map import transcript_texts
from clio_agent.gact.types import Message


def message_content_references(message: Message) -> list[dict[str, Any]]:
    """Expose exact field identities without guessing a capture's media coordinates.

    Tool-call and result parts retain their distinct stored ids even when the UI
    groups them into one invocation. Media revisions identify the recorded part,
    not mutable external bytes fetched after that message was created.
    """
    result = []
    texts = transcript_texts([message])
    for part in message.parts:
        fields = [text for text in texts if text.part_id == part.id]
        for text in fields:
            reference = ContentSelection.model_validate(
                {
                    "session_id": message.session_id,
                    "message_id": message.id,
                    "part_id": part.id,
                    "field": text.field,
                    "content_revision": text.content_revision,
                    "call_id": text.call_id or None,
                    "selection": {"kind": "whole"},
                }
            )
            result.append(
                {
                    "reference": reference.model_dump(),
                    "kind": text.kind,
                    "label": text.kind.replace("_", " ").capitalize(),
                    "preview": text.text[:120],
                    "characters": len(text.text),
                    "coordinate_support": "text",
                }
            )
        # Even mixed tool results can contain images or embedded resources.
        media = part.type not in {"text", "tool_call", "tool_result"} or any(
            getattr(content, "type", "text") != "text" for content in part.content or []
        )
        if not media:
            continue
        raw = json.dumps(part.to_wire(), sort_keys=True, ensure_ascii=False).encode("utf-8")
        artifact = (
            part.uri
            if part.type == "resource_link"
            else (part.resource_id if part.type == "resource_ref" else None)
        )
        reference = ContentSelection.model_validate(
            {
                "session_id": message.session_id,
                "message_id": message.id,
                "part_id": part.id,
                "field": "content",
                "content_revision": hashlib.sha256(raw).hexdigest(),
                "call_id": part.call_id or None,
                "artifact_ref": artifact or None,
                "selection": {"kind": "whole"},
            }
        )
        result.append(
            {
                "reference": reference.model_dump(),
                "kind": part.type,
                "label": part.type.replace("_", " ").capitalize(),
                "preview": "",
                "characters": 0,
                "coordinate_support": "unavailable",
                "explanation": "This recorded block has no captured token or image-patch coordinate map. Its reference can be inspected, but attribution cannot be inferred.",
            }
        )
    return result
