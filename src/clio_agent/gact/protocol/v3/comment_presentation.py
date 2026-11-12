"""What a browser comment session shows in the transcript (v3 projection).

The session part reads "N comments on <title>". Each of its pictures is named by its
mark and anchor (``comment_attachments._image_chip`` stores it as ``label``), or else
by its caption.

A file's comments (``artifact_review`` parts, one per thread) read as one reference to
the commented file version: it opens that version, and its navigation names the threads
so the client can show them.
"""

from __future__ import annotations

import mimetypes
from collections.abc import Mapping
from typing import Any

REVIEW_THREADS = "comment_threads"
REVIEW_LABELS = "comment_labels"


def comment_chip_label(part_type: str, metadata: Mapping[str, Any]) -> str | None:
    """The chip label of a comment session part or picture; ``None`` for any other part."""
    if part_type == "image" and "comment_session" in metadata:
        name = str(metadata.get("label") or metadata.get("caption") or "Image")
        return name[:1].upper() + name[1:]
    if part_type != "comment_session":
        return None
    session = metadata.get("comment_session")
    session = session if isinstance(session, Mapping) else {}
    marks = session.get("marks")
    count = len(marks) if isinstance(marks, list) else 0
    title = str(session.get("title") or "")
    noun = "comment" if count == 1 else "comments"
    return f"{count} {noun} on {title}" if title else f"{count} {noun}"


def artifact_review_reference(
    part: Mapping[str, Any], metadata: Mapping[str, Any], common: Mapping[str, Any]
) -> dict[str, Any]:
    """The artifact reference of one comment thread on a file version."""
    name = str(metadata.get("artifact_name") or "")
    version = part.get("artifact_version")
    navigation: dict[str, Any] = {
        REVIEW_THREADS: [str(part.get("review_id") or "")],
        REVIEW_LABELS: [str(metadata.get("label") or "")],
    }
    if metadata.get("workspace_id"):
        navigation["workspace_id"] = str(metadata["workspace_id"])
    if isinstance(version, int):
        navigation["artifact_version"] = version
    return {
        "id": str(part.get("id") or ""),
        "type": "context_reference",
        "ref_kind": "artifact",
        "ref_id": str(part.get("artifact_id") or ""),
        "label": name or "Commented file",
        "revision": str(part.get("artifact_sha256") or ""),
        "media_type": (mimetypes.guess_type(name)[0] if name else None)
        or "application/octet-stream",
        "navigation": navigation,
        **common,
    }


def merge_artifact_review(blocks: list[dict[str, Any]], block: Mapping[str, Any]) -> bool:
    """Fold a thread's reference into an earlier one on the same version; ``True`` if folded.

    One send makes a part per thread, but the message names the file once.
    """
    navigation = block.get("navigation")
    if block.get("type") != "context_reference" or not isinstance(navigation, Mapping):
        return False
    if REVIEW_THREADS not in navigation:
        return False
    for earlier in blocks:
        earlier_navigation = earlier.get("navigation")
        if (
            earlier.get("type") == "context_reference"
            and earlier.get("ref_id") == block.get("ref_id")
            and isinstance(earlier_navigation, dict)
            and REVIEW_THREADS in earlier_navigation
        ):
            earlier_navigation[REVIEW_THREADS] = [
                *earlier_navigation[REVIEW_THREADS],
                *navigation[REVIEW_THREADS],
            ]
            earlier_navigation[REVIEW_LABELS] = [
                *earlier_navigation.get(REVIEW_LABELS, []),
                *navigation.get(REVIEW_LABELS, []),
            ]
            return True
    return False
