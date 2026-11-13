"""Connected-CLIO source ownership and asynchronous transfer orchestration."""

from __future__ import annotations

import asyncio
import getpass
import logging
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Literal

from clio_schemas.connected_resources import ConnectedSource, ResourceOwner, SourceCapabilities

from clio_agent.gact.storage import s3_keys
from clio_agent.gact.storage.adapters import LocalSource, SourceAdapter
from clio_agent.gact.storage.auth import StorageAuth
from clio_agent.gact.storage.drive import DriveSource, drive_folder_id
from clio_agent.gact.storage.github_access import GitHubAccess
from clio_agent.gact.storage.globus import GlobusSource, GlobusStatusUnavailable
from clio_agent.gact.storage.globus_consent import GlobusConsentRequired
from clio_agent.gact.storage.globus_destination import receiving_destination
from clio_agent.gact.storage.models import CreateSource, SourceRecord, TransferOperation
from clio_agent.gact.storage.sftp import SftpSource
from clio_agent.gact.storage.ssh_target import SshTargetSource
from clio_agent.gact.storage.store import SourceStore
from clio_agent.gact.storage.transfers import materialize, remove_owned_tree

logger = logging.getLogger(__name__)


def provider_capabilities(provider: str) -> SourceCapabilities:
    """Describe provider semantics before sign-in; node checks may narrow them."""
    if provider == "ndp":
        from clio_agent.gact.storage.ndp_source import NDPSource

        return NDPSource.capabilities
    if provider == "globus":
        return GlobusSource.capabilities.model_copy(update={"link_folder": True})
    if provider == "github":
        from clio_agent.gact.storage.github_source import GitHubSource

        return GitHubSource.capabilities
    if provider == "s3":
        from clio_agent.gact.storage.s3_source import S3Folder

        return S3Folder.capabilities
    if provider == "obsidian":
        return SourceCapabilities(
            link_folder=True,
            search=True,
            revision_check=True,
            conditional_write=True,
            writable_folder=True,
            note_read=True,
            note_search=True,
            note_relations=True,
            note_write=True,
            supported_modes=["read_only", "working_copy", "write_enabled"],
        )
    return SourceCapabilities(
        link_folder=True,
        search=True,
        revision_check=True,
        writable_folder=True,
        supported_modes=["read_only", "working_copy", "write_enabled"],
        unavailable_reasons={},
    )


