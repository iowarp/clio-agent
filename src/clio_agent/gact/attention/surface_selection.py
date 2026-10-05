"""Bind viewer selections to a stored A2UI revision, without inventing token maps."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any

from clio_schemas.connected_resources import ContentSelection, ImageSelection, StructuredSelection
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import Self

from clio_agent.gact.attention.content_references import message_content_references
from clio_agent.gact.attention.reasons import AttentionUnavailable


def surface_digest(surface: Any) -> str:
    """Hash the entire displayed definition, including prior batches and data updates."""
    raw = json.dumps(surface.messages, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_surface_reference(app: FastAPI, session_id: str, ref: ContentSelection) -> None:
    """Reject changed/deleted/recreated surfaces before any attribution is attempted."""
    identity = ref.surface
    if identity is None:
        return
    surface = app.state.a2ui_store.get(session_id, identity.surface_id)
    if (
        surface is None
        or surface.state not in {"ready", "pending_action"}
        or surface.part_id != ref.part_id
        or surface.revision != identity.revision
        or surface_digest(surface) != identity.sha256
    ):
        raise AttentionUnavailable("content_revision_changed", "the selected A2UI view changed")


class SurfaceSelectionRequest(BaseModel):
    """A selection made in the displayed revision; it never asserts capture readiness."""

    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    component_id: str = Field(min_length=1, max_length=256)
    source_ref: str = Field(min_length=1, max_length=4096)
    selection: Annotated[ImageSelection | StructuredSelection, Field(discriminator="kind")]

    @model_validator(mode="after")
    def bounded(self) -> Self:
        """Bound selected source keys before any transcript or surface reads."""
        if isinstance(self.selection, StructuredSelection) and (
            len(self.selection.keys) > 1000 or any(len(key) > 4096 for key in self.selection.keys)
        ):
            raise ValueError("Select at most 1000 bounded source keys")
        return self


def bind_surface_selection(
    app: FastAPI, session_id: str, surface_id: str, body: SurfaceSelectionRequest
) -> dict[str, Any]:
    """Return an authoritative part reference only for the exact displayed surface."""
    surface = app.state.a2ui_store.get(session_id, surface_id)
    if surface is None:
        raise HTTPException(404, "This surface is not in the selected conversation")
    if surface.revision != body.revision or surface.state not in {"ready", "pending_action"}:
        raise HTTPException(
            409, "This surface changed or is unavailable. Select its current view again."
        )
    component = next(
        (
            item
            for envelope in reversed(surface.messages)
            for item in envelope.get("updateComponents", {}).get("components", [])
            if item.get("id") == body.component_id
        ),
        None,
    )
    if component is None:
        raise HTTPException(409, "The selected component is no longer in this surface")
    source = component.get("url" if isinstance(body.selection, ImageSelection) else "dataUri")
    if source is None and isinstance(body.selection, StructuredSelection):
        source = f"a2ui://{surface_id}/{body.component_id}"
    if not isinstance(source, str):
        raise HTTPException(
            409, "This component's bound source cannot yet be frozen for attention inspection"
        )
    if body.source_ref != source:
        raise HTTPException(409, "The selected source differs from the recorded component")
    if isinstance(body.selection, StructuredSelection) and (
        body.selection.surface_id != surface_id
        or body.selection.component_id != body.component_id
        or body.selection.source_ref != source
    ):
        raise HTTPException(409, "The selection belongs to another component or source")
    message = next(
        (
            message
            for message in app.state.messages.get(session_id, [])
            if any(part.id == surface.part_id and part.type == "a2ui" for part in message.parts)
        ),
        None,
    )
    if message is None:
        raise HTTPException(409, "Wait for this surface's message to finish before inspecting it")
    reference = next(
        (
            row["reference"]
            for row in message_content_references(message)
            if row["reference"]["part_id"] == surface.part_id
            and row["reference"]["field"] == "content"
        ),
        None,
    )
    if reference is None:
        raise HTTPException(409, "This surface has no stored transcript part to inspect")
    reference["selection"] = body.selection.model_dump()
    reference["surface"] = {
        "surface_id": surface_id,
        "component_id": body.component_id,
        "revision": surface.revision,
        "sha256": surface_digest(surface),
    }
    if isinstance(body.selection, ImageSelection):
        reference["artifact_ref"] = source
    result = ContentSelection.model_validate(reference)
    # A concurrent producer may have updated the projection during this read.
    try:
        validate_surface_reference(app, session_id, result)
    except AttentionUnavailable as exc:
        raise HTTPException(409, "This surface changed. Select its current view again.") from exc
    return result.model_dump()
