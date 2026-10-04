"""Trusted setup and source operations scoped to the currently connected CLIO."""

from __future__ import annotations

import asyncio
from pathlib import Path, PurePosixPath
from typing import Any, Callable, TypeVar

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from clio_agent import paths
from clio_agent.gact.storage.auth import application
from clio_agent.gact.storage.boundary import source_policy_change
from clio_agent.gact.storage.desktop_upload import UploadedSource, publish_uploaded_source
from clio_agent.gact.storage.globus import GlobusSource
from clio_agent.gact.storage.models import CreateSource, Manifest, SourceRecord, TransferOperation
from clio_agent.gact.storage.review import apply_review, review_changes
from clio_agent.gact.storage.service import StorageService, provider_capabilities

T = TypeVar("T")


class BrowseSource(BaseModel):
    """Browse a folder or search within the explicitly approved source root."""

    model_config = ConfigDict(extra="forbid")
    folder: str = ""
    query: str = ""
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=200)
    materialized: bool = False


class CompleteSignIn(BaseModel):
    """Trusted setup input: never registered as an agent-facing tool schema."""

    model_config = ConfigDict(extra="forbid")
    flow_id: str = Field(min_length=1, max_length=512)
    callback_url: str = Field(min_length=1, max_length=16384, repr=False)


class ApplyChanges(BaseModel):
    """Explicit selection from an inspected working-copy review."""

    model_config = ConfigDict(extra="forbid")
    review_id: str
    paths: list[str] = Field(min_length=1, max_length=10000)


class AttachSourceFile(BaseModel):
    """A selected file in an approved materialized source."""

    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)