class StorageService:
    """Own sources on one CLIO; every remote-connected UI uses this same backend owner."""

    def __init__(self, root: Path, credential_path: Path) -> None:
        self.store = SourceStore(root)
        self.auth = StorageAuth(credential_path)
        self.github = GitHubAccess()
        from clio_agent.gact.storage.ndp_catalog import NDPCatalog

        self.ndp = NDPCatalog()
        self.ndp_selection_lock = threading.RLock()
        from clio_agent.gact.storage.obsidian_account import ObsidianAccount
        from clio_agent.gact.storage.obsidian_sync import ObsidianSync

        self.obsidian_account = ObsidianAccount(
            credential_path.parent / "obsidian", root.parent / "knowledge-vaults"
        )
        self.obsidian_sync = ObsidianSync(self.obsidian_account)
        # GACT currently authenticates one owner (loopback or the instance bearer).
        # Bind to that OS owner, not an ephemeral Desktop launch token or a
        # client-supplied username. Future multi-user auth supplies distinct subjects.
        self.principal = "os-owner:" + getpass.getuser()
        from clio_agent.gact.storage.ndp_endpoints import NDPEndpointService

        self.ndp_endpoints = NDPEndpointService(self.store, self.principal)
        self.target_source: Callable[[SourceRecord], SshTargetSource | SftpSource] | None = None

    def set_link_access(
        self, record: SourceRecord, access: Literal["read_only", "publish_later", "write_through"]
    ) -> None:
        """Keep approved link access and advertised knowledge write capability in agreement."""
        if record.source.provider in {"ndp", "s3"} and access != "read_only":
            raise PermissionError("NDP and S3 sources are read-only")
        record.link_access = access
        if record.source.source_kind == "knowledge":
            record.source = record.source.model_copy(
                update={
                    "capabilities": record.source.capabilities.model_copy(
                        update={"note_write": access != "read_only"}
                    )
                }
            )

    async def shutdown(self) -> None:
        """Stop the independent note synchronizer; native task hosts own storage execution."""
        await asyncio.to_thread(self.obsidian_sync.shutdown)

    def create(
        self, workspace_id: str, body: CreateSource, workspace_root: Path | None = None
    ) -> SourceRecord:
        """Register a selected source, creating owned empty storage only when requested."""
        record = self._prepare_source(workspace_id, body, workspace_root)
        self.store.put("source", record.source.id, record)
        return record

    def _prepare_source(
        self, workspace_id: str, body: CreateSource, workspace_root: Path | None
    ) -> SourceRecord:
        if body.configuration.target_id and body.provider != "sftp":
            raise ValueError("An SSH target can only be used by an SSH source")
        if body.s3_credentials is not None and (
            body.provider != "s3" or body.configuration.s3_access != "keys"
        ):
            raise ValueError("Access keys apply only to an S3 source that uses keys")
        if body.sftp_credentials is not None and (
            body.provider != "sftp" or body.configuration.ssh_origin != "clio"
        ):
            raise ValueError("Passwords apply only to browser SFTP connections")
        if (
            body.configuration.ssh_authentication != "configured"
            and body.configuration.ssh_origin != "clio"
        ):
            raise ValueError("Desktop authentication is handled by OpenSSH")
        capabilities = provider_capabilities(body.provider)
        if (
            body.provider == "sftp"
            and body.configuration.target_id
            and body.configuration.ssh_origin != "clio"
        ):
            capabilities = SshTargetSource.capabilities
            if body.mode not in capabilities.supported_modes:
                raise ValueError("This SSH connection currently supports read-only transfers")
        root = body.root
        if body.provider == "ndp":
            from clio_agent.gact.storage.ndp_catalog import NDPSelection

            if not body.configuration.ndp_selection_id:
                raise ValueError("Choose NDP resources in the dataset picker")
            selection = self.store.get(
                "ndp-selection", body.configuration.ndp_selection_id, NDPSelection
            )
            if selection.dataset.id != root or not any(
                selection.dataset.endpoint.same_destination(endpoint)
                for endpoint in self.ndp.endpoints()
            ):
                raise ValueError("Choose resources from the configured NDP Endpoint")
        if body.create_vault:
            vault = self.store.root.parent / "knowledge-vaults" / uuid.uuid4().hex
            vault.mkdir(parents=True, exist_ok=False)
            (vault / ".obsidian").mkdir()
            (vault / ".obsidian" / "app.json").write_text("{}\n", encoding="utf-8")
            root = str(vault)
        if body.provider == "google_drive":
            root = drive_folder_id(root)
        if body.provider == "s3":
            from clio_agent.gact.storage.s3_source import S3Location

            if body.configuration.s3_access == "keys" and body.s3_credentials is None:
                raise ValueError("Enter the access key ID and secret access key")
            if body.mode != "read_only":
                raise ValueError("S3 sources are read-only")
            root = S3Location.parse(root).root
        if body.provider == "github":
            from clio_agent.gact.storage.linked import github_location

            github_location(root, body.configuration.github_ref)
        if body.provider in {"local", "obsidian"}:
            if body.provider == "obsidian":
                from clio_agent.gact.storage.obsidian import ObsidianSource

                adapter: LocalSource = ObsidianSource(
                    root,
                    writable=body.mode != "read_only",
                    excluded=body.configuration.obsidian_excluded_folders,
                )
            else:
                adapter = LocalSource(root, writable=body.mode != "read_only")
            capabilities, root = adapter.capabilities, str(adapter.root)
            protected = (self.store.root.resolve(), self.auth.private_root.resolve())
            if any(
                adapter.root.is_relative_to(path) or path.is_relative_to(adapter.root)
                for path in protected
            ):
                raise ValueError(
                    "Select a data folder outside CLIO's private source and sign-in storage"
                )
            if workspace_root and workspace_root.resolve().is_relative_to(adapter.root):
                raise ValueError("Select an input folder, not the workspace or its parent")
            for existing in self.store.list("source", SourceRecord):
                other = existing.source
                if (
                    existing.connected
                    and other.provider in {"local", "obsidian"}
                    and ((body.mode == "write_enabled") != (other.mode == "write_enabled"))
                ):
                    other_root = Path(other.root).resolve()
                    if adapter.root.is_relative_to(other_root) or other_root.is_relative_to(
                        adapter.root
                    ):
                        raise ValueError(
                            "This folder overlaps a connected source with a different write mode"
                        )
        source = ConnectedSource(
            id="source_" + uuid.uuid4().hex,
            provider=body.provider,
            source_kind="knowledge" if body.provider == "obsidian" else "files",
            connection=body.configuration.obsidian_connection
            if body.provider == "obsidian"
            else "provider",
            label=body.label,
            owner=ResourceOwner(
                clio_id=self.store.clio_id, host_id=body.configuration.target_id or "local"
            ),
            root=root,
            mode=body.mode,
            capabilities=capabilities,
            workspace_id=workspace_id,
            local_path=None,
            materialization="not_materialized",
        )
        record = SourceRecord(
            source=source,
            configuration=body.configuration,
            principal=self.principal,
            workspace_root=str(workspace_root.resolve()) if workspace_root else "",
        )
        if body.provider == "s3":
            if body.s3_credentials is not None:
                s3_keys.save(self.auth, record, body.s3_credentials)
            self._check_s3(record)
        if body.provider == "sftp" and body.configuration.target_id:
            if self.target_source is None:
                raise ValueError("SSH target connections are unavailable on this CLIO")
            if body.sftp_credentials is not None:
                self.auth.save_sftp_credentials(record, body.sftp_credentials)
            try:
                checked = self.target_source(record)
                if isinstance(checked, SftpSource):
                    checked.close()
            except Exception:
                self.auth.disconnect(record.source.id)
                raise
        return record

    def can_edit_location(self, record: SourceRecord) -> bool:
        """Keep an established source identity stable for captures, copies and references."""
        return not (
            record.origin == "desktop_upload"
            or record.linked_manifest_id
            or record.manifest_id
            or record.source.local_path
            or any(
                operation.source_id == record.source.id
                for operation in self.store.list("operation", TransferOperation)
            )
        )

    def require_idle(self, record: SourceRecord) -> None:
        """Refuse edits while an operation may still publish data or progress."""
        if any(
            operation.source_id == record.source.id and (operation.state in {"queued", "running"})
            for operation in self.store.list("operation", TransferOperation)
        ):
            raise ValueError("Cancel or finish the active transfer before changing this source")

    def update(
        self, record: SourceRecord, body: CreateSource, workspace_root: Path
    ) -> SourceRecord:
        """Edit setup in place and reevaluate access against the saved account's grants."""
        if body.create_vault:
            raise ValueError("Create a new source to create a new vault")
        self.require_idle(record)
        if body.provider != record.source.provider:
            raise ValueError("Connect a new source to use a different provider")
        root = drive_folder_id(body.root) if body.provider == "google_drive" else body.root
        changed = (
            root != record.source.root
            or body.mode != record.source.mode
            or body.configuration != record.configuration
        )
        if changed:
            if not self.can_edit_location(record):
                raise ValueError(
                    "This source already has data or transfer history. Connect a new source to change its folder or mode."
                )
            if not record.source.workspace_id:
                raise ValueError("This source does not belong to a workspace")
            candidate = self._prepare_source(record.source.workspace_id, body, workspace_root)
            credentials = (
                self.auth.sftp_credentials(candidate)
                if candidate.source.provider == "sftp"
                else None
            )
            self.auth.reset_source(candidate.source.id)
            self.auth.reset_source(record.source.id)
            record.source = candidate.source.model_copy(update={"id": record.source.id})
            record.configuration = candidate.configuration
            record.target_route = candidate.target_route
            record.sign_in_required = False
            record.connected = True
            if credentials is not None:
                self.auth.save_sftp_credentials(record, credentials)
        else:
            record.source = record.source.model_copy(update={"label": body.label})
        self.store.put("source", record.source.id, record)
        return record

    def remove(self, record: SourceRecord) -> None:
        """Forget a connection while retaining copies, manifests and original source data."""
        self.require_idle(record)
        self.obsidian_sync.stop(record.source.id)
        self.auth.disconnect(record.source.id)
        record.connected = False
        record.removed = True
        self.store.put("source", record.source.id, record)

    def get(self, workspace_id: str, source_id: str, *, connected: bool = True) -> SourceRecord:
        """Reject references crossing workspace, principal or connected-CLIO boundaries."""
        record = self.store.get("source", source_id, SourceRecord)
        if (
            record.removed
            or record.source.workspace_id != workspace_id
            or record.principal != self.principal
            or record.source.owner.clio_id != self.store.clio_id
        ):
            raise KeyError(source_id)
        if connected and not record.connected:
            raise ValueError(
                "This source is disconnected; reconnect it before accessing upstream data"
            )
        return record

    def sources(self, workspace_id: str) -> list[SourceRecord]:
        """List this owner's sources, including disconnected copies retained for inspection."""
        return [
            row
            for row in self.store.list("source", SourceRecord)
            if not row.removed
            and row.source.workspace_id == workspace_id
            and row.principal == self.principal
        ]

    @contextmanager
    def adapter(self, record: SourceRecord) -> Iterator[SourceAdapter | GlobusSource]:
        """Open a private provider connection; never return its credentials to a caller."""
        source = record.source
        if source.provider == "obsidian":
            from clio_agent.gact.storage.obsidian import ObsidianSource

            adapter: SourceAdapter | GlobusSource = ObsidianSource(
                source.root,
                writable=source.mode == "write_enabled",
                excluded=record.configuration.obsidian_excluded_folders,
            )
        elif source.provider == "local":
            adapter = LocalSource(source.root, writable=source.mode == "write_enabled")
        elif source.provider == "sftp":
            if record.configuration.target_id:
                if self.target_source is None:
                    raise ValueError("SSH target connections are unavailable on this CLIO")
                adapter = self.target_source(record)
            else:
                adapter = SftpSource(
                    record.configuration.ssh_profile,
                    source.root,
                    writable=source.mode == "write_enabled",
                )
        elif source.provider == "google_drive":
            adapter = DriveSource(
                source.root, self.auth.token(record), writable=source.mode == "write_enabled"
            )
        elif source.provider == "ndp":
            from clio_agent.gact.storage.ndp_catalog import NDPSelection
            from clio_agent.gact.storage.ndp_source import NDPSource

            selected = self.store.get(
                "ndp-selection", record.configuration.ndp_selection_id, NDPSelection
            )
            if not any(
                selected.dataset.endpoint.same_destination(endpoint)
                for endpoint in self.ndp.endpoints()
            ):
                raise PermissionError("This NDP Endpoint changed; attach the dataset again")
            if selected.dataset.id != source.root:
                raise PermissionError("This NDP selection belongs to another dataset")
            if selected.dataset.endpoint.authenticated:
                self.auth.token(record)
            adapter = NDPSource(selected.dataset, lambda: self.auth.token(record))
        elif source.provider == "s3":
            from clio_agent.gact.storage.s3_source import S3Folder, S3Location, s3_client

            adapter = S3Folder(
                s3_client(record.configuration, s3_keys.keys(self.auth, record)),
                S3Location.parse(source.root),
            )
        elif source.provider == "globus":
            adapter = GlobusSource(record, self.auth.token(record))
        elif source.provider == "github":
            from clio_agent.gact.storage.github_source import GitHubSignInRequired, GitHubSource
            from clio_agent.gact.storage.linked import linked_adapter

            with linked_adapter(self, record, refresh=True) as folder:
                try:
                    yield GitHubSource(
                        folder.fs, folder.root, writable=source.mode == "write_enabled"
                    )
                except GitHubSignInRequired:
                    self.auth.reject_token(record, folder.fs.token)
                    self.github.clear()
                    raise
            return
        else:
            raise ValueError("This provider supports linked folders only")
        try:
            yield adapter
        except GlobusConsentRequired as exc:
            self.auth.require_globus_consent(record, exc.scopes)
            raise
        finally:
            if isinstance(adapter, (DriveSource, SftpSource)):
                adapter.close()

    def validate_download(self, record: SourceRecord) -> None:
        """Reject unsupported or conflicting downloads before native submission."""
        if record.origin == "desktop_upload":
            raise ValueError("Choose the desktop folder again to explicitly upload a new revision")
        if not self.download_available(record):
            raise ValueError("Downloads require this connector's transfer backend; use Link folder")
        self.require_idle(record)

    async def execute_transfer(
        self,
        record: SourceRecord,
        workspace_root: Path,
        operation: TransferOperation,
        *,
        progress: Callable[[int, int], None] | None = None,
        existing_job_only: bool = False,
        cancellation_requested: Callable[[], bool] | None = None,
        shutdown_requested: Callable[[], bool] | None = None,
    ) -> bool:
        """Join each provider step; return False only for local shutdown retirement."""

        def step() -> bool:
            current = self.store.get("operation", operation.id, TransferOperation)
            if cancellation_requested is not None and cancellation_requested():
                current = self.store.update_operation(operation.id, cancel_requested=True)
            with self.adapter(record) as adapter:
                if isinstance(adapter, GlobusSource):
                    result = (
                        adapter.poll_existing_job(self.store, current, workspace_root)
                        if existing_job_only
                        else adapter.poll(self.store, current, workspace_root)
                    )
                    if progress is not None:
                        progress(result.bytes_done, result.bytes_total)
                    return result.state in {"completed", "failed", "cancelled"}
                if existing_job_only:
                    raise ValueError("Existing-job continuation is unavailable for this provider")
                materialize(self.store, record, adapter, workspace_root, current, progress=progress)
                return True

        def retire() -> bool:
            if (
                record.source.provider != "globus"
                or shutdown_requested is None
                or not shutdown_requested()
            ):
                return False
            current = self.store.get("operation", operation.id, TransferOperation)
            if (
                current.cancel_requested
                or cancellation_requested is not None
                and cancellation_requested()
            ):
                return False
            self.store.update_operation(
                operation.id,
                state="interrupted",
                error=current.error
                or "Local observation retired for shutdown; provider settlement remains unconfirmed.",
            )
            return True

        def retiring() -> bool:
            return (
                record.source.provider == "globus"
                and shutdown_requested is not None
                and shutdown_requested()
            )

        interrupted = False
        while True:
            if retiring() and await asyncio.to_thread(retire):
                return False
            worker = asyncio.create_task(asyncio.to_thread(step))
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    interrupted = interrupted or not retiring()
                    # The native cancellation request must reach the provider,
                    # including cloud transfers, before its worker can settle.
                    if not retiring():
                        await asyncio.to_thread(
                            self.store.update_operation, operation.id, cancel_requested=True
                        )
                except (OSError, ValueError, RuntimeError):
                    break  # The joined worker's error and cleanup state are inspected below.
            try:
                settled = worker.result()
            except GlobusStatusUnavailable as exc:
                self.store.update_operation(operation.id, error=str(exc))
                settled = False
            except (OSError, ValueError, RuntimeError) as exc:
                current = self.store.get("operation", operation.id, TransferOperation)
                if current.state not in {"completed", "failed", "cancelled"}:
                    self.store.update_operation(operation.id, state="interrupted", error=str(exc))
                    if retiring() and await asyncio.to_thread(retire):
                        return False
                    raise
                if interrupted:
                    raise asyncio.CancelledError from exc
                return True
            if settled:
                if interrupted:
                    raise asyncio.CancelledError
                return True
            if retiring() and await asyncio.to_thread(retire):
                return False
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                interrupted = interrupted or not retiring()
                if not retiring():
                    await asyncio.to_thread(
                        self.store.update_operation, operation.id, cancel_requested=True
                    )

    def _check_s3(self, record: SourceRecord) -> None:
        """Ask the bucket for one key under the prefix, so a wrong name or key fails now."""
        from clio_agent.gact.storage.s3_source import S3Location, s3_client

        location = S3Location.parse(record.source.root)
        client = s3_client(record.configuration, s3_keys.keys(self.auth, record))
        try:
            client.list_objects_v2(Bucket=location.bucket, Prefix=location.prefix + "/", MaxKeys=1)
        except Exception as exc:
            from clio_agent.gact.storage.s3_source import _denied

            self.auth.disconnect(record.source.id)
            raise ValueError(f"The bucket could not be listed: {_denied(exc)}") from None

    def download_available(self, record: SourceRecord) -> bool:
        """Offer copies only when the selected connector's transfer backend is ready."""
        if record.origin == "desktop_upload":
            return False
        if record.source.provider == "github":
            return False
        if record.source.provider == "google_drive":
            return self.auth.connected(record)
        if record.source.provider == "globus":
            return bool(
                record.configuration.destination_collection_id
                or receiving_destination(self.store)[0]
            )
        return True

    def disconnect(self, record: SourceRecord) -> SourceRecord:
        """Disconnect source access while retaining reusable accounts and owned evidence."""
        for operation in self.store.list("operation", TransferOperation):
            if operation.source_id == record.source.id and operation.state in {"queued", "running"}:
                raise ValueError(
                    "Cancel or finish the active transfer before disconnecting this source"
                )
        self.obsidian_sync.stop(record.source.id)
        self.auth.disconnect(record.source.id)
        record.connected = False
        self.store.put("source", record.source.id, record)
        return record

    def remove_copy(self, record: SourceRecord, workspace_root: Path) -> SourceRecord:
        """Remove only CLIO's recorded copy, never the original or retained baselines."""
        if record.source.local_path and record.source.local_path == record.source.root:
            raise ValueError("Disconnect the original folder instead of removing it")
        if any(
            row.source_id == record.source.id and row.state in {"queued", "running"}
            for row in self.store.list("operation", TransferOperation)
        ):
            raise ValueError("Cancel or finish the transfer before removing its copy")
        if not record.download_read_only and record.source.local_path:
            expected = workspace_root / "connected-data" / record.source.id
            if Path(record.source.local_path).resolve() != expected.resolve():
                raise ValueError("The working-copy path no longer matches its ownership record")
            remove_owned_tree(expected, workspace_root / "connected-data")
        # Read-only materializations are immutable baseline evidence and are
        # retained; removing the usable reference does not delete that evidence.
        record.source = record.source.model_copy(
            update={"local_path": None, "materialization": "not_materialized"}
        )
        self.store.put("source", record.source.id, record)
        return record
