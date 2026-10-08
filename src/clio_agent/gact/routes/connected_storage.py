"""Trusted setup and source operations scoped to the currently connected CLIO."""

from __future__ import annotations

import asyncio
from pathlib import Path, PurePosixPath
from typing import Any, Callable, TypeVar
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from clio_agent import paths
from clio_agent.gact.infrastructure.models import CommandResult, CommandSpec
from clio_agent.gact.routes.storage_inputs import (
    ApplyChanges,
    AttachSourceFile,
    BrowseSource,
    CompleteSignIn,
    DownloadSelection,
    DraftSelection,
    GitHubRevisions,
    StartSignIn,
)
from clio_agent.gact.routes.storage_sftp import register_storage_sftp_routes
from clio_agent.gact.storage.auth import application
from clio_agent.gact.storage.boundary import source_policy_change
from clio_agent.gact.storage.desktop_upload import UploadedSource, publish_uploaded_source
from clio_agent.gact.storage.drafts import SourceDrafts
from clio_agent.gact.storage.github_oauth import account_url
from clio_agent.gact.storage.github_revisions import github_revisions
from clio_agent.gact.storage.globus import GlobusSource
from clio_agent.gact.storage.globus_destination import GlobusDestination, destination_status
from clio_agent.gact.storage.linked_changes import (
    discard_edits,
    pending_edits,
    publish_edits,
    review_edits,
)
from clio_agent.gact.storage.mapping import mapping_options
from clio_agent.gact.storage.models import (
    CreateSource,
    Manifest,
    SftpCredentials,
    SourceRecord,
    TransferOperation,
)
from clio_agent.gact.storage.service import StorageService, provider_capabilities
from clio_agent.gact.storage.sftp import SftpSource
from clio_agent.gact.storage.ssh_target import SshTargetSource
from clio_agent.gact.storage.task_adapter import validate_task_owner

T = TypeVar("T")


