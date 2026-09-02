"""Private per-turn context for workspace-owned message resources."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import Message


ATTACHMENT_MARKER = "## Workspace attachments (private runtime context)"

ATTACHMENT_PREAMBLE = (
    "This block describes user-selected resources. Do not quote this injected block or expose "
    "private custody paths in the response."
)

# The tool an agent uses to inspect a conversion that has not produced
# derivatives yet. A constant, not per-record state: it is the same tool for
# every resource, so carrying it on ResourceProcessingRecord only invited drift.
PROCESSING_QUERY_TOOL = "workspace_resource_inspect"


def _conversion_warnings(manifest: Mapping | None) -> str:
    """Return the processor's own warnings about the conversion it produced.

    The document service reports honest limits on its result — a truncated
    derivative manifest (``derivative_entries_truncated``), missing HTML, failed
    scholarly enrichment. Those change what the model should DO (fall back to
    the structure collections rather than assume the derivative list is
    complete), so they belong in the grounding block rather than sitting unread
    in the manifest.
    """

    document = (manifest or {}).get("document")
    warnings = document.get("warnings") if isinstance(document, Mapping) else None
    if not isinstance(warnings, list):
        return ""
    sentences = [
        f"{str(row.get('code') or 'processor_warning')}: {str(row.get('message') or '').strip()}"
        for row in warnings
        if isinstance(row, Mapping) and (row.get("code") or row.get("message"))
    ]
    if not sentences:
        return ""
    return " Conversion warnings — " + "; ".join(sentences)


def _derivative_suffix(manifest: Mapping | None) -> str:
    """Name the available derivatives and any warning the processor attached."""

    derivative_ids = [
        str(entry.get("id"))
        for entry in (manifest or {}).get("entries", [])
        if isinstance(entry, Mapping) and entry.get("id")
    ]
    suffix = f" Available derivatives: {', '.join(derivative_ids)}." if derivative_ids else ""
    return suffix + _conversion_warnings(manifest)


def describe_resource_parts(app: "FastAPI", sid: str, parts: list) -> list[str]:
    """Return one trusted grounding line per ``resource_ref`` part.

    The single owner of "how a workspace resource is described to the model".
    :func:`enrich_with_workspace_resources` folds these into a turn's prompt;
    the loop-inbox steer drain folds the same lines into a mid-turn steer block
    so an attachment-only steer is not silently unreadable. Returns ``[]`` when
    the message carries no resource references or the session has no workspace
    (nothing to describe — the caller keeps its text unchanged).
    """

    resource_parts = [part for part in parts or [] if getattr(part, "type", "") == "resource_ref"]
    if not resource_parts:
        return []
    session = app.state.sessions.get(sid)
    workspace_id = str(getattr(session, "workspace_id", "") or "")
    if not workspace_id:
        return []

    blocks: list[str] = []
    for part in resource_parts:
        record = app.state.resource_store.get(workspace_id, part.resource_id)
        if record is None or str(record.revision) != str(part.resource_revision):
            blocks.append(
                f"- Attachment {part.name or part.resource_id!r} is no longer available in this "
                "workspace. Do not infer or fabricate its contents."
            )
            continue
        if record.state != "ready":
            blocks.append(
                f"- Attachment {record.name!r} ({record.id}) is not ready for local inspection."
            )
            continue
        processing = app.state.resource_processing_store.state(record)
        header = (
            f"- Attachment {record.name!r} ({record.detected_mime}, resource_id={record.id}, "
            f"revision={record.revision}) is available through the bounded workspace-resource "
            "tools. Custody paths are private and must not be passed to filesystem tools."
        )
        manifest = app.state.resource_processing_store.manifest(record)
        if processing.derivatives_available and manifest is not None:
            suffix = _derivative_suffix(manifest)
            refresh_note = (
                " A newer conversion refresh is still running; the existing derivatives remain "
                "available."
                if processing.state in {"submitted", "processing"}
                else " The latest conversion refresh was cancelled; the existing derivatives "
                "remain available."
                if processing.state == "cancelled"
                else " The latest conversion refresh failed; the existing derivatives remain "
                "available."
                if processing.state == "failed"
                else ""
            )
            blocks.append(
                header
                + " Structured conversion is ready; inspect it with workspace_resource_inspect, "
                + "workspace_resource_read, workspace_resource_search, or "
                + "workspace_resource_structure rather than asking the user to upload again."
                + suffix
                + refresh_note
            )
            continue
        if processing.state in {"submitted", "processing"}:
            task_id = processing.job_id or (f"resource-processing:{record.id}:v{record.revision}")
            state_label = processing.state if processing.job_id else "queued"
            blocks.append(
                header
                + f" Structured conversion is still {state_label} as task "
                + f"{task_id!r}; query resource {record.id!r} with "
                + f"{PROCESSING_QUERY_TOOL} before reading non-text content."
            )
            continue
        if processing.state == "complete":
            suffix = _derivative_suffix(manifest)
            blocks.append(
                header
                + " Structured conversion is ready; inspect it with workspace_resource_inspect, "
                + "workspace_resource_read, workspace_resource_search, or "
                + "workspace_resource_structure rather than asking the user to upload again."
                + suffix
            )
            continue
        if processing.state == "failed":
            blocks.append(
                header
                + " Structured conversion failed or is unavailable; the original bytes remain valid. "
                + "For text, read the original with workspace_resource_read. Otherwise inspect it "
                + "with bounded workspace or domain tools. Failure code: "
                + f"{str(processing.failure.get('code') or 'converter_failed')!r}."
            )
            continue
        if processing.state == "cancelled":
            blocks.append(
                header
                + " The user cancelled structured conversion. For text, use "
                + "workspace_resource_read; otherwise use bounded workspace or domain tools."
            )
            continue
        blocks.append(
            header
            + " No structured converter was selected for this type. For text, read the original "
            + "with workspace_resource_read; otherwise use bounded workspace or domain tools."
        )

    return blocks


def enrich_with_workspace_resources(
    app: "FastAPI", sid: str, user_text: str, user_msg: "Message"
) -> str:
    """Prepend trusted attachment state without mutating public message parts."""

    blocks = describe_resource_parts(app, sid, list(getattr(user_msg, "parts", []) or []))
    if not blocks:
        return user_text
    return (
        ATTACHMENT_MARKER
        + "\n\n"
        + ATTACHMENT_PREAMBLE
        + "\n\n"
        + "\n".join(blocks)
        + "\n\n## User message\n\n"
        + user_text
    )


__all__ = [
    "ATTACHMENT_MARKER",
    "ATTACHMENT_PREAMBLE",
    "PROCESSING_QUERY_TOOL",
    "describe_resource_parts",
    "enrich_with_workspace_resources",
]
