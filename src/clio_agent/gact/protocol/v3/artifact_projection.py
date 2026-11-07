"""The transcript's artifact entity: what a ``resource_link`` part shows as a file card.

A ``resource_link`` part (``gact/artifacts/wire.py``) carries an artifact's identity in its
metadata. The client reads it as an artifact entity: id, name, media type, version, custody
and, when the artifact shows as more than its file name, its ``presentation`` as the entity's
``producer`` (a paper's title, type and members).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

__all__ = ["artifact_entity"]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def artifact_entity(
    part: Mapping[str, Any], part_id: str, *, session_id: str, created_at: str
) -> tuple[str, dict[str, Any]]:
    """The artifact id and entity a ``resource_link`` part projects to."""
    metadata = _mapping(part.get("metadata"))
    artifact_id = str(metadata.get("artifact_id") or part.get("uri") or part_id)
    version = metadata.get("version")
    presentation = metadata.get("presentation")
    entity: dict[str, Any] = {
        "id": artifact_id,
        "session_id": session_id,
        "name": str(part.get("name") or Path(artifact_id).name or "Artifact"),
        "media_type": str(
            part.get("mime_type") or part.get("media_type") or "application/octet-stream"
        ),
        "uri": str(part.get("uri") or ""),
    }
    if isinstance(version, int) and not isinstance(version, bool) and version > 0:
        entity["version"] = version
    for key, wire in (
        ("workspace_id", "workspace_id"),
        ("fetch_path", "fetch_url"),
        ("custody", "custody"),
        ("sha256", "sha256"),
    ):
        if metadata.get(wire):
            entity[key] = str(metadata[wire])
    if isinstance(metadata.get("size_bytes"), int):
        entity["size"] = int(metadata["size_bytes"])
    if isinstance(presentation, Mapping) and presentation:
        entity["producer"] = dict(presentation)
    entity["created_at"] = created_at
    return artifact_id, entity