def register_connected_storage_routes(app: FastAPI) -> None:
    """Register host-owned sources, trusted auth and separately named lifecycle operations."""
    service = StorageService(
        paths.user_data_dir() / "connected-sources",
        paths.user_config_dir() / "storage-auth" / "credentials.json",
    )
    app.state.connected_storage = service
    controller_loop: asyncio.AbstractEventLoop | None = None
    source_changes = asyncio.Lock()

    def drafts() -> SourceDrafts:
        owner = getattr(app.state, "source_drafts", None)
        if owner is None:
            owner = SourceDrafts(service, app.state.resource_store)
            app.state.source_drafts = owner
            service.transfer_settled = owner.cleanup
        return owner

    def target_source(record: SourceRecord) -> SshTargetSource | SftpSource:
        target_id = record.configuration.target_id
        target = app.state.infrastructure_store.target(target_id)
        if target is None or target.kind != "ssh" or target.ssh is None:
            raise ValueError("Select a saved SSH connection on this CLIO")
        route = target.ssh.model_dump_json()
        if record.target_route and record.target_route != route:
            raise ValueError(
                "This SSH connection changed. Connect the folder again to approve its new host."
            )
        record.target_route = route
        if record.configuration.ssh_origin == "clio":
            return SftpSource.from_route(
                target.ssh,
                record.source.root,
                credentials=service.auth.sftp_credentials(record),
                writable=record.source.mode == "write_enabled",
            )

        def execute(spec: CommandSpec) -> CommandResult:
            current = app.state.infrastructure_store.target(target_id)
            if current is None or current.ssh is None or current.ssh.model_dump_json() != route:
                raise ValueError("The selected SSH connection changed")
            if controller_loop is None or controller_loop.is_closed():
                raise ValueError("Reconnect the selected SSH host before accessing files")
            future = asyncio.run_coroutine_threadsafe(
                app.state.infrastructure_runtime.execute_on_target(target_id, spec), controller_loop
            )
            try:
                return future.result(timeout=spec.timeout_seconds + 10)
            except TimeoutError:
                future.cancel()
                raise ValueError("The selected SSH host did not respond") from None

        return SshTargetSource(
            record.source.root, execute, windows=target.ssh.platform == "windows"
        )

    service.target_source = target_source
    register_storage_sftp_routes(app)

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
        nonlocal controller_loop
        controller_loop = asyncio.get_running_loop()
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

        async with source_changes:
            return await run(update)

    def start_browser_auth(
        request: Request, record: SourceRecord, body: StartSignIn | None
    ) -> dict[str, Any]:
        desktop_redirect = body.desktop_redirect if body else None
        app_config = application(record.source.provider)
        local_browser = False
        if not desktop_redirect and record.source.provider == "google_drive":
            redirect = urlparse(app_config.redirect_uri)
            if redirect.scheme == "http" and redirect.hostname in {"127.0.0.1", "localhost", "::1"}:
                local_hosts = {"127.0.0.1", "localhost", "::1"}
                origin = urlparse(request.headers.get("origin", str(request.base_url)))
                local_browser = (
                    request.url.hostname in local_hosts and origin.hostname in local_hosts
                )
                if not local_browser:
                    raise ValueError(
                        "This Google sign-in needs CLIO Desktop or a browser on the CLIO machine. "
                        "Remote browser sign-in requires a configured HTTPS return address."
                    )
        return service.auth.start(
            record, app_config, desktop_redirect=desktop_redirect, local_browser=local_browser
        )

    def visible(record: SourceRecord) -> dict[str, Any]:
        link_available = record.origin != "desktop_upload"
        if record.source.provider == "sftp":
            link_available = link_available and (
                not record.configuration.target_id or record.configuration.ssh_origin == "clio"
            )
        return {
            **record.source.model_dump(mode="json"),
            **({"account_url": account_url()} if record.source.provider == "github" else {}),
            "connected": record.connected,
            "origin": record.origin,
            "authenticated": record.source.provider == "local"
            or (
                record.source.provider == "sftp"
                and record.configuration.ssh_authentication == "configured"
            )
            or service.auth.connected(record),
            "account_authenticated": record.source.provider in {"google_drive", "globus", "github"}
            and service.auth.account_connected(
                record.principal,
                record.source.owner.clio_id,
                record.source.provider,
                record.source.owner.host_id,
            ),
            "access_without_signin": record.source.provider in {"google_drive", "github"}
            and record.source.mode == "read_only"
            and not record.sign_in_required,
            "configuration": record.configuration.model_dump(),
            "can_edit_location": service.can_edit_location(record),
            "link_access": record.linked_access,
            "download_access": "read_only" if record.download_read_only else "editable",
            "pending_edits": len(pending_edits(service, record).changes),
            "linked": bool(record.linked_manifest_id),
            "link_revision": record.linked_manifest_id,
            "link_available": link_available,
            "download_available": service.download_available(record),
        }

    def connect_write_grant(wid: str, record: SourceRecord) -> None:
        # Live edits use SourceFileSystem, which captures originals before writing.
        # Revoke any old raw OS grant that would bypass that custody boundary.
        disconnect_write_grant(wid, record)

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

    @app.post("/v1/storage/github/revisions")
    async def revisions(body: GitHubRevisions) -> dict[str, Any]:
        """Discover revisions without granting workspace access or writing a source."""
        return await run(lambda: github_revisions(service, body.url, body.kind, body.page))

    @app.get("/v1/storage/providers")
    async def providers() -> dict[str, Any]:
        """Return reusable provider definitions, including honest setup prerequisites."""
        definitions = []
        for identifier, label, logo in [
            ("local", "Files on this CLIO", "folder"),
            ("sftp", "SSH / SFTP", "ssh"),
            ("google_drive", "Google Drive", "google-drive"),
            ("globus", "Globus", "globus"),
            ("github", "GitHub", "github"),
        ]:
            browser = identifier in {"google_drive", "globus", "github"}
            config = application(identifier) if browser else None
            configured = not config or bool(config.client_id and config.redirect_uri)
            definitions.append(
                {
                    "id": identifier,
                    **({"account_url": account_url()} if identifier == "github" else {}),
                    "name": label,
                    "logo": logo,
                    "authentication": "browser"
                    if browser
                    else "ssh_profile"
                    if identifier == "sftp"
                    else "none",
                    "configured": configured,
                    "authenticated": browser
                    and service.auth.account_connected(
                        service.principal, service.store.clio_id, identifier
                    ),
                    "capabilities": provider_capabilities(identifier).model_dump(),
                    "setup_requirement": None
                    if configured
                    else "Sign-in is not available in this build. No account setup is needed from you.",
                }
            )
        return {"clio_id": service.store.clio_id, "host_id": "local", "providers": definitions}

    @app.delete("/v1/storage/accounts/{provider}")
    async def sign_out_account(provider: str) -> dict[str, bool]:
        """Sign this user out of a provider on this CLIO, across workspaces."""
        if provider not in {"google_drive", "globus", "github"}:
            raise HTTPException(404, "Connected account not found")

        def sign_out() -> None:
            for record in service.store.list("source", SourceRecord):
                if record.principal == service.principal and record.source.provider == provider:
                    service.require_idle(record)
            service.auth.sign_out(service.principal, service.store.clio_id, provider)
            if provider == "github":
                service.github.clear()

        await change_access(sign_out)
        return {"signed_out": True}

    @app.post("/v1/storage/accounts/{provider}/auth/start")
    async def start_account_auth(
        provider: str, request: Request, body: StartSignIn | None = None
    ) -> dict[str, Any]:
        """Start a reusable login independently of connecting a folder."""
        return await run(
            lambda: start_browser_auth(
                request,
                service.auth.account_login(service.principal, service.store.clio_id, provider),
                body,
            )
        )

    @app.post("/v1/storage/accounts/{provider}/auth/complete")
    async def complete_account_auth(provider: str, body: CompleteSignIn) -> dict[str, bool]:
        """Finish provider sign-in without creating a source or workspace attachment."""
        completed = await change_access(
            lambda: service.auth.complete(
                service.auth.account_login(service.principal, service.store.clio_id, provider),
                body.flow_id,
                body.callback_url,
            )
        )
        return {"authenticated": completed}

    @app.get("/v1/storage/globus-destination")
    async def globus_destination() -> dict[str, object]:
        """Describe receiving storage on this connected CLIO host, shared by all sources."""
        return await run(lambda: destination_status(service.store))

    @app.put("/v1/storage/globus-destination")
    async def save_globus_destination(body: GlobusDestination) -> dict[str, object]:
        """Save one host mapping; existing native jobs keep their recorded destination."""

        def save() -> dict[str, object]:
            body.validate_storage(service.store.root)
            service.store.put("host-config", "globus-destination", body)
            return destination_status(service.store)

        return await change_access(save)

    @app.get("/v1/workspaces/{wid}/sources")
    async def sources(wid: str) -> dict[str, Any]:
        """List connected sources and retained disconnected copies."""
        workspace_root(wid)
        if getattr(app.state, "resource_store", None) is not None:
            await run(drafts().cleanup)
        return {"sources": [visible(row) for row in await run(lambda: service.sources(wid))]}

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/draft")
    async def begin_draft(wid: str, source_id: str) -> dict[str, str]:
        """Capture the data already kept before adding an unsent attachment."""
        return {
            "id": await change_access(
                lambda: drafts().begin(record_for(wid, source_id, connected=False))
            )
        }

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/draft/{draft_id}/{action}")
    async def finish_draft(wid: str, source_id: str, draft_id: str, action: str) -> dict[str, bool]:
        """Keep a sent attachment or withdraw its temporary workspace data."""
        record_for(wid, source_id, connected=False)
        if action not in {"keep", "discard"}:
            raise HTTPException(404, "Unknown attachment action")
        await change_access(lambda: drafts().finish(source_id, draft_id, keep=action == "keep"))
        return {"finished": True}

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

    @app.patch("/v1/workspaces/{wid}/sources/{source_id}")
    async def edit_source(wid: str, source_id: str, body: CreateSource) -> dict[str, Any]:
        """Edit the existing source without accumulating duplicate setup records."""

        def edit() -> SourceRecord:
            record = record_for(wid, source_id, connected=False)
            was_writable = record.source.mode == "write_enabled"
            record = service.update(record, body, workspace_root(wid))
            if record.source.mode == "write_enabled" and not was_writable:
                try:
                    connect_write_grant(wid, record)
                except Exception:
                    record.connected = False
                    service.store.put("source", source_id, record)
                    raise
            return record

        return visible(await change_access(edit))

    @app.delete("/v1/workspaces/{wid}/sources/{source_id}")
    async def remove_source(wid: str, source_id: str) -> dict[str, bool]:
        """Remove the saved connection; never delete upstream files or retained evidence."""

        def remove() -> None:
            record = record_for(wid, source_id, connected=False)
            service.require_idle(record)
            if record.source.mode == "write_enabled":
                disconnect_write_grant(wid, record)
            service.remove(record)

        await change_access(remove)
        return {"removed": True}

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/browse")
    async def browse_source(wid: str, source_id: str, body: BrowseSource) -> dict[str, Any]:
        """Return bounded entries with stable source-relative paths."""
        record = record_for(wid, source_id, connected=not body.materialized)

        def browse() -> dict[str, Any]:
            if (
                not body.materialized
                and record.source.provider == "github"
                and record.sign_in_required
                and not service.auth.connected(record)
            ):
                raise PermissionError("Sign in to GitHub to access this repository")
            if body.materialized:
                if not record.manifest_id or not record.source.local_path:
                    raise ValueError("Transfer this source before browsing its approved inputs")
                rows = service.store.get("manifest", record.manifest_id, Manifest).entries
            elif record.linked_manifest_id:
                rows = service.store.get("manifest", record.linked_manifest_id, Manifest).entries
            elif record.source.provider in {"github", "google_drive"} and record.connected:
                from clio_agent.gact.storage.linked import linked_adapter

                with linked_adapter(service, record) as adapter:
                    rows = adapter.entries(
                        folder="" if body.query else body.folder, recursive=bool(body.query)
                    )
            else:
                if not record.connected:
                    raise ValueError("Reconnect this live source before browsing it")
                with service.adapter(record) as adapter:
                    rows = (
                        adapter.entries(
                            folder="" if body.query else body.folder,
                            recursive=bool(body.query),
                        )
                        if isinstance(adapter, GlobusSource)
                        else adapter.entries(metadata_only=True)
                        if isinstance(adapter, SftpSource)
                        else adapter.entries()
                    )
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

        def publish() -> dict[str, Any]:
            lease = ""

            def prepare(record: SourceRecord) -> None:
                nonlocal lease
                lease = drafts().begin(record)
                drafts().uploaded_resources(
                    record.source.id, lease, [item.resource_id for item in body.files]
                )

            record = publish_uploaded_source(
                service.store,
                app.state.resource_store,
                wid,
                root,
                service.principal,
                body,
                on_prepare=prepare if body.draft else None,
            )
            if body.draft and not lease:
                lease = drafts().begin(record)
            return {**visible(record), "draft_id": lease}

        return await change_access(publish)

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/auth/sftp")
    async def sftp_sign_in(wid: str, source_id: str, body: SftpCredentials) -> dict[str, Any]:
        """Refresh only this source's private, in-memory SFTP credentials."""

        def sign_in() -> SourceRecord:
            record = record_for(wid, source_id, connected=False)
            if record.source.provider != "sftp" or record.configuration.ssh_origin != "clio":
                raise ValueError("This source does not use browser SFTP sign-in")
            service.require_idle(record)
            record.configuration.ssh_authentication = (
                "password"
                if body.password is not None
                else "key"
                if body.private_key
                else "configured"
            )
            service.auth.save_sftp_credentials(record, body)
            try:
                with service.adapter(record):
                    pass
            except Exception:
                service.auth.disconnect(source_id)
                raise
            record.connected = True
            service.store.put("source", source_id, record)
            return record

        return visible(await change_access(sign_in))

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/auth/start")
    async def start_auth(
        wid: str, source_id: str, request: Request, body: StartSignIn | None = None
    ) -> dict[str, Any]:
        """Open provider sign-in only from the trusted user setup surface."""
        return await run(lambda: start_browser_auth(request, record_for(wid, source_id), body))

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/reference")
    async def reference(
        wid: str, source_id: str, body: AttachSourceFile, background_tasks: BackgroundTasks
    ) -> dict[str, Any]:
        """Attach approved bytes with source identity through ordinary resource custody."""
        from clio_agent.gact.resource_lifecycle import emit_workspace_event, schedule_processing
        from clio_agent.gact.resource_materialization import materialize_once
        from clio_agent.gact.storage.references import source_folder_resource, source_resource

        record = record_for(wid, source_id, connected=False)

        def prepare_resource() -> Any:
            if body.folder:
                return source_folder_resource(
                    service.store,
                    app.state.resource_store,
                    record,
                    linked=body.linked,
                    folder=body.path,
                )
            if not body.path:
                raise ValueError("Select a file to attach")
            selected = None
            if body.linked:
                from clio_agent.gact.storage.linked import linked_file

                selected = linked_file(service, record, body.path)
            return source_resource(
                service.store, app.state.resource_store, record, body.path, linked_path=selected
            )

        def prepare() -> Any:
            return materialize_once(app, prepare_resource())

        resource = await run(
            lambda: drafts().prepare(source_id, body.draft_id, prepare)
            if body.draft_id
            else prepare()
        )
        payload = resource.to_wire()
        emit_workspace_event(app, wid, "resource.ready", payload)
        if not body.draft_id:
            schedule_processing(app, resource, background_tasks)
        return payload

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/auth/complete")
    async def complete_auth(wid: str, source_id: str, body: CompleteSignIn) -> dict[str, Any]:
        """Consume authorization without returning tokens or including them in source metadata."""
        completed = await change_access(
            lambda: service.auth.complete(
                record_for(wid, source_id), body.flow_id, body.callback_url
            )
        )
        return {"authenticated": completed, "source_id": source_id}

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/transfer", status_code=202)
    async def transfer(
        wid: str, source_id: str, body: DownloadSelection | None = None
    ) -> dict[str, Any]:
        """Start an explicit refresh, retaining previous inputs until a complete copy is ready."""
        nonlocal controller_loop
        controller_loop = asyncio.get_running_loop()
        async with source_changes:
            record = record_for(wid, source_id)
            try:
                validate_task_owner(app, wid, body.session_id if body else "")
                if getattr(app.state, "resource_store", None) is not None:
                    drafts().change(source_id, body.draft_id if body else "")
                paths = body.paths if body else None
                if paths:
                    from clio_agent.gact.storage.models import FileEntry

                    for path in paths:
                        FileEntry(path=path, kind="file")
                service.require_idle(record)
                if body and body.access:
                    if (
                        record.manifest_id
                        and (body.access == "read_only") != record.download_read_only
                    ):
                        raise ValueError("Remove the downloaded copy before changing its access")
                    record.download_access = body.access
                    service.store.put("source", source_id, record)
                operation = service.start_transfer(record, workspace_root(wid), paths)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        if body and body.session_id:
            from clio_agent.gact.storage.task_adapter import storage_handle

            storage_handle(app, body.session_id, operation, f"Download {record.source.label}")
        return operation.model_dump()

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/link", status_code=202)
    async def link(wid: str, source_id: str, body: DraftSelection | None = None) -> dict[str, Any]:
        """Link or explicitly refresh folder metadata through its fsspec backend."""
        from clio_agent.gact.storage.indexing import start_index

        def attach() -> SourceRecord:
            record = record_for(wid, source_id)
            if getattr(app.state, "resource_store", None) is not None:
                drafts().change(source_id, body.draft_id if body else "")
            if body and body.access:
                if pending_edits(service, record).changes:
                    raise ValueError("Publish or discard local edits before changing this link")
                if body.access != "read_only":
                    options = mapping_options(service, record)
                    if body.access not in options["link_access"]:
                        raise PermissionError(options["reason"])
                    if body.access == "write_through" and not body.confirm_remote:
                        raise ValueError("Confirm that saving edits will update the originals")
                record.link_access = body.access
                service.store.put("source", source_id, record)
            validate_task_owner(app, wid, body.session_id if body else "")
            return record

        record = await change_access(attach)
        operation = start_index(service, record)
        if body and body.session_id:
            from clio_agent.gact.storage.task_adapter import storage_handle

            storage_handle(app, body.session_id, operation, f"Index {record.source.label}")
        return {**visible(record), "indexing_operation": operation.model_dump()}

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/unlink")
    async def unlink(wid: str, source_id: str) -> dict[str, Any]:
        """Remove the virtual folder; retain credentials, downloads, and attached evidence."""

        def detach() -> SourceRecord:
            record = record_for(wid, source_id, connected=False)
            service.require_idle(record)
            if getattr(app.state, "resource_store", None) is not None:
                drafts().change(source_id)
            if pending_edits(service, record).changes:
                raise ValueError("Publish or discard local edits before unlinking this folder")
            record.linked_manifest_id = None
            service.store.put("source", source_id, record)
            return record

        return visible(await change_access(detach))

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
        if operation.task_handle and operation.owner_session_id:
            from clio_agent.gact.task_controls import cancel_selected

            outcome = await asyncio.to_thread(
                cancel_selected, app, operation.owner_session_id, operation.task_handle
            )
            if outcome["errors"]:
                raise HTTPException(409, outcome["errors"])
            return service.store.get("operation", operation_id, TransferOperation).model_dump(
                exclude={"native_request"}
            )
        return (
            await run(lambda: service.store.update_operation(operation_id, cancel_requested=True))
        ).model_dump(exclude={"native_request"})

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/review")
    async def review(wid: str, source_id: str) -> dict[str, Any]:
        """Inspect local changes and upstream conflicts without applying anything."""
        record = record_for(wid, source_id)

        def inspect() -> dict[str, Any]:
            return review_edits(service, record).model_dump()

        return await run(inspect)

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/apply")
    async def apply(wid: str, source_id: str, body: ApplyChanges) -> dict[str, str]:
        """Apply exactly the files the user selected from a current review."""
        operation = await change_access(
            lambda: publish_edits(service, record_for(wid, source_id), body.review_id, body.paths)
        )
        return {"operation_id": operation}

    @app.get("/v1/workspaces/{wid}/sources/{source_id}/mapping-options")
    async def mapping(wid: str, source_id: str) -> dict[str, Any]:
        """Inspect access to this resource without changing it."""
        return await run(lambda: mapping_options(service, record_for(wid, source_id)))

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/discard-edits")
    async def discard(wid: str, source_id: str) -> dict[str, Any]:
        """Discard local linked edits only after an explicit user action."""
        return visible(
            await change_access(lambda: discard_edits(service, record_for(wid, source_id)))
        )

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/disconnect")
    async def disconnect(wid: str, source_id: str) -> dict[str, Any]:
        """Disconnect source access, retaining saved accounts, copied inputs and baselines."""
        record = record_for(wid, source_id, connected=False)

        def detach() -> SourceRecord:
            if record.source.mode == "write_enabled":
                disconnect_write_grant(wid, record)
            return service.disconnect(record)

        return visible(await change_access(detach))

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/reconnect")
    async def reconnect(wid: str, source_id: str) -> dict[str, Any]:
        """Reconnect the same source using its reusable account when one is saved."""
        record = record_for(wid, source_id, connected=False)

        def attach() -> SourceRecord:
            if record.source.mode == "write_enabled":
                connect_write_grant(wid, record)
            record.connected = True
            service.auth.reconnect(source_id)
            service.store.put("source", source_id, record)
            return record

        return visible(await change_access(attach))

    @app.post("/v1/workspaces/{wid}/sources/{source_id}/remove-copy")
    async def remove_copy(wid: str, source_id: str) -> dict[str, Any]:
        """Remove the owned working copy, never upstream data or baseline evidence."""
        record = record_for(wid, source_id, connected=False)
        return visible(await run(lambda: service.remove_copy(record, workspace_root(wid))))
