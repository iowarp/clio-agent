"""Declared, observer-only MCP adapters; clients need no tool-name heuristics."""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

import yaml

from clio_agent.gact.tool_result_presentation import ToolPresentation
from clio_agent.tools.file_diff import unified_file_diff
from clio_agent.tools.file_policy import validate_read_path, validate_write_path

logger = logging.getLogger(__name__)
Adapter = Callable[[Mapping[str, Any], Any, Any], dict[str, Any]]
Capture = Callable[[Mapping[str, Any]], Any]


@dataclass(frozen=True)
class PresentationAdapter:
    """Result presenter and optional pre-call evidence capture."""

    present: Adapter
    capture: Capture | None = None
    start: Capture | None = None
    action: str = ""
    subject: str = ""


@dataclass(frozen=True)
class FileWriteSnapshot:
    """Observed file content before a write."""

    path: str
    content: str


MCP_PRESENTATION_ADAPTERS: dict[str, PresentationAdapter] = {}


def register_presentation_adapter(name: str, adapter: PresentationAdapter) -> None:
    """Register result semantics without modifying the upstream MCP server."""

    MCP_PRESENTATION_ADAPTERS[name] = adapter


def capture_tool_presentation(name: str, args: dict[str, Any]) -> Any:
    """Capture evidence through the declared lifecycle hook."""

    adapter = MCP_PRESENTATION_ADAPTERS.get(name)
    try:
        return adapter.capture(copy.deepcopy(args)) if adapter and adapter.capture else None
    except Exception:
        logger.exception("Tool presentation capture failed: %s", name)
        return None


def starting_presentation(name: str, args: Mapping[str, Any]) -> dict[str, Any] | None:
    """Expose declared invocation content before the result arrives."""

    adapter = MCP_PRESENTATION_ADAPTERS.get(name)
    if adapter is None or adapter.start is None:
        return None
    try:
        content = adapter.start(copy.deepcopy(args))
        if content is None:
            return None
        value = {**content, "action": adapter.action or None}
        return ToolPresentation.model_validate(value).model_dump(exclude_none=True)
    except Exception:
        logger.exception("Starting tool presentation failed: %s", name)
        return None


def _structured(result: Any) -> Mapping[str, Any]:
    row = result.get("structuredContent", {}) if isinstance(result, Mapping) else {}
    return row if isinstance(row, Mapping) else {}


def standard_mcp_presentation(result: Any) -> dict[str, Any]:
    """Present standard MCP content; structured results remain technical detail."""

    blocks: list[dict[str, Any]] = []
    content = result.get("content", []) if isinstance(result, Mapping) else []
    for index, item in enumerate(content):
        if not isinstance(item, Mapping):
            continue
        block_id = f"content-{index}"
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            duplicate = False
            if "structuredContent" in result:
                try:
                    duplicate = json.loads(item["text"]) == result["structuredContent"]
                except json.JSONDecodeError:
                    pass
            if not duplicate:
                blocks.append({"id": block_id, "type": "text", "text": item["text"]})
        elif item.get("type") in {"image", "audio"}:
            blocks.append(
                {
                    "id": block_id,
                    "type": "media" if item.get("data") else "text",
                    "media_type": str(item.get("mimeType") or ""),
                    "label": f"{str(item['type']).capitalize()} · {item.get('mimeType', '')}",
                    "text": str(
                        item.get("data") or "Media payload unavailable in the retained tool result."
                    ),
                }
            )
        elif item.get("type") in {"resource", "resource_link"}:
            resource = item.get("resource", item)
            if isinstance(resource, Mapping):
                if isinstance(resource.get("text"), str):
                    blocks.append({"id": block_id, "type": "text", "text": resource["text"]})
                elif resource.get("uri"):
                    blocks.append(
                        {
                            "id": block_id,
                            "type": "link",
                            "target": "url",
                            "uri": str(resource["uri"]),
                            "label": str(resource.get("name") or resource["uri"]),
                        }
                    )
    return {"summary": "", "blocks": blocks}


def present_mcp_result(
    name: str, args: Mapping[str, Any], result: Any, snapshot: Any = None
) -> dict[str, Any]:
    """Validate an adapter's output, falling back with a technical diagnostic."""

    try:
        args, result = copy.deepcopy(args), copy.deepcopy(result)
        adapter = MCP_PRESENTATION_ADAPTERS.get(name)
        value = (
            adapter.present(args, result, snapshot)
            if adapter
            else standard_mcp_presentation(result)
        )
        if adapter and adapter.action and (value.get("blocks") or value.get("summary")):
            value = {**value, "action": adapter.action}
        return ToolPresentation.model_validate(value).model_dump(exclude_none=True)
    except Exception:
        logger.exception("Tool presentation failed: %s", name)
        return {"summary": "", "blocks": [], "diagnostic": "presentation_failed"}


