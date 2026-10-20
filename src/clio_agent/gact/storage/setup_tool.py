"""Workspace inputs, provider accounts and permission-bound source access."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.gact import context
from clio_agent.gact.agents.tool_instrumentation import native_tool


def connected_data_status() -> dict[str, Any]:
    """Read workspace folders, attached sources and provider sign-in status.

    Workspace files do not require a connected source. An empty sources list
    does not mean that data is missing. This call never opens setup or signs in.
    If needed, present an account's login_action in an ordinary A2UI Button in
    the answer; only the user click opens private sign-in. No credentials are
    returned. Check again after the user completes setup; do not poll.
    NDP-specific tools are enabled on the next turn only when this workspace
    has a connected NDP dataset or a visible saved NDP Endpoint. Otherwise use
    Attach > NDP to select a dataset or connect an Endpoint first. Signing in
    alone does not activate those tools.
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
    from clio_agent.gact.storage.auth import application
    from clio_agent.gact.storage.ndp_oauth import configuration as ndp_configuration
    from clio_agent.gact.storage.ndp_selection import selection_summary
    from clio_agent.gact.storage.ndp_source import public_selection

    accounts = []
    for provider in ("github", "google_drive", "globus", "ndp"):
        authenticated = service.auth.account_connected(
            service.principal, service.store.clio_id, provider
        )
        accounts.append(
            {
                "provider": provider,
                "authenticated": authenticated,
                "sign_in_available": bool(
                    ndp_configuration()[1]
                    if provider == "ndp"
                    else (application(provider).client_id and application(provider).redirect_uri)
                ),
                "login_action": {
                    "name": f"data_source/login/{provider}",
                    "context": {
                        "clio_id": service.store.clio_id,
                        "workspace_id": session.workspace_id,
                    },
                },
            }
        )
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
                "source_kind": source.source_kind,
                "connection": source.connection,
                "knowledge_capabilities": {
                    "note_read": source.capabilities.note_read,
                    "note_search": source.capabilities.note_search,
                    "note_relations": source.capabilities.note_relations,
                    "note_write": record.linked_access != "read_only"
                    and source.source_kind == "knowledge",
                },
                "label": source.label,
                "selection": selection_summary(service, record),
                "mode": source.mode,
                "link_access": record.linked_access,
                "download_access": "read_only" if record.download_read_only else "editable",
                "connected": record.connected,
                "authenticated": service.auth.connected(record),
                "access_without_signin": source.provider == "ndp"
                and public_selection(service.store, record),
                "materialization": source.materialization,
                "revision": source.revision,
                "local_path": source.local_path,
                "linked": bool(record.linked_manifest_id),
                "linked_revision": record.linked_manifest_id,
                "open_references": {
                    "linked": {
                        "source_id": source.id,
                        "revision": record.linked_manifest_id,
                        "linked": True,
                    }
                    if record.linked_manifest_id
                    else None,
                    "downloaded": {
                        "source_id": source.id,
                        "revision": record.manifest_id,
                        "linked": False,
                    }
                    if record.manifest_id and source.local_path
                    else None,
                },
                "open_reference": {
                    "source_id": source.id,
                    "revision": record.linked_manifest_id or record.manifest_id,
                    "linked": bool(record.linked_manifest_id),
                }
                if record.linked_manifest_id or record.manifest_id
                else None,
                "owner": source.owner.model_dump(mode="json"),
            }
        )
    from clio_agent.gact.storage.ndp_tool_activation import session_ndp_tools_enabled

    return {
        "status": "available",
        "clio_id": service.store.clio_id,
        "workspace_id": session.workspace_id,
        "workspace_folders": [
            {
                "path": path,
                "role": "primary" if path == workspace.root_path else "additional",
                "access": "workspace",
            }
            for path in dict.fromkeys(
                [
                    *([workspace.root_path] if workspace.root_path else []),
                    *(getattr(workspace, "config", {}).get("granted_write_roots") or []),
                ]
            )
        ],
        "accounts": accounts,
        "sources": sources,
        "ndp_catalogs": [
            {"id": item.id, "name": item.name, "scope": item.server}
            for item in service.ndp.endpoints()
        ]
        if session_ndp_tools_enabled()
        else [],
        "ndp_endpoints": [
            row.model_dump(exclude={"schema_document", "principal"})
            for row in service.ndp_endpoints.connections(session.workspace_id)
        ],
    }


def setup_presentation(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    """Distinguish an accepted connection task from the source/account inventory."""
    row = structured if isinstance(structured, Mapping) else result
    if isinstance(row, Mapping):
        from clio_agent.gact.task_tools import task_submission_presentation

        if (submission := task_submission_presentation(row)) is not None:
            return submission
    if not isinstance(row, Mapping) or row.get("status") != "available":
        return {"summary": "Connected data is unavailable", "blocks": []}
    lines = ["### Workspace folders"]
    lines.extend(f"- `{folder['path']}` ({folder['role']})" for folder in row["workspace_folders"])
    lines.extend(["", "### Accounts", "| Provider | Sign-in |", "| --- | --- |"])
    lines.extend(
        f"| {account['provider']} | {'Signed in' if account['authenticated'] else 'Signed out'} |"
        for account in row["accounts"]
    )
    lines.extend(["", "### Attached sources"])
    lines.extend(
        f"- {source['label']} ({source['provider']}, {source['link_access']}, "
        f"{'connected' if source['connected'] else 'disconnected'})"
        for source in row["sources"]
    )
    if not row["sources"]:
        lines.append("No attached remote sources. Files may already be in the workspace folders.")
    connection = row.get("connection")
    state = connection.get("state") if isinstance(connection, Mapping) else None
    return {
        "summary": (
            "Source linked"
            if state == "linked"
            else "Sign-in required"
            if state == "sign_in_required"
            else "Workspace inputs and provider accounts"
        ),
        "blocks": [
            {
                "id": "source-inventory",
                "type": "markdown",
                "text": "\n".join(lines),
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
        raise ValueError(
            "This source has not been linked or downloaded. Use connected_data_download, "
            "wait for its task, then open with linked=false; or use an existing Link "
            "attachment from Attach with its returned revision and linked flag."
        )
    # Download results expose both a snapshot id and its content revision. Both
    # identify this exact current baseline; a linked revision is a separate index.
    valid_revisions = {manifest_id}
    if not linked and record.source.revision:
        valid_revisions.add(record.source.revision)
    if revision and revision not in valid_revisions:
        other_revisions = (
            {record.manifest_id, record.source.revision} if linked else {record.linked_manifest_id}
        )
        if revision in other_revisions:
            raise ValueError(
                "This revision belongs to the "
                + (
                    "downloaded snapshot; use linked=false"
                    if linked
                    else "linked index; use linked=true"
                )
                + ", or use connected_data_status's matching open_references entry."
            )
        raise ValueError(
            "This folder changed since it was attached; use connected_data_status "
            "to obtain the current approved open_references entry."
        )
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
            "linked": linked,
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
        presentation="source_opened",
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
        presentation="source_written",
        representation="row",
        domain="resources",
        title="Save linked file",
        read_only=False,
    )
