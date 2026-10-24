"""Native storage workers reusing approved providers, manifests and transfer algorithms."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastmcp import Context
from fastmcp.utilities.tasks import TaskConfig

from clio_agent.arc.task_types import (
    ProviderContinuation,
    SubmissionContext,
    TaskProgress,
    TaskSnapshot,
)
from clio_agent.errors import ClioError
from clio_agent.gact.native_work import NativeWork, WorkEvidence, now
from clio_agent.gact.off_loop import join_cleanup, join_off_loop
from clio_agent.gact.storage.connect_tool import prepare_connect
from clio_agent.gact.storage.globus import GlobusSource
from clio_agent.gact.storage.identity import source_submission_identity
from clio_agent.gact.storage.indexing import run_index
from clio_agent.gact.storage.models import (
    FileEntry,
    Manifest,
    SourceConfiguration,
    SourceRecord,
    TransferOperation,
)
from clio_agent.gact.storage.ndp_selection import NDPConnectionSelection
from clio_agent.gact.task_history import RecordingUnconfirmed
from clio_agent.tools.native_roots import NativeRoot

if TYPE_CHECKING:
    from clio_agent.gact.native_tools import NativeTools


logger = logging.getLogger(__name__)


def register_native_storage(
    root: NativeRoot, work: NativeWork, app: Any, *, native: NativeTools
) -> None:
    """Install Download and Indexing on the independent storage queue."""
    service = app.state.connected_storage

    def runtime_lost(task: TaskSnapshot) -> None:
        """Retire publication custody after the native owner proves local execution lost."""
        expected = {"Download": "materialize", "Indexing": "indexing"}.get(task.kind)
        if expected is None:
            return
        try:
            operation = service.store.get("operation", task.handle, TransferOperation)
        except KeyError:
            return  # The queued body never acquired source publication custody.
        if operation.kind != expected:
            raise ValueError("Retained source operation does not match the native task")
        if operation.native_request is not None or operation.native_job_id is not None:
            raise ValueError("Provider custody must settle through its original recovery owner")
        if operation.state in {"queued", "running"}:
            service.store.update_operation(
                operation.id,
                state="interrupted",
                error="The local runtime is gone; inspect uncertain effects before retrying.",
            )

    work.runtime_loss_observer = runtime_lost

    def owner(context: SubmissionContext) -> tuple[Any, Path]:
        session = app.state.sessions.get(context.owner.session_id)
        if session is None:
            raise ValueError("Storage session is unavailable")
        workspace = app.state.workspaces.get(session.workspace_id)
        if (
            workspace is None
            or Path(workspace.root_path).resolve() != Path(context.workspace).resolve()
        ):
            raise ValueError("Storage workspace changed before execution")
        return session, Path(workspace.root_path)

    def validate_source(
        record: SourceRecord, context: SubmissionContext, operation: TransferOperation | None = None
    ) -> None:
        if (
            not record.connected
            or record.principal != service.principal
            or record.source.owner.clio_id != service.store.clio_id
        ):
            raise PermissionError("Source access or ownership changed before execution")
        expected = context.attribution.get("source_identity")
        actual = source_submission_identity(record)
        if (
            expected is not None
            and expected != actual
            and (operation is None or operation.provider_identity != actual)
        ):
            raise PermissionError("Approved source configuration changed before execution")

    def progress(context: SubmissionContext, done: int, total: int | None, unit: str) -> None:
        with work.history.serialized():
            current = work.history.task_snapshot(context.handle)
            if current is None or current.terminal_at is not None:
                return
            work.history.record_snapshot(
                current.model_copy(
                    update={
                        "updated_at": now(),
                        "progress": TaskProgress(completed=done, total=total, unit=unit),
                    }
                ),
                observation_id=uuid4().hex,
            )

    def associate(
        context: SubmissionContext, record: SourceRecord, operation: TransferOperation
    ) -> None:
        with work.history.serialized():
            current = work.history.task_snapshot(context.handle)
            if current is None:
                raise ValueError("Storage task is unavailable")
            details = work.history.write_content(
                context.owner.session_id,
                {
                    "source_id": record.source.id,
                    "operation_id": operation.id,
                    "provider": record.source.provider,
                    "selected_paths": operation.selected_paths,
                    "revision": record.configuration.github_ref,
                },
            )
            work.history.record_snapshot(
                current.model_copy(
                    update={
                        "details": details,
                        "updated_at": now(),
                        "resource_id": record.source.id,
                    }
                ),
                observation_id=uuid4().hex,
            )

    def result(record: SourceRecord, operation: TransferOperation) -> dict[str, Any]:
        manifest_id = operation.manifest_id
        manifest = service.store.get("manifest", manifest_id, Manifest) if manifest_id else None
        return {
            "source_id": record.source.id,
            "provider": record.source.provider,
            "operation_id": operation.id,
            "state": operation.state,
            "bytes_done": operation.bytes_done,
            "bytes_total": operation.bytes_total,
            "manifest_id": manifest_id,
            "open_reference": {
                "source_id": record.source.id,
                "revision": manifest_id,
                "linked": operation.kind == "indexing",
            }
            if manifest_id and operation.state == "completed"
            else None,
            "revision": manifest.revision if manifest is not None else None,
            "entries": len(manifest.entries) if manifest is not None else 0,
            "hashes": manifest.hashes if manifest is not None else {},
            "error": operation.error,
            "native_job_id": operation.native_job_id,
        }

    def cancellation_needed(context: SubmissionContext) -> bool:
        with work.history.serialized():
            current = work.history.task_snapshot(context.handle)
            if current is None:
                raise ValueError("Provider continuation owner is unavailable")
            try:
                work._check_admission(current)
            except ValueError:
                return True  # the host-only continuation can then only cancel/clean up
        return bool(
            context.provider_continuation and context.provider_continuation.cancellation_only
        )

    @root.tool(name="connected_data_continue", task=TaskConfig(mode="required"))
    async def continue_job(
        ctx: Context, operation_id: str, _clio_submission: str
    ) -> dict[str, Any]:
        """Host-only continuation of a verified existing provider job; never submit provider work."""

        async def body(context: SubmissionContext, evidence: WorkEvidence) -> dict[str, Any]:
            continuation = context.provider_continuation
            if (
                continuation is None
                or continuation.operation_id != operation_id
                or operation_id != context.handle
            ):
                raise ValueError("Provider continuation custody is missing")

            def prepare() -> tuple[SourceRecord, TransferOperation, Path]:
                session, workspace = owner(context)
                original = SubmissionContext.model_validate(
                    work.history.read_content(continuation.original_submission)
                )
                operation = service.store.get("operation", operation_id, TransferOperation)
                record = service.get(session.workspace_id, continuation.source_id)
                if (
                    original.owner != context.owner
                    or original.tool != "connected_data_download"
                    or original.arguments.get("source_id") != record.source.id
                    or original.arguments.get("paths") != operation.selected_paths
                    or operation.source_id != record.source.id
                    or record.source.provider != "globus"
                    or operation.native_job_id != continuation.provider_job_id
                ):
                    raise ValueError(
                        "Provider continuation differs from the original job/paths/owner"
                    )
                validate_source(record, original, operation)
                if operation.publication_started_at is not None and not cancellation_needed(
                    context
                ):
                    raise ValueError(
                        "Earlier local publication is uncertain; inspect it before continuing"
                    )
                if cancellation_needed(context):
                    operation = service.store.update_operation(operation.id, cancel_requested=True)
                return record, operation, workspace

            record, operation, workspace = await join_off_loop(prepare)
            previous_publication_absent = operation.publication_started_at is None
            retired = False
            evidence.acquiring_effects()
            try:
                retired = not await service.execute_transfer(
                    record,
                    workspace,
                    operation,
                    existing_job_only=True,
                    cancellation_requested=lambda: cancellation_needed(context),
                    shutdown_requested=lambda: native._closing,
                    progress=lambda done, total: progress(context, done, total, "bytes"),
                )
            finally:

                async def finalize() -> TransferOperation:
                    settled = await join_off_loop(
                        service.store.get, "operation", operation.id, TransferOperation
                    )
                    if (
                        settled.state in {"completed", "failed", "cancelled"}
                        and settled.provider_cleanup_confirmed
                    ):
                        drafts = getattr(app.state, "source_drafts", None)
                        if drafts is not None:
                            await join_off_loop(drafts.cleanup)
                        evidence.confirm_cleanup()
                        if previous_publication_absent:

                            def confirm_previous() -> None:
                                with work.history.serialized():
                                    for attempt in work.history.native_attempts(context.handle):
                                        if (
                                            attempt.incarnation
                                            not in continuation.previous_incarnations
                                            or attempt.cleanup_state
                                            in {"confirmed", "never_started"}
                                        ):
                                            continue
                                        receipt = work.history.write_content(
                                            context.owner.session_id,
                                            {
                                                "incarnation": attempt.incarnation,
                                                "execution_key": attempt.execution_key,
                                                "provider_job_id": continuation.provider_job_id,
                                                "continuation": context.reference,
                                                "previous_local_execution_retired": True,
                                                "previous_publication_absent": True,
                                                "provider_and_staging_settled": True,
                                            },
                                        )
                                        work.history.record_attempt(
                                            attempt.model_copy(
                                                update={
                                                    "cleanup_state": "confirmed",
                                                    "cleanup_receipt": receipt,
                                                    "finished_at": now(),
                                                }
                                            )
                                        )

                            async def retain_previous_evidence() -> None:
                                await join_off_loop(confirm_previous)

                            try:
                                await retain_previous_evidence()
                            except (ClioError, OSError, TimeoutError, RecordingUnconfirmed):
                                work.recovery.defer(
                                    "provider-predecessor-cleanup:" + context.reference,
                                    retain_previous_evidence,
                                )
                    if retired or not settled.provider_cleanup_confirmed:
                        evidence.retire_provider_observer(
                            {
                                "operation_id": settled.id,
                                "source_id": settled.source_id,
                                "provider_job_id": settled.native_job_id,
                                "publication_started_at": settled.publication_started_at,
                            }
                        )
                    return settled

                settled = await join_cleanup(finalize())
            latest = await join_off_loop(service.get, record.source.workspace_id, record.source.id)
            return await join_off_loop(result, latest, settled)

        return await work.run(
            _clio_submission, "connected_data_continue", {"operation_id": operation_id}, body
        )

    async def recover_provider(task: TaskSnapshot, context: SubmissionContext) -> bool:
        def prepare() -> ProviderContinuation | None:
            if task.kind != "Download":
                return None
            try:
                operation = service.store.get("operation", task.handle, TransferOperation)
            except KeyError:
                return None
            session, _ = owner(context)
            record = service.get(session.workspace_id, operation.source_id)
            if record.source.provider != "globus":
                return None
            if operation.native_request is None and operation.native_job_id is None:
                return None
            if (
                not operation.native_job_id
                or task.submission is None
                or task.backend is None
                or not task.backend.task_id
                or operation.provider_identity is None
            ):
                raise ValueError(
                    "Provider acceptance/custody is uncertain; inspect the original job, without resubmission"
                )
            validate_source(record, context, operation)
            attempts = work.history.native_attempts(task.handle)
            previous_leg = [
                attempt for attempt in attempts if attempt.backend.task_id == task.backend.task_id
            ]
            if len(previous_leg) > 1:
                raise ValueError(
                    "The lost native leg had multiple execution attempts; inspect provider effects before continuing"
                )
            cancel_only = cancellation_needed(context)
            if operation.publication_started_at is not None and not cancel_only:
                raise ValueError(
                    "Local publication may already have happened; inspect its manifest/files before continuing"
                )
            origin = (
                context.provider_continuation.original_submission
                if context.provider_continuation is not None
                else task.submission
            )
            return ProviderContinuation(
                original_submission=origin,
                previous_submission=task.submission,
                previous_backend=task.backend,
                operation_id=operation.id,
                source_id=operation.source_id,
                provider_job_id=operation.native_job_id,
                cancellation_only=cancel_only,
                previous_incarnations=tuple(attempt.incarnation for attempt in attempts),
            )

        continuation: ProviderContinuation | None = None

        async def reattach() -> bool:
            nonlocal continuation
            if native._closing and not await join_off_loop(cancellation_needed, context):
                return True  # only observation retires; recording retries remain pending
            if "provider-reservation:" + task.handle in native.recovery.pending:
                return False  # the exact reserved context owns its safe retry
            latest = await join_off_loop(work.history.task_snapshot, task.handle)
            if (
                latest is None
                or latest.terminal_at is not None
                or latest.submission != task.submission
            ):
                return True  # a different reservation must never be submitted again
            continuation = await join_off_loop(prepare)
            if continuation is not None:
                await native.continue_provider(task, context, continuation)
            return True

        try:
            await reattach()
            if continuation is None:
                return "provider-reservation:" + task.handle in native.recovery.pending
        except Exception as error:
            logger.exception(
                "Task boundary reason=provider_reattachment_failed error_type=%s",
                type(error).__name__,
            )
            reason = str(error)
            native.recovery.defer("provider-reattach:" + task.handle, reattach)
            await native._record_or_defer(
                "provider-recovery:" + task.handle,
                lambda: native._uncertain(task.handle, reason, lost=True),
            )
        return True

    async def cancel_provider(task: TaskSnapshot) -> bool:
        def request() -> bool:
            if task.kind != "Download":
                return False
            try:
                operation = service.store.get("operation", task.handle, TransferOperation)
            except KeyError:
                return False
            if not operation.native_job_id:
                return False
            session = app.state.sessions.get(task.owner.session_id)
            if session is None:
                raise ValueError("Provider cancellation owner is unavailable")
            record = service.get(session.workspace_id, operation.source_id)
            if record.source.provider != "globus":
                return False
            operation = service.store.update_operation(operation.id, cancel_requested=True)
            with service.adapter(record) as adapter:
                if not isinstance(adapter, GlobusSource):
                    raise ValueError("Provider cancellation does not match the retained job")
                adapter.request_cancel_existing(operation)
            return True  # retain the native body so provider/staging cleanup can settle

        return await join_off_loop(request)

    async def provider_survives_shutdown(task: TaskSnapshot) -> bool:
        def classify() -> bool:
            if task.kind != "Download" or task.submission is None:
                return False
            context = SubmissionContext.model_validate(work.history.read_content(task.submission))
            source_id = (
                context.provider_continuation.source_id
                if context.provider_continuation is not None
                else context.arguments.get("source_id")
            )
            if not isinstance(source_id, str):
                raise ValueError("Shutdown provider custody is unavailable")
            identity = context.attribution.get("source_identity")
            if isinstance(identity, dict) and identity.get("source_id") == source_id:
                return identity.get("provider") == "globus"
            # Classification reads original source custody. A deleted session or
            # workspace cannot turn a surviving provider job into local work.
            record = service.store.get("source", source_id, SourceRecord)
            return record.source.provider == "globus"

        return await join_off_loop(classify)

    native.provider_shutdown = provider_survives_shutdown
    native.provider_recovery = recover_provider
    native.provider_cancel = cancel_provider

    @root.tool(name="connected_data_download", task=TaskConfig(mode="required"))
    async def download(
        ctx: Context, source_id: str, _clio_submission: str, paths: list[str] | None = None
    ) -> dict[str, Any]:
        """Download approved source paths; return a task handle immediately and notify on completion.

        Continue independent work, observe progress or wait_tasks explicitly.
        Conversation Stop leaves the transfer running; cancel_tasks cancels it.
        """

        async def body(context: SubmissionContext, evidence: WorkEvidence) -> dict[str, Any]:
            created: TransferOperation | None = None
            loop = asyncio.get_running_loop()

            def report(done: int, total: int) -> None:
                def record() -> None:
                    progress(context, done, total, "bytes")

                try:
                    record()
                except Exception as error:
                    logger.exception(
                        "Task boundary reason=download_progress_recording_unconfirmed error_type=%s",
                        type(error).__name__,
                    )

                    async def retry() -> None:
                        await work.history.io.run(record)

                    loop.call_soon_threadsafe(
                        work.recovery.defer, "storage-progress:" + context.handle, retry
                    )

            def prepare() -> tuple[SourceRecord, TransferOperation, Path]:
                nonlocal created
                session, workspace = owner(context)
                if paths is not None and not 1 <= len(paths) <= 1000:
                    raise ValueError("Select between 1 and 1000 relative source paths")
                for path in paths or ():
                    FileEntry(path=path, kind="file")
                record = service.get(session.workspace_id, source_id)
                try:
                    operation = service.store.get("operation", context.handle, TransferOperation)
                except KeyError:
                    validate_source(record, context)
                    service.validate_download(record)
                    operation = service.store.begin_operation(
                        record.source.id, "materialize", identifier=context.handle
                    )
                    created = operation
                    operation = service.store.update_operation(
                        operation.id,
                        selected_paths=list(paths) if paths is not None else None,
                        description=context.tool,
                    )
                else:
                    if (
                        operation.source_id != record.source.id
                        or operation.kind != "materialize"
                        or operation.selected_paths != paths
                    ):
                        raise ValueError("Retained transfer does not match the accepted invocation")
                    # Native redelivery reuses provider custody. In particular,
                    # Globus.poll inspects native_job_id instead of submitting a
                    # second transfer when that identity has been confirmed.
                validate_source(record, context, operation)
                associate(context, record, operation)
                return record, operation, workspace

            try:
                record, operation, workspace = await join_off_loop(prepare)
            except BaseException:
                if created is not None:
                    await join_off_loop(
                        service.store.update_operation,
                        created.id,
                        state="cancelled",
                        cancel_requested=True,
                    )
                raise
            evidence.acquiring_effects()
            retired = False
            try:
                retired = not await service.execute_transfer(
                    record,
                    workspace,
                    operation,
                    progress=report,
                    cancellation_requested=lambda: cancellation_needed(context),
                    shutdown_requested=lambda: native._closing,
                )
            finally:

                async def finalize() -> TransferOperation:
                    settled = await join_off_loop(
                        service.store.get, "operation", operation.id, TransferOperation
                    )
                    if settled.state in {"completed", "failed", "cancelled"} and (
                        record.source.provider != "globus" or settled.provider_cleanup_confirmed
                    ):
                        drafts = getattr(app.state, "source_drafts", None)
                        if drafts is not None:
                            await join_off_loop(drafts.cleanup)
                        evidence.confirm_cleanup()
                    if record.source.provider == "globus" and (
                        retired or not settled.provider_cleanup_confirmed
                    ):
                        evidence.retire_provider_observer(
                            {
                                "operation_id": settled.id,
                                "source_id": settled.source_id,
                                "provider_job_id": settled.native_job_id,
                                "publication_started_at": settled.publication_started_at,
                            }
                        )
                    return settled

                settled = await join_cleanup(finalize())
            latest = await asyncio.to_thread(service.get, record.source.workspace_id, source_id)
            return await asyncio.to_thread(result, latest, settled)

        return await work.run(
            _clio_submission,
            "connected_data_download",
            {"source_id": source_id, "paths": paths},
            body,
        )

    async def index_body(
        context: SubmissionContext, evidence: WorkEvidence, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        created: TransferOperation | None = None
        loop = asyncio.get_running_loop()

        def report(count: int) -> None:
            def record() -> None:
                progress(context, count, None, "entries")

            try:
                record()
            except Exception as error:
                logger.exception(
                    "Task boundary reason=indexing_progress_recording_unconfirmed error_type=%s",
                    type(error).__name__,
                )

                async def retry() -> None:
                    await work.history.io.run(record)

                loop.call_soon_threadsafe(
                    work.recovery.defer, "storage-progress:" + context.handle, retry
                )

        def prepare() -> tuple[SourceRecord, TransferOperation] | dict[str, Any]:
            nonlocal created
            session, workspace = owner(context)
            if "source_id" in arguments:
                record = service.get(session.workspace_id, arguments["source_id"])
            else:
                identity = context.attribution.get("source_identity")
                approved_source_id: str | None = None
                if identity is not None:
                    selected_source = (
                        identity.get("source_id") if isinstance(identity, dict) else None
                    )
                    if not isinstance(selected_source, str):
                        raise PermissionError("Approved source identity is unavailable")
                    approved_source_id = selected_source
                if context.attribution.get("ndp_selection_digest") is not None:
                    from clio_agent.gact.storage.connect_tool import connection_approval

                    _, _, digest = connection_approval(service, session.workspace_id, arguments)
                    if digest != context.attribution["ndp_selection_digest"]:
                        raise PermissionError("Approved NDP selection changed before execution")
                prepared = prepare_connect(
                    app,
                    arguments,
                    human_action=True,
                    session_id=context.owner.session_id,
                    approved_source_id=approved_source_id,
                )
                if "immediate_result" in prepared:
                    return prepared["immediate_result"]
                record = service.get(session.workspace_id, prepared["source_id"])
            validate_source(record, context)
            operation = service.store.begin_operation(
                record.source.id, "indexing", identifier=context.handle
            )
            created = operation
            associate(context, record, operation)
            return record, operation

        try:
            prepared = await join_off_loop(prepare)
        except BaseException:
            if created is not None:
                await join_off_loop(
                    service.store.update_operation,
                    created.id,
                    state="cancelled",
                    cancel_requested=True,
                )
            raise
        if isinstance(prepared, dict):
            return prepared
        record, operation = prepared
        evidence.acquiring_effects()
        try:
            await run_index(service, record, operation, report_progress=report)
        finally:

            async def finalize() -> TransferOperation:
                settled = await join_off_loop(
                    service.store.get, "operation", operation.id, TransferOperation
                )
                if settled.state in {"completed", "failed", "cancelled"}:
                    evidence.confirm_cleanup()
                return settled

            settled = await join_cleanup(finalize())
        latest = await asyncio.to_thread(
            service.store.get, "source", record.source.id, SourceRecord
        )
        return await asyncio.to_thread(result, latest, settled)

    @root.tool(name="connected_data_connect", task=TaskConfig(mode="required"))
    async def connect(
        ctx: Context,
        provider: str,
        root: str,
        label: str,
        _clio_submission: str,
        mode: str = "read_only",
        configuration: SourceConfiguration | NDPConnectionSelection | None = None,
    ) -> dict[str, Any]:
        """Connect and index a folder as a task; Stop leaves accepted indexing running."""
        arguments = {
            "provider": provider,
            "root": root,
            "label": label,
            "mode": mode,
            "configuration": configuration.model_dump(mode="json")
            if configuration is not None
            else None,
        }

        async def body(context: SubmissionContext, evidence: WorkEvidence) -> dict[str, Any]:
            return await index_body(context, evidence, arguments)

        return await work.run(_clio_submission, "connected_data_connect", arguments, body)

    @root.tool(name="connected_data_index", task=TaskConfig(mode="required"))
    async def index(ctx: Context, source_id: str, _clio_submission: str) -> dict[str, Any]:
        """Refresh a connected folder manifest as a task, retaining its previous manifest on failure."""

        async def body(context: SubmissionContext, evidence: WorkEvidence) -> dict[str, Any]:
            return await index_body(context, evidence, {"source_id": source_id})

        return await work.run(
            _clio_submission, "connected_data_index", {"source_id": source_id}, body
        )