def enrich_tool_observer_result(
    result: Any, args: dict[str, Any], snapshot: Any, *, name: str = "fs_apply_edit_write"
) -> Any:
    """Attach presentation to the observer copy without changing the raw result."""

    if not isinstance(result, Mapping):
        return result
    return {**result, "presentation": present_mcp_result(name, args, result, snapshot)}


def observe_mcp_result(name: str, result: Any, args: dict[str, Any], snapshot: Any) -> Any:
    """Project the raw MCP result at the observation boundary only."""
    from clio_agent.tools.mcp_results import call_tool_result_to_observer

    return enrich_tool_observer_result(
        call_tool_result_to_observer(result), args, snapshot, name=name
    )


def _capture_write(args: Mapping[str, Any]) -> FileWriteSnapshot:
    path = Path(validate_write_path(str(args["filepath"]), field="filepath"))
    content = (
        Path(validate_read_path(str(path))).read_text(encoding="utf-8", errors="replace")
        if path.exists()
        else ""
    )
    return FileWriteSnapshot(str(path), content)


def _file_start(args: Mapping[str, Any]) -> dict[str, Any] | None:
    """Name the declared file before execution, without inventing its effects."""
    path = str(args.get("filepath") or "")
    if not path:
        return None
    return {
        "subject": "file-link",
        "summary": "",
        "blocks": [
            {
                "id": "file-link",
                "type": "link",
                "target": "file",
                "uri": path,
                "label": PureWindowsPath(path).name,
            }
        ],
    }


def _read(args: Mapping[str, Any], result: Any, snapshot: Any) -> dict[str, Any]:
    del args, snapshot
    row = _structured(result)
    if "content" not in row:
        return standard_mcp_presentation(result)
    path = PureWindowsPath(str(row.get("path") or ""))
    content = str(row.get("content") or "")
    kind = "code"
    metadata: list[dict[str, Any]] = []
    if path.suffix.lower() in {".md", ".markdown"}:
        kind = "markdown"
        # This is declared file-format presentation, not model-output repair.
        # The unmodified source remains in the result's technical evidence.
        lines = content.splitlines(keepends=True)
        if lines and lines[0].strip() == "---":
            end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
            if end is not None:
                try:
                    frontmatter = yaml.safe_load("".join(lines[1:end]))
                except yaml.YAMLError:
                    frontmatter = None
                if isinstance(frontmatter, Mapping):
                    identity = "\n".join(
                        f"{key.replace('_', ' ').capitalize()}: {value}"
                        for key, value in frontmatter.items()
                        if isinstance(key, str) and isinstance(value, str | int | float | bool)
                    )
                    if identity:
                        metadata.append({"id": "metadata", "type": "text", "text": identity})
                    content = "".join(lines[end + 1 :]).lstrip("\r\n")
    elif path.suffix.lower() in {".txt", ".log"}:
        kind = "text"
    return {
        "subject": "file-link",
        "summary": f"{row.get('size_bytes', 0)} bytes",
        "blocks": [
            {
                "id": "file-link",
                "type": "link",
                "target": "file",
                "uri": str(row.get("path") or ""),
                "label": path.name,
            },
            *metadata,
            {
                "id": "file",
                "type": kind,
                "text": content,
            },
        ],
    }


def _diff(args: Mapping[str, Any], result: Any, snapshot: Any) -> dict[str, Any]:
    row = _structured(result)
    diff = str(row.get("unified_diff") or "")
    if isinstance(snapshot, FileWriteSnapshot) and row.get("ok") is True:
        after = Path(validate_read_path(snapshot.path)).read_text(
            encoding="utf-8", errors="replace"
        )
        diff = unified_file_diff(snapshot.content, after, Path(snapshot.path).name, context=1)
    return {
        "subject": "file-link",
        "summary": "",
        "blocks": [
            {
                "id": "file-link",
                "type": "link",
                "target": "file",
                "uri": str(row.get("path") or args.get("filepath") or ""),
                "label": PureWindowsPath(str(row.get("path") or args.get("filepath") or "")).name,
            },
            {
                "id": "diff",
                "type": "diff",
                "text": diff,
            },
        ],
    }


def _terminal(args: Mapping[str, Any], result: Any, snapshot: Any) -> dict[str, Any]:
    del snapshot
    row = _structured(result)
    return {
        "summary": "",
        "blocks": [
            {
                "id": "terminal",
                "type": "terminal",
                "command": str(row.get("command") or args.get("command") or ""),
                "text": str(row.get("stdout") or "") + str(row.get("stderr") or ""),
                "exit_code": row.get("exit_code"),
                "timed_out": bool(row.get("timed_out")),
            }
        ],
    }


