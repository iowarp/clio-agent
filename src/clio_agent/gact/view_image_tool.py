"""Bounded workspace-image inspection for image-capable CLIO agents.

The native tool deliberately returns a small, durable descriptor instead of
base64 image bytes.  ReAct stores tool observations in ARC, so returning a
``dspy.Image`` directly would persist the expanded payload in the live context
plane and event log.  The adapter-side hydration seam revalidates the exact
workspace file immediately before the next model call and replaces the
descriptor with ``dspy.Image`` only in the ephemeral provider request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from clio_agent.gact.resource_mime import detect_media_type
from clio_agent.providers.native_attachment_bounds import (
    check_block_bytes,
    check_total_bytes,
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
    return {
        "type": VIEW_IMAGE_DESCRIPTOR_TYPE,
        "path": relative.as_posix(),
        "media_type": media_type,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _is_descriptor(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("type") == VIEW_IMAGE_DESCRIPTOR_TYPE


def _hydrate_descriptor(value: Mapping[str, Any]) -> tuple[Any, int]:
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


def hydrate_view_image_results(
    inputs: dict[str, Any],
    history_field_name: str,
    *,
    running_total_bytes: list[int] | None = None,
) -> int:
    """Hydrate retained view-image descriptors in one DSPy History input.

    The source ``dspy.History`` is replaced rather than mutated.  This keeps the
    durable/in-memory trajectory descriptor-only while the returned history sent
    to the provider contains real image blocks.  Returns the number of hydrated
    images; unrelated history values are byte-for-byte equivalent.

    ``running_total_bytes`` is a one-element mutable box shared with sibling
    hydration passes (:func:`clio_agent.gact.view_pdf_tool.hydrate_view_pdf_results`)
    for the SAME provider request, so ``check_total_bytes`` bounds every native
    attachment kind together rather than each kind separately -- an
    image-heavy step and a PDF in the same step could each stay under the
    aggregate ceiling on its own while their sum exceeded it. Defaults to a
    fresh, unshared counter for a standalone call.
    """

    import dspy  # noqa: PLC0415
    from dspy.adapters.types.tool import ToolCallResults, ToolCalls  # noqa: PLC0415

    history = inputs.get(history_field_name)
    if not isinstance(history, dspy.History):
        return 0

    image_count = 0
    total_bytes = running_total_bytes if running_total_bytes is not None else [0]
    messages: list[dict[str, Any]] = []
    for original in history.messages:
        message = dict(original)
        tool_calls = message.get("tool_calls")
        results = tool_calls.tool_call_results if isinstance(tool_calls, ToolCalls) else None
        if not isinstance(results, ToolCallResults):
            messages.append(message)
            continue

        hydrated_results: list[ToolCallResults.ToolCallResult] = []
        changed = False
        for result in results.tool_call_results:
            if result.name != "view_image" or result.is_error or not _is_descriptor(result.value):
                hydrated_results.append(result)
                continue
            image, byte_length = _hydrate_descriptor(result.value)
            total_bytes[0] += byte_length
            check_total_bytes(total_bytes[0])
            hydrated_results.append(result.model_copy(update={"value": image}))
            image_count += 1
            changed = True
        if changed:
            assert isinstance(tool_calls, ToolCalls)
            hydrated = results.model_copy(update={"tool_call_results": hydrated_results})
            message["tool_calls"] = tool_calls.model_copy(update={"tool_call_results": hydrated})
        messages.append(message)

    if image_count:
        inputs[history_field_name] = dspy.History(messages=messages)
    return image_count


def promote_view_image_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Promote JSONAdapter tool-image markers into real user image blocks.

    DSPy's JSONAdapter serializes a custom type returned by a natively framed
    tool call into the tool message as text.  Provider APIs require the matching
    tool response to remain in place, so keep that response and append a user
    image message immediately after it.  ChatAdapter already emits a user image
    block and therefore passes through unchanged.
    """

    from dspy.adapters.types.base_type import (  # noqa: PLC0415
        CUSTOM_TYPE_END_IDENTIFIER,
        CUSTOM_TYPE_START_IDENTIFIER,
        split_message_content_for_custom_types,
    )

    promoted: list[dict[str, Any]] = []
    for original in messages:
        message = dict(original)
        content = message.get("content")
        if not (
            message.get("role") == "tool"
            and message.get("name") == "view_image"
            and isinstance(content, str)
            and CUSTOM_TYPE_START_IDENTIFIER in content
            and CUSTOM_TYPE_END_IDENTIFIER in content
        ):
            promoted.append(message)
            continue

        image_message = {"role": "user", "content": content}
        split_message_content_for_custom_types([image_message])
        blocks = image_message.get("content")
        if not (
            isinstance(blocks, list)
            and any(
                isinstance(block, Mapping) and block.get("type") == "image_url" for block in blocks
            )
        ):
            promoted.append(message)
            continue
        message["content"] = "Workspace image attached in the following user message."
        promoted.extend([message, image_message])
    return promoted


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
    )


__all__ = [
    "VIEW_IMAGE_DESCRIPTOR_TYPE",
    "VIEW_IMAGE_MEDIA_TYPES",
    "ViewImageError",
    "build_view_image_tool",
    "hydrate_view_image_results",
    "promote_view_image_tool_messages",
]
