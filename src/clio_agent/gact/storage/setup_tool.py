"""Read-only agent handoff to trusted, owner-bound connected-data setup."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.gact import context
from clio_agent.gact.agents.tool_instrumentation import native_tool


def connected_data_status() -> dict[str, Any]:
    """Show the trusted Connect data action and read approved source status.

    The user selects folders and signs in privately in the connected-data UI.
    This tool cannot authorize access, accept credentials, or change sources.
    Call it again after setup to obtain approved source references and local
    materialization paths. Do not poll; wait for the user to finish setup.
    """
    app = context.active_app()
    sid = context.active_session_id()
    if app is None or not sid:
        raise ValueError("Connected-data setup requires an active CLIO workspace session")
    session = app.state.sessions.get(sid)
    service = getattr(app.state, "connected_storage", None)
    if session is None or service is None:
        raise ValueError("Connected-data setup requires an active CLIO workspace session")
    workspace = app.state.workspaces.get(session.workspace_id)
    if workspace is None:
        raise ValueError("The session workspace is unavailable on this CLIO")
    sources = []
    for record in service.sources(session.workspace_id):
        source = record.source
        # Never serialize private records, authentication objects, configuration,
        # callbacks or upstream locators into an observed tool result.
        if source.owner.clio_id != service.store.clio_id:
            continue
        sources.append(
            {
                "id": source.id,
                "provider": source.provider,
                "label": source.label,
                "mode": source.mode,
                "link_access": record.linked_access,
                "download_access": "read_only" if record.download_read_only else "editable",
                "connected": record.connected,
                "materialization": source.materialization,
                "revision": source.revision,
                "local_path": source.local_path,
                "linked": bool(record.linked_manifest_id),
                "linked_revision": record.linked_manifest_id,
                "owner": source.owner.model_dump(mode="json"),
            }
        )
    return {
        "status": "setup_available",
        "clio_id": service.store.clio_id,
        "workspace_id": session.workspace_id,
        "sources": sources,
    }


def setup_presentation(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    """Offer a native setup link without any sign-in URL or credential payload."""
    row = structured if isinstance(structured, Mapping) else result
    if not isinstance(row, Mapping) or row.get("status") != "setup_available":
        return {"summary": "Connected data is unavailable", "blocks": []}
    return {
        "summary": "Choose data for this workspace",
        "blocks": [
            {
                "id": "connected-data",
                "type": "link",
                "target": "connected_data",
                "uri": row["clio_id"],
                "workspace_id": row["workspace_id"],
                "label": "Connect data",
            }
        ],
    }


def build_connected_data_status_tool() -> Any:
    """Build the credential-free setup/status tool for the main session."""
    return native_tool(
        connected_data_status,
        name="connected_data_status",
        desc=connected_data_status.__doc__,
        args={},
        presentation=setup_presentation,
        domain="resources",
        title="Connected data",
        read_only=True,
    )


def connected_data_open(
    source_id: str,
    path: str = "",
    offset: int = 0,
    revision: str = "",
    linked: bool = True,
    folder: str = "",
) -> dict[str, Any]:
    """List an attached folder or open one file through the approved storage path.

    Omit path to list metadata (200 entries per page). A file path fetches only that
    file and returns an immutable resource reference for workspace_resource_read or
    workspace_resource_inspect. Use the revision and linked flag from the folder
    attachment. Pass its folder field to restrict listing and reads to that folder.
    File paths remain relative to the source root, as shown in the index.
    Linked files use fsspec; downloaded files use the approved baseline.
    This does not authorize new sources or expose credentials. Linked paths are
    virtual CLIO paths, not operating-system mounts for shell commands.
    """
    from clio_agent.gact.storage.linked import linked_file
    from clio_agent.gact.storage.models import Manifest
    from clio_agent.gact.storage.references import folder_entries, source_resource

    app = context.active_app()
    sid = context.active_session_id()
    session = app.state.sessions.get(sid) if app is not None and sid else None
    if app is None or session is None:
        raise ValueError("Linked data requires an active workspace session")
    service = app.state.connected_storage
    record = service.get(session.workspace_id, source_id, connected=linked)
    manifest_id = record.linked_manifest_id if linked else record.manifest_id
    if not manifest_id or (not linked and not record.source.local_path):
        raise ValueError("Ask the user to link or download this folder in Attach first")
    if revision and revision != manifest_id:
        raise ValueError("This folder changed since it was attached; ask for an updated reference")
    manifest = service.store.get("manifest", manifest_id, Manifest)
    entries = folder_entries(manifest, folder)
    if path and folder and not path.startswith(folder + "/"):
        raise ValueError("Select a file within the attached folder")
    if not path:
        if offset < 0:
            raise ValueError("Offset must be nonnegative")
        return {
            "source_id": source_id,
            "revision": manifest.id,
            "folder": folder,
            "access": record.linked_access
            if linked
            else ("read_only" if record.download_read_only else "editable"),
            "writes_propagate": linked and record.linked_access == "write_through",
            "entries": [
                entry.model_dump(exclude={"locator"}) for entry in entries[offset : offset + 200]
            ],
            "next_offset": offset + 200 if offset + 200 < len(entries) else None,
        }
    selected = linked_file(service, record, path) if linked else None
    resource = source_resource(
        service.store, app.state.resource_store, record, path, linked_path=selected
    )
    return {
        "resource_id": resource.id,
        "name": resource.name,
        "sha256": resource.sha256,
        "source_id": source_id,
        "path": path,
        "revision": manifest_id,
    }


def build_connected_data_open_tool() -> Any:
    """Reuse the approved folder and existing resource read pipeline for agent access."""
    return native_tool(
        connected_data_open,
        name="connected_data_open",
        presentation="resource",
        representation="row",
        desc=connected_data_open.__doc__,
        args={
            "source_id": {
                "type": "string",
                "description": "Approved source id from connected_data_status.",
            },
            "path": {
                "type": "string",
                "description": "File path from its listing; omit to list files.",
            },
            "offset": {"type": "integer", "description": "Optional listing offset."},
            "revision": {"type": "string", "description": "Revision in the folder attachment."},
            "folder": {"type": "string", "description": "Folder field from the attachment index."},
            "linked": {
                "type": "boolean",
                "description": "The attachment's linked flag; false reads downloaded inputs.",
            },
        },
        domain="resources",
        title="Open linked data",
        read_only=True,
    )


def connected_data_write(source_id: str, path: str, content: str, revision: str) -> dict[str, Any]:
    """Save UTF-8 text through a user-approved editable folder link.

    Check access from connected_data_open: publish_later stages edits in CLIO until
    the user reviews and publishes them; write_through changes the originals on save.
    Originals are retained for provenance. Use the current returned revision for the
    next write. Read-only links reject writes. Downloads are detached workspace copies;
    edit an editable download normally. Never publish staged edits, request credentials,
    or change permissions on the user's behalf. GitHub write_through creates a commit
    per save; publish_later groups the user's selected edits into one commit.
    """
    from clio_agent.gact.storage.source_filesystem import SourceFileSystem

    app = context.active_app()
    sid = context.active_session_id()
    session = app.state.sessions.get(sid) if app is not None and sid else None
    if app is None or session is None:
        raise ValueError("Linked data requires an active workspace session")
    if not revision:
        raise ValueError("Open the linked folder to obtain its current revision first")
    fs = SourceFileSystem(
        app.state.connected_storage,
        session.workspace_id,
        source_id,
        app.state.resource_store,
        revision=revision,
    )
    with fs.open(path, "wb") as writer:
        writer.write(content.encode("utf-8"))
    return {
        "source_id": source_id,
        "path": path,
        "revision": fs.revision,
        "operation_id": fs.last_operation,
        "writes_propagate": fs._record().linked_access == "write_through",
    }


def build_connected_data_write_tool() -> Any:
    """Expose the same fsspec write boundary with an effectful native declaration."""
    return native_tool(
        connected_data_write,
        name="connected_data_write",
        desc=connected_data_write.__doc__,
        args={
            "source_id": {"type": "string", "description": "Approved writable linked source."},
            "path": {"type": "string", "description": "Path relative to the linked source root."},
            "content": {"type": "string", "description": "Complete replacement UTF-8 text."},
            "revision": {"type": "string", "description": "Current linked folder revision."},
        },
        presentation="resource",
        representation="row",
        domain="resources",
        title="Save linked file",
        read_only=False,
    )
