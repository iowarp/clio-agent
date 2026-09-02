"""Bounded operations over workspace-owned resources — the single owner.

These functions expose resource identity, searchable text, structured document
outlines, and individual structured nodes without revealing custody paths or
returning unbounded original bytes. They are deliberately read-only: upload,
deletion, and provider delivery remain user-authorized HTTP/message operations.

**One implementation per operation.** The HTTP routes
(``GET .../search``, ``.../structure``, ``.../structure/{collection}/{index}``)
call straight into these functions rather than reimplementing them. Two copies
had already drifted: different byte caps, and — worse — different readiness
gates, so the enrichment block told the model to use a tool that refused in a
state the route happily served. The readiness gate is
``derivatives_available`` on BOTH surfaces: a completed conversion whose LATER
refresh failed still has usable derivatives, and refusing them because the
newest attempt failed loses work that is sitting on disk.

Bounds are configuration (``resources.*``), and one key feeds both surfaces of
the same semantic bound.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent import conf
from clio_agent.gact.resource_custody import ResourceRecord

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import AgentDef


class ResourceQueryError(ValueError):
    """A typed refusal from a bounded resource operation.

    Subclasses :class:`ValueError` so the native tool surface keeps reporting
    the message to the model unchanged, while the HTTP layer can map ``code`` to
    an exact status instead of guessing from prose (or returning a 500).
    """

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def text_scan_max_bytes() -> int:
    """Byte ceiling on text CLIO will linearly scan (search and direct read)."""

    return conf.resolve(
        "resources.text_scan_bytes",
        env="CLIO_RESOURCE_TEXT_SCAN_BYTES",
        default=2 * 1024 * 1024,
        cast=conf.as_int,
    )


def text_read_max_chars() -> int:
    """Character ceiling on one direct text read response."""

    return conf.resolve(
        "resources.text_read_chars",
        env="CLIO_RESOURCE_TEXT_READ_CHARS",
        default=64 * 1024,
        cast=conf.as_int,
    )


def search_match_limit() -> int:
    """Matches returned by one bounded resource search before truncation."""

    return conf.resolve(
        "resources.search_match_limit",
        env="CLIO_RESOURCE_SEARCH_MATCH_LIMIT",
        default=50,
        cast=conf.as_int,
    )


def search_excerpt_chars() -> int:
    """Characters of each matching line returned by a bounded resource search."""

    return conf.resolve(
        "resources.search_excerpt_chars",
        env="CLIO_RESOURCE_SEARCH_EXCERPT_CHARS",
        default=500,
        cast=conf.as_int,
    )


def _record(app: "FastAPI", workspace_id: str, resource_id: str) -> ResourceRecord:
    record = app.state.resource_store.get(workspace_id, resource_id)
    if record is None:
        raise ResourceQueryError(
            "not_found",
            f"resource not found in this workspace: {resource_id}",
            resource_id=resource_id,
        )
    return record


def _read_text(path: Path) -> str:
    """Read bounded UTF-8 text, typing a decode failure instead of raising 500.

    The custody MIME sniff only sees the first 8 KiB, so a file that looks like
    text can still carry an invalid byte later on. That is a typed refusal
    (``resource_not_decodable``), not a server error.
    """

    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ResourceQueryError(
            "resource_not_decodable",
            "resource is not valid UTF-8 text beyond its detected prefix",
            detail=str(exc),
        ) from exc


def list_workspace_resources(app: "FastAPI", workspace_id: str) -> dict[str, Any]:
    """Return bounded metadata for resources owned by one workspace."""

    rows = app.state.resource_store.list(workspace_id)
    return {
        "workspace_id": workspace_id,
        "resources": [row.to_wire() for row in rows[:100]],
        "truncated": len(rows) > 100,
    }


def inspect_workspace_resource(
    app: "FastAPI", workspace_id: str, resource_id: str
) -> dict[str, Any]:
    """Return custody, processing, derivative, and delivery metadata."""

    record = _record(app, workspace_id, resource_id)
    processing = app.state.resource_processing_store.state(record)
    manifest = app.state.resource_processing_store.manifest(record) or {}
    derivatives = [
        {key: value for key, value in row.items() if key != "content_path"}
        for row in manifest.get("entries", [])
        if isinstance(row, dict)
    ]
    deliveries = [
        row.model_dump()
        for row in app.state.resource_delivery_store.list(workspace_id)
        if row.resource_id == resource_id and row.resource_revision == record.revision
    ]
    return {
        "resource": record.to_wire(),
        "processing": processing.model_dump(),
        "derivatives": derivatives,
        "deliveries": deliveries,
    }


def _search_path(path: Path, query: str) -> tuple[list[dict[str, Any]], bool]:
    scan_bytes = text_scan_max_bytes()
    if path.stat().st_size > scan_bytes:
        raise ResourceQueryError(
            "search_input_too_large",
            "text representation exceeds the bounded search limit; use structured nodes",
            max_scan_bytes=scan_bytes,
        )
    needle = query.strip().casefold()
    if not needle:
        raise ResourceQueryError("invalid_request", "search query cannot be empty")
    match_limit = search_match_limit()
    excerpt_chars = search_excerpt_chars()
    matches: list[dict[str, Any]] = []
    for line_number, line in enumerate(_read_text(path).splitlines(), start=1):
        if needle in line.casefold():
            matches.append({"line": line_number, "text": line[:excerpt_chars]})
        if len(matches) >= match_limit:
            return matches, True
    return matches, False


def _text_path(app: "FastAPI", record: ResourceRecord, derivative_id: str) -> tuple[Path, str]:
    """Resolve the textual original or one named textual derivative to a path."""

    if not derivative_id:
        if not record.detected_mime.startswith("text/"):
            raise ResourceQueryError(
                "search_unavailable",
                "original resource is not text; choose a named textual derivative",
                detected_mime=record.detected_mime,
            )
        return app.state.resource_store.content_path(record), "original"
    try:
        path, derivative = app.state.resource_processing_store.derivative_path(
            record, derivative_id
        )
    except KeyError as exc:
        raise ResourceQueryError(
            "derivative_not_found",
            f"text derivative not found: {derivative_id}",
            derivative_id=derivative_id,
        ) from exc
    media_type = str(derivative.get("media_type") or "")
    if not (media_type.startswith("text/") or media_type == "application/json"):
        raise ResourceQueryError(
            "search_unavailable",
            "selected derivative is not textual",
            derivative_id=derivative_id,
            media_type=media_type,
        )
    return path, derivative_id


def search_workspace_resource(
    app: "FastAPI",
    workspace_id: str,
    resource_id: str,
    query: str,
    derivative_id: str = "",
) -> dict[str, Any]:
    """Search bounded original text or one named textual derivative."""

    record = _record(app, workspace_id, resource_id)
    path, representation = _text_path(app, record, derivative_id)
    matches, truncated = _search_path(path, query)
    return {
        "resource_id": resource_id,
        "revision": record.revision,
        "representation": representation,
        "query": query,
        "matches": matches,
        "truncated": truncated,
    }


def read_workspace_resource_text(
    app: "FastAPI",
    workspace_id: str,
    resource_id: str,
    derivative_id: str = "",
) -> dict[str, Any]:
    """Read one bounded textual original or named textual derivative."""

    record = _record(app, workspace_id, resource_id)
    path, representation = _text_path(app, record, derivative_id)
    scan_bytes = text_scan_max_bytes()
    if path.stat().st_size > scan_bytes:
        raise ResourceQueryError(
            "read_input_too_large",
            "text representation exceeds the bounded read limit; use search or structure tools",
            max_scan_bytes=scan_bytes,
        )
    read_chars = text_read_max_chars()
    content = _read_text(path)
    return {
        "resource_id": resource_id,
        "revision": record.revision,
        "representation": representation,
        "content": content[:read_chars],
        "total_chars": len(content),
        "truncated": len(content) > read_chars,
    }


def read_workspace_resource_structure(
    app: "FastAPI",
    workspace_id: str,
    resource_id: str,
    collection: str = "",
    index: int = -1,
) -> dict[str, Any]:
    """Return the structure outline or one bounded structured node.

    The gate is ``derivatives_available``, matching the HTTP sibling: a resource
    whose conversion completed and whose LATEST refresh failed or was cancelled
    still has a persisted structure, and the enrichment block has already told
    the model to read it.
    """

    record = _record(app, workspace_id, resource_id)
    processing = app.state.resource_processing_store.state(record)
    if not processing.derivatives_available:
        return {
            "resource_id": resource_id,
            "revision": record.revision,
            "processing": processing.model_dump(),
            "available": False,
        }
    if not collection:
        return {
            **app.state.resource_processing_store.structure_outline(record),
            "available": True,
        }
    if index < 0:
        raise ResourceQueryError(
            "invalid_request", "index must be zero or greater when collection is supplied"
        )
    try:
        node = app.state.resource_processing_store.node(record, collection, index)
    except (FileNotFoundError, IndexError, KeyError) as exc:
        raise ResourceQueryError(
            "structure_node_not_found",
            f"structured node not found: {collection}[{index}]",
            collection=collection,
            index=index,
        ) from exc
    except ValueError as exc:
        raise ResourceQueryError("structure_node_too_large", str(exc)) from exc
    return {
        "resource_id": resource_id,
        "revision": record.revision,
        "collection": collection,
        "index": index,
        "node": node,
        "available": True,
    }


def build_resource_tools(agent_def: "AgentDef") -> list[Any]:
    """Build the read-only resource tools attached to an active React agent."""

    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415
    from clio_agent.gact.artifacts.minting import _session_workspace_id  # noqa: PLC0415

    del agent_def

    def active() -> tuple[Any, str]:
        app = _ctx.active_app()
        session_id = _ctx.active_session_id()
        if app is None or not session_id:
            raise ValueError("workspace resource tool called outside an active session")
        return app, _session_workspace_id(app, session_id)

    def resource_list() -> dict[str, Any]:
        app, workspace_id = active()
        return list_workspace_resources(app, workspace_id)

    def resource_inspect(resource_id: str) -> dict[str, Any]:
        app, workspace_id = active()
        return inspect_workspace_resource(app, workspace_id, resource_id)

    def resource_search(resource_id: str, query: str, derivative_id: str = "") -> dict[str, Any]:
        app, workspace_id = active()
        return search_workspace_resource(app, workspace_id, resource_id, query, derivative_id)

    def resource_read(resource_id: str, derivative_id: str = "") -> dict[str, Any]:
        app, workspace_id = active()
        return read_workspace_resource_text(app, workspace_id, resource_id, derivative_id)

    def resource_structure(
        resource_id: str, collection: str = "", index: int = -1
    ) -> dict[str, Any]:
        app, workspace_id = active()
        return read_workspace_resource_structure(app, workspace_id, resource_id, collection, index)

    return [
        native_tool(
            resource_list,
            name="workspace_resource_list",
            title="List Uploaded Resources",
            representation="row",
            desc=(
                "List uploaded resources owned by this workspace. Returns immutable resource "
                "IDs, revisions, server-detected media types, hashes, sizes, and readiness."
            ),
            args={},
        ),
        native_tool(
            resource_inspect,
            name="workspace_resource_inspect",
            title="Inspect Uploaded Resource",
            representation="row",
            desc=(
                "Inspect one uploaded resource without reading its bytes. Returns custody, "
                "structured-processing, derivative, and provider-delivery provenance."
            ),
            args={"resource_id": {"type": "string", "description": "Immutable resource id."}},
        ),
        native_tool(
            resource_read,
            name="workspace_resource_read",
            title="Read Uploaded Text",
            representation="row",
            desc=(
                "Read a bounded textual upload or named Docling textual derivative. Use this "
                "instead of filesystem tools because uploaded-resource custody paths are private."
            ),
            args={
                "resource_id": {"type": "string", "description": "Immutable resource id."},
                "derivative_id": {
                    "type": "string",
                    "description": "Optional named textual derivative; omit for original text.",
                },
            },
        ),
        native_tool(
            resource_search,
            name="workspace_resource_search",
            title="Search Uploaded Resource",
            representation="row",
            desc=(
                "Search bounded original text or a named Docling textual derivative. Use "
                "workspace_resource_inspect first to discover derivative IDs."
            ),
            args={
                "resource_id": {"type": "string", "description": "Immutable resource id."},
                "query": {"type": "string", "description": "Case-insensitive text query."},
                "derivative_id": {
                    "type": "string",
                    "description": "Optional named textual derivative; omit for original text.",
                },
            },
        ),
        native_tool(
            resource_structure,
            name="workspace_resource_structure",
            title="Read Document Structure",
            representation="row",
            desc=(
                "Read the bounded Docling outline for an uploaded document, or one exact page, "
                "table, figure, or text node by collection and zero-based index."
            ),
            args={
                "resource_id": {"type": "string", "description": "Immutable resource id."},
                "collection": {
                    "type": "string",
                    "description": "Optional pages|tables|pictures|texts collection.",
                },
                "index": {
                    "type": "integer",
                    "description": "Zero-based node index; omit for the outline.",
                },
            },
        ),
    ]


__all__ = [
    "ResourceQueryError",
    "build_resource_tools",
    "inspect_workspace_resource",
    "list_workspace_resources",
    "read_workspace_resource_text",
    "read_workspace_resource_structure",
    "search_workspace_resource",
]
