"""Connected-CLIO source ownership and asynchronous transfer orchestration."""

from __future__ import annotations

import asyncio
import getpass
import logging
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from clio_schemas.connected_resources import ConnectedSource, ResourceOwner, SourceCapabilities

from clio_agent.gact.storage.adapters import LocalSource, SourceAdapter
from clio_agent.gact.storage.auth import StorageAuth
from clio_agent.gact.storage.drive import DriveSource, drive_folder_id
from clio_agent.gact.storage.github_access import GitHubAccess
from clio_agent.gact.storage.globus import GlobusSource
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
    if provider == "globus":
        return GlobusSource.capabilities.model_copy(update={"link_folder": True})
    if provider == "github":
        from clio_agent.gact.storage.github_source import GitHubSource

        return GitHubSource.capabilities
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
        # GACT currently authenticates one owner (loopback or the instance bearer).
        # Bind to that OS owner, not an ephemeral Desktop launch token or a
        # client-supplied username. Future multi-user auth supplies distinct subjects.
        self.principal = "os-owner:" + getpass.getuser()
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.transfer_settled: Callable[[], None] | None = None
        self.target_source: Callable[[SourceRecord], SshTargetSource | SftpSource] | None = None
        self.store.recover()

    def reconcile(self, workspaces: dict[str, Path]) -> None:
        """Resume observing native jobs after restart without duplicating submissions."""
        for operation in self.store.list("operation", TransferOperation):
            if operation.native_request and operation.state == "interrupted":
                record = self.store.get("source", operation.source_id, SourceRecord)
                workspace = workspaces.get(record.source.workspace_id or "")
                if record.connected and workspace is not None and workspace.is_dir():
                    self.start_transfer(record, workspace)

    async def shutdown(self) -> None:
        """Cooperatively stop local transfers and retain native jobs for later reconciliation."""
        tasks = list(self.tasks.items())
        for identifier, task in tasks:
            operation = self.store.get("operation", identifier, TransferOperation)
            if operation.native_request:
                task.cancel()
            else:
                self.store.update_operation(identifier, cancel_requested=True)
        if tasks:
            _, pending = await asyncio.wait([task for _, task in tasks], timeout=10)
            for task in pending:
                task.cancel()
            await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)

    def create(
        self, workspace_id: str, body: CreateSource, workspace_root: Path | None = None
    ) -> SourceRecord:
        """Register an explicitly selected source without materializing or writing data."""
        record = self._prepare_source(workspace_id, body, workspace_root)
        self.store.put("source", record.source.id, record)
        return record

    def _prepare_source(
        self, workspace_id: str, body: CreateSource, workspace_root: Path | None
    ) -> SourceRecord:
        if body.configuration.target_id and body.provider != "sftp":
            raise ValueError("An SSH target can only be used by an SSH source")
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
        if body.provider == "google_drive":
            root = drive_folder_id(root)
        if body.provider == "github":
            from clio_agent.gact.storage.linked import github_location

            github_location(root, body.configuration.github_ref)
        if body.provider == "local":
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
                    and other.provider == "local"
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
            operation.source_id == record.source.id
            and (
                operation.state in {"queued", "running"}
                or bool(operation.native_request)
                and operation.state == "interrupted"
            )
            for operation in self.store.list("operation", TransferOperation)
        ):
            raise ValueError("Cancel or finish the active transfer before changing this source")

    def update(
        self, record: SourceRecord, body: CreateSource, workspace_root: Path
    ) -> SourceRecord:
        """Edit setup in place and reevaluate access against the saved account's grants."""
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
        if source.provider == "local":
            adapter: SourceAdapter | GlobusSource = LocalSource(
                source.root, writable=source.mode == "write_enabled"
            )
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

    def start_transfer(
        self, record: SourceRecord, workspace_root: Path, selected_paths: list[str] | None = None
    ) -> TransferOperation:
        """Queue explicit materialization or resume the same native transfer identity."""
        if record.origin == "desktop_upload":
            raise ValueError("Choose the desktop folder again to explicitly upload a new revision")
        if not self.download_available(record):
            raise ValueError("Downloads require this connector's transfer backend; use Link folder")
        existing = next(
            (
                row
                for row in reversed(self.store.list("operation", TransferOperation))
                if row.source_id == record.source.id
                and row.native_request
                and row.state == "interrupted"
            ),
            None,
        )
        if not existing:
            self.require_idle(record)
        operation = existing or self.store.begin_operation(
            record.source.id, "refresh" if record.manifest_id else "materialize"
        )
        if not existing and selected_paths is not None:
            operation = self.store.update_operation(operation.id, selected_paths=selected_paths)
        if existing:
            if existing.id in self.tasks:
                raise ValueError("This source already has an active transfer")
            operation = self.store.update_operation(existing.id, state="queued", error=None)
        record.source = record.source.model_copy(
            update={"materialization": "transferring", "operation_id": operation.id}
        )
        self.store.put("source", record.source.id, record)
        self.tasks[operation.id] = asyncio.create_task(
            self._transfer(record, workspace_root, operation)
        )
        self.tasks[operation.id].add_done_callback(lambda _: self.tasks.pop(operation.id, None))
        return operation

    async def _transfer(
        self, record: SourceRecord, workspace_root: Path, operation: TransferOperation
    ) -> None:
        def step() -> bool:
            current = self.store.get("operation", operation.id, TransferOperation)
            with self.adapter(record) as adapter:
                if isinstance(adapter, GlobusSource):
                    result = adapter.poll(self.store, current, workspace_root)
                    return result.state in {"completed", "failed", "cancelled"}
                materialize(self.store, record, adapter, workspace_root, current)
                return True

        try:
            while not await asyncio.to_thread(step):
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            # The provider job may still be running. Keep its immutable request
            # and submission ID, and reconcile it after reconnection.
            self.store.update_operation(
                operation.id,
                state="interrupted",
                error="CLIO stopped; reconnect to resume checking this transfer",
            )
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            current = self.store.get("operation", operation.id, TransferOperation)
            if current.state not in {"failed", "cancelled"}:
                self.store.update_operation(
                    operation.id,
                    state="interrupted" if current.native_request else "failed",
                    error=str(exc),
                )
            record = self.store.get("source", record.source.id, SourceRecord)
            record.source = record.source.model_copy(
                update={"materialization": "stale" if record.manifest_id else "failed"}
            )
            self.store.put("source", record.source.id, record)

        except Exception:
            logger.exception(
                "Storage transfer failed source_id=%s operation_id=%s",
                record.source.id,
                operation.id,
            )
            self.store.update_operation(
                operation.id,
                state="interrupted",
                error="Storage transfer stopped unexpectedly; inspect and retry",
            )
            record = self.store.get("source", record.source.id, SourceRecord)
            record.source = record.source.model_copy(
                update={"materialization": "stale" if record.manifest_id else "failed"}
            )
            self.store.put("source", record.source.id, record)

        finally:
            if self.transfer_settled is not None:
                await asyncio.to_thread(self.transfer_settled)

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