def _web(args: Mapping[str, Any], result: Any, snapshot: Any) -> dict[str, Any]:
    del snapshot
    row = _structured(result)
    blocks: list[dict[str, Any]] = []
    document = row.get("document")
    document = document if isinstance(document, Mapping) else {}
    metadata = document.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    title = row.get("title") or metadata.get("title")
    for field in ("markdown", "content", "text"):
        if isinstance(row.get(field), str):
            blocks.append({"id": field, "type": "markdown", "text": row[field]})
            break
    identity = []
    for field, label in (
        ("conversion_id", "Conversion"),
        ("status", "HTTP status"),
        ("provider", "Provider"),
        ("extractor", "Extractor"),
        ("content_type", "Content type"),
        ("size_bytes", "Bytes"),
    ):
        if isinstance(row.get(field), str | int):
            identity.append(f"{label}: {row[field]}")
    structure = document.get("structure_summary")
    if isinstance(structure, Mapping) and isinstance(structure.get("pages"), int):
        identity.append(f"Pages: {structure['pages']}")
    if identity:
        blocks.append({"id": "identity", "type": "text", "text": "\n".join(identity)})
    if isinstance(row.get("results"), list):
        blocks.append(
            {"id": "result-count", "type": "text", "text": f"{len(row['results'])} search results"}
        )
    failures = row.get("unresponsive_engines")
    if isinstance(failures, list) and failures:
        blocks.append(
            {
                "id": "engine-status",
                "type": "text",
                "text": "\n".join(
                    f"{engine.get('engine', 'Search engine')}: {engine.get('reason', 'Unavailable')}"
                    for engine in failures
                    if isinstance(engine, Mapping)
                ),
            }
        )
    if row.get("error"):
        blocks.append({"id": "error", "type": "text", "text": str(row["error"])})
    if title and row.get("url"):
        blocks.insert(
            0,
            {
                "id": "source",
                "type": "link",
                "target": "url",
                "uri": str(row["url"]),
                "label": "Source document",
            },
        )
    events = row.get("events", [])
    if isinstance(events, list):
        text = "\n".join(
            f"{event.get('stage', '')} · {event.get('message', '')}"
            for event in events
            if isinstance(event, Mapping)
        )
        if text:
            blocks.append({"id": "events", "type": "text", "text": text})
    for field, label in (
        ("local_path", "Saved Markdown"),
        ("markdown_path", "Saved Markdown"),
        ("metadata_path", "Saved metadata"),
    ):
        if isinstance(row.get(field), str) and row[field]:
            blocks.append(
                {
                    "id": field,
                    "type": "link",
                    "target": "resource",
                    "uri": row[field],
                    "label": label,
                }
            )
    for index, hit in enumerate(row.get("results", [])):
        if isinstance(hit, Mapping) and hit.get("url"):
            blocks.append(
                {
                    "id": f"result-{index}",
                    "type": "link",
                    "target": "url",
                    "uri": str(hit["url"]),
                    "label": str(hit.get("title") or hit["url"]),
                }
            )
    url = str(row.get("url") or args.get("url") or "")
    query = str(args.get("query") or args.get("conversion_id") or "")
    if url or query:
        blocks = [block for block in blocks if block["id"] != "source"]
        blocks.insert(
            0,
            {
                "id": "subject",
                "type": "link" if url else "text",
                "target": "url" if url else None,
                "uri": url,
                "label": str(title or url or query),
                "text": query,
            },
        )
    return {"subject": "subject" if url or query else "", "summary": "", "blocks": blocks}


register_presentation_adapter(
    "fs_read_file",
    PresentationAdapter(_read, start=_file_start, action="Read", subject="file-link"),
)
register_presentation_adapter(
    "fs_propose_edit",
    PresentationAdapter(_diff, start=_file_start, action="Propose edit", subject="file-link"),
)
register_presentation_adapter(
    "fs_apply_edit_write",
    PresentationAdapter(
        _diff, _capture_write, start=_file_start, action="Write", subject="file-link"
    ),
)
register_presentation_adapter(
    "shell_bash",
    PresentationAdapter(_terminal, start=lambda args: _terminal(args, {}, None), action="Run"),
)
for _name, _action in (
    ("web_search", "Search"),
    ("web_fetch", "Fetch"),
    ("web_fetch_events", "Conversion events"),
):
    register_presentation_adapter(_name, PresentationAdapter(_web, action=_action))