def register_connected_storage_routes(app: FastAPI) -> None:
    """Register host-owned sources, trusted auth and separately named lifecycle operations."""
    service = StorageService(
        paths.user_data_dir() / "connected-sources",
        paths.user_config_dir() / "storage-auth" / "credentials.json",
    )
    app.state.connected_storage = service

    def workspace_root(wid: str) -> Path:
        workspace = app.state.workspaces.get(wid)
        if workspace is None:
            raise HTTPException(404, "Workspace not found on this CLIO")
        root = Path(workspace.root_path)
        if not root.is_absolute() or not root.is_dir():
            raise HTTPException(409, "The workspace directory is unavailable on this CLIO")
        return root

    def record_for(wid: str, source_id: str, *, connected: bool = True) -> SourceRecord:
        workspace_root(wid)
        try:
            return service.get(wid, source_id, connected=connected)
        except KeyError as exc:
            raise HTTPException(404, "Source not found on this CLIO workspace") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    async def run(action: Callable[[], T]) -> T:
        try:
            return await asyncio.to_thread(action)
        except KeyError as exc:
            raise HTTPException(404, "Storage reference not found") from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    async def change_access(action: Callable[[], T]) -> T:
        def update() -> T:
            with source_policy_change(getattr(app.state, "agent", None)):
                return action()

        return await run(update)

    def visible(record: SourceRecord) -> dict[str, Any]:
        return {
            **record.source.model_dump(mode="json"),
            "connected": record.connected,
            "origin": record.origin,
            "authenticated": record.source.provider in {"local", "sftp"}
            or service.auth.connected(record),
            "configuration": record.configuration.model_dump(),
        }

    def connect_write_grant(wid: str, record: SourceRecord) -> None:
        from clio_agent.gact.runtime.grants import apply_root_grant

        workspace = app.state.workspaces.get(wid)
        existing = getattr(workspace, "config", {}).get("granted_write_roots", [])
        record.owns_write_grant = record.source.root not in existing
        # Persist the owner before changing the workspace grant, for restart recovery.
        service.store.put("source", record.source.id, record)
        apply_root_grant(app, wid, record.source.root)

    def disconnect_write_grant(wid: str, record: SourceRecord) -> None:
        from clio_agent.gact.runtime.grants import revoke_root_grant

        if not record.owns_write_grant:
            return
        other = next(
            (
                row
                for row in service.sources(wid)
                if row.source.id != record.source.id
                and row.connected
                and row.source.mode == "write_enabled"
                and row.source.root == record.source.root
            ),
            None,
        )
        if other:
            other.owns_write_grant = True
            service.store.put("source", other.source.id, other)
        else:
            revoke_root_grant(app, wid, record.source.root)
        record.owns_write_grant = False

    @app.get("/v1/storage/providers")
    async def providers() -> dict[str, Any]:
        """Return reusable provider definitions, including honest setup prerequisites."""
        definitions = []
        for identifier, label, logo in [
            ("local", "Files on this CLIO", "folder"),
            ("sftp", "SSH / SFTP", "ssh"),
            ("google_drive", "Google Drive", "google-drive"),
            ("globus", "Globus", "globus"),
        ]:
            browser = identifier in {"google_drive", "globus"}
            config = application(identifier) if browser else None
            configured = not config or bool(config.client_id and config.redirect_uri)
            definitions.append(
                {
                    "id": identifier,
                    "name": label,
                    "logo": logo,
                    "authentication": "browser"
                    if browser
                    else "ssh_profile"
                    if identifier == "sftp"
                    else "none",
                    "configured": configured,
                    "capabilities": provider_capabilities(identifier).model_dump(),
                    "setup_requirement": None
                    if configured
                    else "CLIO's distributor must register its sign-in application before this connector can be used.",
                }
            )
        return {"clio_id": service.store.clio_id, "host_id": "local", "providers": definitions}

    @app.get("/v1/workspaces/{wid}/sources")
    async def sources(wid: str) -> dict[str, Any]:
        """List connected sources and retained disconnected copies."""
        workspace_root(wid)
        return {"sources": [visible(row) for row in await run(lambda: service.sources(wid))]}

    @app.post("/v1/workspaces/{wid}/sources", status_code=201)
    async def create_source(wid: str, body: CreateSource) -> dict[str, Any]:
        """Approve a source; transferring it remains a separate explicit operation."""
        root = workspace_root(wid)

        def create() -> SourceRecord:
            record = service.create(wid, body, root)
            if record.source.mode == "write_enabled":
                try:
                    connect_write_grant(wid, record)
                except Exception:
                    record.connected = False
                    service.store.put("source", record.source.id, record)
                    raise
            return record

        return visible(await change_access(create))

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/browse")
    async def browse_source(wid: str, source_id: str, body: BrowseSource) -> dict[str, Any]:
        """Return bounded entries with stable source-relative paths."""
        record = record_for(wid, source_id, connected=not body.materialized)

        def browse() -> dict[str, Any]:
            if body.materialized and record.source.mode != "write_enabled":
                if not record.manifest_id or not record.source.local_path:
                    raise ValueError("Transfer this source before browsing its approved inputs")
                rows = service.store.get("manifest", record.manifest_id, Manifest).entries
            else:
                if not record.connected:
                    raise ValueError("Reconnect this live source before browsing it")
                with service.adapter(record) as adapter:
                    rows = adapter.entries()
            folder = body.folder.strip("/")
            rows = [
                row
                for row in rows
                if (
                    body.query.casefold() in row.path.casefold()
                    if body.query
                    else (
                        str(PurePosixPath(row.path).parent)
                        if str(PurePosixPath(row.path).parent) != "."
                        else ""
                    )
                    == folder
                )
            ]
            return {
                "source_id": source_id,
                "entries": [
                    row.model_dump() for row in rows[body.offset : body.offset + body.limit]
                ],
                "next_offset": body.offset + body.limit
                if body.offset + body.limit < len(rows)
                else None,
            }

        return await run(browse)

    @app.post("/v1/workspaces/{wid}/sources/from-uploads", status_code=201)
    async def source_from_uploads(wid: str, body: UploadedSource) -> dict[str, Any]:
        """Publish an explicit desktop folder transfer from validated workspace custody."""
        root = workspace_root(wid)
        return visible(
            await run(
                lambda: publish_uploaded_source(
                    service.store, app.state.resource_store, wid, root, service.principal, body
                )
            )
        )

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/auth/start")
    async def start_auth(wid: str, source_id: str) -> dict[str, str]:
        """Open provider sign-in only from the trusted user setup surface."""
        return await run(lambda: service.auth.start(record_for(wid, source_id)))

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/reference")
    async def reference(
        wid: str, source_id: str, body: AttachSourceFile, background_tasks: BackgroundTasks
    ) -> dict[str, Any]:
        """Attach approved bytes with source identity through ordinary resource custody."""
        from clio_agent.gact.resource_lifecycle import emit_workspace_event, schedule_processing
        from clio_agent.gact.resource_materialization import materialize_once
        from clio_agent.gact.storage.references import source_resource

        record = record_for(wid, source_id, connected=False)
        resource = await run(
            lambda: source_resource(service.store, app.state.resource_store, record, body.path)
        )
        resource = await run(lambda: materialize_once(app, resource))
        payload = resource.to_wire()
        emit_workspace_event(app, wid, "resource.ready", payload)
        schedule_processing(app, resource, background_tasks)
        return payload

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/auth/complete")
    async def complete_auth(wid: str, source_id: str, body: CompleteSignIn) -> dict[str, Any]:
        """Consume authorization without returning tokens or including them in source metadata."""
        record = record_for(wid, source_id)
        await change_access(lambda: service.auth.complete(record, body.flow_id, body.callback_url))
        return {"authenticated": True, "source_id": source_id}

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/transfer", status_code=202)
    async def transfer(wid: str, source_id: str) -> dict[str, Any]:
        """Start an explicit refresh, retaining previous inputs until a complete copy is ready."""
        record = record_for(wid, source_id)
        try:
            operation = service.start_transfer(record, workspace_root(wid))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return operation.model_dump()

    @app.get("/v1/workspaces/{wid}/sources/{source_id}/operations")
    async def operations(wid: str, source_id: str) -> dict[str, Any]:
        """Read durable transfer progress and native task IDs."""
        record_for(wid, source_id, connected=False)
        rows = await run(lambda: service.store.list("operation", TransferOperation))
        return {
            "operations": [
                row.model_dump(exclude={"native_request"})
                for row in rows
                if row.source_id == source_id
            ]
        }

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/operations/{operation_id}/cancel")
    async def cancel(wid: str, source_id: str, operation_id: str) -> dict[str, Any]:
        """Request cancellation; completion/cleanup waits for the transfer owner's confirmation."""
        record_for(wid, source_id, connected=False)
        operation = await run(
            lambda: service.store.get("operation", operation_id, TransferOperation)
        )
        if operation.source_id != source_id:
            raise HTTPException(404, "Transfer not found for this source")
        return (
            await run(lambda: service.store.update_operation(operation_id, cancel_requested=True))
        ).model_dump(exclude={"native_request"})

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/review")
    async def review(wid: str, source_id: str) -> dict[str, Any]:
        """Inspect local changes and upstream conflicts without applying anything."""
        record = record_for(wid, source_id)

        def inspect() -> dict[str, Any]:
            with service.adapter(record) as adapter:
                if isinstance(adapter, GlobusSource):
                    raise ValueError("This collection does not support reviewed source updates")
                return review_changes(service.store, record, adapter).model_dump()

        return await run(inspect)

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/apply")
    async def apply(wid: str, source_id: str, body: ApplyChanges) -> dict[str, str]:
        """Apply exactly the files the user selected from a current review."""
        record = record_for(wid, source_id)

        def update() -> str:
            with service.adapter(record) as adapter:
                if isinstance(adapter, GlobusSource):
                    raise ValueError("This collection does not support reviewed source updates")
                return apply_review(service.store, record, adapter, body.review_id, body.paths)

        return {"operation_id": await run(update)}

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/disconnect")
    async def disconnect(wid: str, source_id: str) -> dict[str, Any]:
        """Disconnect credentials and live access, retaining copied inputs and baselines."""
        record = record_for(wid, source_id, connected=False)

        def detach() -> SourceRecord:
            if record.source.mode == "write_enabled":
                disconnect_write_grant(wid, record)
            return service.disconnect(record)

        return visible(await change_access(detach))

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/reconnect")
    async def reconnect(wid: str, source_id: str) -> dict[str, Any]:
        """Reconnect the same identity; browser sources require a new sign-in after disconnect."""
        record = record_for(wid, source_id, connected=False)

        def attach() -> SourceRecord:
            if record.source.mode == "write_enabled":
                connect_write_grant(wid, record)
            record.connected = True
            service.store.put("source", source_id, record)
            return record

        return visible(await change_access(attach))

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/remove-copy")
    async def remove_copy(wid: str, source_id: str) -> dict[str, Any]:
        """Remove the owned working copy, never upstream data or baseline evidence."""
        record = record_for(wid, source_id, connected=False)
        return visible(await run(lambda: service.remove_copy(record, workspace_root(wid))))
