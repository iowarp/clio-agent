"""Bounded workspace-image inspection for image-capable CLIO agents.

The native tool deliberately returns a small, durable descriptor instead of
base64 image bytes.  The agent loop stores tool observations in ARC, so returning
the image directly would persist the expanded payload in the live context plane and
event log.  When the loop rebuilds its context
(:func:`clio_agent.gact.agents.clio_react_record.result_part`) it revalidates the
exact workspace file and replaces the descriptor with a native image part only in
the ephemeral provider request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from clio_agent.gact import viewed_media
from clio_agent.gact.resource_mime import detect_media_type
from clio_agent.providers.native_attachment_bounds import (
    check_block_bytes,
)
from clio_agent.tools.execution import get_active_tool_workspace_root
from clio_agent.tools.file_policy import FileAccessPolicy

VIEW_IMAGE_DESCRIPTOR_TYPE = "clio.workspace_image.v1"
VIEW_IMAGE_MEDIA_TYPES = frozenset(
    {
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)


class ViewImageError(ValueError):
    """A typed refusal raised when a workspace image cannot be safely viewed."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _workspace_root() -> Path:
    raw = get_active_tool_workspace_root().strip()
    if not raw:
        raise ViewImageError(
            "view_image_workspace_unavailable",
            "view_image requires an active CLIO workspace.",
        )
    try:
        return Path(raw).resolve(strict=True)
    except FileNotFoundError as exc:
        raise ViewImageError(
            "view_image_workspace_unavailable",
            f"The active CLIO workspace does not exist: {raw}",
        ) from exc


def _workspace_image(path: str) -> tuple[Path, Path, bytes, str]:
    root = _workspace_root()
    requested = Path(path)
    candidate = requested if requested.is_absolute() else root / requested
    resolved = FileAccessPolicy.from_env().validate_read(str(candidate), field="path")
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ViewImageError(
            "view_image_outside_workspace",
            "view_image can only inspect files inside the active CLIO workspace.",
        ) from exc

    data = resolved.read_bytes()
    check_block_bytes("image", len(data), label=relative.as_posix())
    media_type, _source = detect_media_type(resolved.name, data[:4096])
    if media_type not in VIEW_IMAGE_MEDIA_TYPES:
        supported = ", ".join(sorted(VIEW_IMAGE_MEDIA_TYPES))
        raise ViewImageError(
            "view_image_unsupported_media_type",
            f"view_image supports {supported}; detected {media_type!r} for {relative.as_posix()}.",
        )
    return resolved, relative, data, media_type


def _descriptor(path: str) -> dict[str, Any]:
    _resolved, relative, data, media_type = _workspace_image(path)
    snapshot_path, snapshot_sha256 = viewed_media.snapshot(data, _resolved.suffix)
    return {
        "type": VIEW_IMAGE_DESCRIPTOR_TYPE,
        "path": relative.as_posix(),
        "media_type": media_type,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        # What the agent saw, kept: history reads this, never the live file.
        "snapshot": snapshot_path,
        "snapshot_sha256": snapshot_sha256,
    }


def _is_descriptor(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("type") == VIEW_IMAGE_DESCRIPTOR_TYPE


def _hydrate_descriptor(value: Mapping[str, Any]) -> tuple[Any, int]:
    if value.get("snapshot"):
        import dspy  # noqa: PLC0415 - keep UI/bootstrap imports light

        data = viewed_media.read_snapshot(
            str(value["snapshot"]), str(value.get("snapshot_sha256") or "")
        )
        encoded = base64.b64encode(data).decode("ascii")
        return dspy.Image(url=f"data:{value.get('media_type')};base64,{encoded}"), len(data)
    path = str(value.get("path") or "").strip()
    if not path or Path(path).is_absolute():
        raise ViewImageError(
            "view_image_descriptor_invalid",
            "The retained view_image result does not contain a workspace-relative path.",
        )
    _resolved, relative, data, media_type = _workspace_image(path)
    expected_type = str(value.get("media_type") or "")
    expected_size = value.get("size_bytes")
    expected_sha256 = str(value.get("sha256") or "")
    actual_sha256 = hashlib.sha256(data).hexdigest()
    if (
        relative.as_posix() != path.replace("\\", "/")
        or expected_type != media_type
        or expected_size != len(data)
        or not expected_sha256
        or not hmac.compare_digest(expected_sha256, actual_sha256)
    ):
        raise ViewImageError(
            "view_image_file_changed",
            f"The workspace image changed after view_image inspected it: {relative.as_posix()}",
        )

    import dspy  # noqa: PLC0415 - keep UI/bootstrap imports light

    encoded = base64.b64encode(data).decode("ascii")
    return dspy.Image(url=f"data:{media_type};base64,{encoded}"), len(data)


def build_view_image_tool() -> Any:
    """Build the declared native tool that inspects one workspace image."""

    from clio_agent.gact.agents.native_presenters_workspace_file import (  # noqa: PLC0415
        workspace_file_presentation,
    )
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    def view_image(path: str) -> dict[str, Any]:
        """Attach a workspace image to the next model step for visual inspection.

        Use this only for an image that already exists inside the active workspace.
        PDF files must first be rendered into bounded PNG, JPEG, GIF, or WebP pages.
        The file is size-bounded, media-sniffed, hashed, and revalidated before its
        pixels reach the model.
        """

        return _descriptor(path)

    return native_tool(
        view_image,
        name="view_image",
        presentation=workspace_file_presentation,
        domain="workspace",
        desc=view_image.__doc__,
        title="View image",
        args={
            "path": {
                "type": "string",
                "description": "Workspace-relative or absolute path to a PNG, JPEG, GIF, or WebP image.",
            }
        },
        read_only=True,
    )


__all__ = [
    "VIEW_IMAGE_DESCRIPTOR_TYPE",
    "VIEW_IMAGE_MEDIA_TYPES",
    "ViewImageError",
    "build_view_image_tool",
]
