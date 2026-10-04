"""Connected-CLIO source ownership and asynchronous transfer orchestration."""

from __future__ import annotations

import asyncio
import getpass
import logging
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from clio_schemas.connected_resources import ConnectedSource, ResourceOwner, SourceCapabilities

from clio_agent.gact.storage.adapters import LocalSource, SourceAdapter
from clio_agent.gact.storage.auth import StorageAuth
from clio_agent.gact.storage.drive import DriveSource
from clio_agent.gact.storage.globus import GlobusSource
from clio_agent.gact.storage.models import CreateSource, SourceRecord, TransferOperation
from clio_agent.gact.storage.sftp import SftpSource
from clio_agent.gact.storage.store import SourceStore
from clio_agent.gact.storage.transfers import materialize, remove_owned_tree

logger = logging.getLogger(__name__)


def provider_capabilities(provider: str) -> SourceCapabilities:
    """Describe provider semantics before sign-in; node checks may narrow them."""
    if provider == "globus":
        return GlobusSource.capabilities
    return SourceCapabilities(
        search=True,
        revision_check=True,
        writable_folder=provider == "local",
        supported_modes=["read_only", "working_copy", "write_enabled"]
        if provider == "local"
        else ["read_only", "working_copy"],
        unavailable_reasons={}
        if provider == "local"
        else {
            "write_enabled": "This provider transfers files; it does not expose a writable operating-system folder on this CLIO."
        },
    )


class StorageService:
    """Own sources on one CLIO; every remote-connected UI uses this same backend owner."""

    def __init__(self, root: Path, credential_path: Path) -> None:
        self.store = SourceStore(root)
        self.auth = StorageAuth(credential_path)
        # GACT currently authenticates one owner (loopback or the instance bearer).
        # Bind to that OS owner, not an ephemeral Desktop launch token or a
        # client-supplied username. Future multi-user auth supplies distinct subjects.
        self.principal = "os-owner:" + getpass.getuser()
        self.tasks: dict[str, asyncio.Task[None]] = {}
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
        capabilities = provider_capabilities(body.provider)
        root = body.root
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
            owner=ResourceOwner(clio_id=self.store.clio_id, host_id="local"),
            root=root,
            mode=body.mode,
            capabilities=capabilities,
            workspace_id=workspace_id,
            local_path=root if body.mode == "write_enabled" else None,
            materialization="ready" if body.mode == "write_enabled" else "not_materialized",
        )
        record = SourceRecord(
            source=source,
            configuration=body.configuration,
            principal=self.principal,
            workspace_root=str(workspace_root.resolve()) if workspace_root else "",
        )
        self.store.put("source", source.id, record)
        return record

    def get(self, workspace_id: str, source_id: str, *, connected: bool = True) -> SourceRecord:
        """Reject references crossing workspace, principal or connected-CLIO boundaries."""
        record = self.store.get("source", source_id, SourceRecord)
        if (
            record.source.workspace_id != workspace_id
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
            if row.source.workspace_id == workspace_id and row.principal == self.principal
        ]

    @contextmanager
    def adapter(self, record: SourceRecord) -> Iterator[SourceAdapter | GlobusSource]:
        """Open a private provider connection; never return its credentials to a caller."""
        source = record.source
        if source.provider == "local":
            adapter: SourceAdapter | GlobusSource = LocalSource(
                source.root, writable=source.mode != "read_only"
            )
        elif source.provider == "sftp":
            adapter = SftpSource(
                record.configuration.ssh_profile,
                source.root,
                writable=source.mode == "working_copy",
            )
        elif source.provider == "google_drive":
            adapter = DriveSource(
                source.root, self.auth.token(record), writable=source.mode == "working_copy"
            )
        else:
            adapter = GlobusSource(record, self.auth.token(record))
        try:
            yield adapter
        finally:
            if isinstance(adapter, (DriveSource, SftpSource)):
                adapter.close()

    def start_transfer(self, record: SourceRecord, workspace_root: Path) -> TransferOperation:
        """Queue explicit materialization or resume the same native transfer identity."""
        if record.source.mode == "write_enabled":
            raise ValueError(
                "This source is already a live writable folder; it has no refresh transfer"
            )
        if record.origin == "desktop_upload":
            raise ValueError("Choose the desktop folder again to explicitly upload a new revision")
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
        operation = existing or self.store.begin_operation(
            record.source.id, "refresh" if record.manifest_id else "materialize"
        )
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

    def disconnect(self, record: SourceRecord) -> SourceRecord:
        """Disconnect credentials and upstream access while retaining all owned evidence."""
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
        if record.source.mode == "write_enabled":
            raise ValueError(
                "This is the original source folder; disconnect it instead of removing a copy"
            )
        if any(
            row.source_id == record.source.id and row.state in {"queued", "running"}
            for row in self.store.list("operation", TransferOperation)
        ):
            raise ValueError("Cancel or finish the transfer before removing its copy")
        if record.source.mode == "working_copy" and record.source.local_path:
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
