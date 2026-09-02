"""Converter lifecycle for one workspace resource revision.

The owner module for "what happens to a resource after its bytes land": the
submit / poll / complete / fail / cancel transitions, the workspace lifecycle
events they publish, and the two races the route layer got wrong.

Two invariants live here rather than in the HTTP layer, because both are
properties of the LIFECYCLE and not of any one request:

* **A poll never overwrites a decision taken while it was in flight.** Both
  ``submit_processing`` and :func:`refresh_processing` re-read durable state
  after their ``await`` and yield to a cancellation that landed meanwhile — a
  status round-trip is a read, and a read must not resurrect work the user
  stopped.
* **A converter that stops answering is a typed failure, not silence.** Polls
  that raise are counted on the durable record; past
  ``resources.status_poll_failure_threshold`` consecutive failures the record
  transitions to ``failed`` with a ``converter_status_unavailable`` reason
  naming the exception class, so the resource stops sitting in ``processing``
  forever and ``reprocess`` becomes available again.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from clio_agent import conf
from clio_agent.gact.events import Event
from clio_agent.gact.resource_custody import ResourceRecord
from clio_agent.gact.resource_processing import (
    ResourceConverterUnavailable,
    ResourceCustodyGone,
    ResourceProcessingRecord,
)

logger = logging.getLogger(__name__)

# Exceptions a converter round-trip may raise without it being a server bug.
CONVERTER_TRANSPORT_ERRORS = (
    httpx.HTTPError,
    ResourceConverterUnavailable,
    OSError,
    RuntimeError,
    ValueError,
)


def status_poll_failure_threshold() -> int:
    """Consecutive failed status polls tolerated before a typed degradation."""

    return max(
        1,
        conf.resolve(
            "resources.status_poll_failure_threshold",
            env="CLIO_RESOURCE_STATUS_POLL_FAILURE_THRESHOLD",
            default=5,
            cast=conf.as_int,
        ),
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit_workspace_event(
    app: Any, workspace_id: str, event_type: str, payload: dict[str, Any]
) -> None:
    """Publish one lifecycle event to every session that belongs to the workspace."""

    for session in app.state.sessions.list(workspace_id=workspace_id):
        app.state.bus.publish(Event(type=event_type, session_id=session.id, payload=payload))


def _persist(app: Any, record: ResourceRecord, state: ResourceProcessingRecord) -> bool:
    """Persist processing state, refusing once the resource itself is gone.

    Returns whether the write landed. A DELETE that races an in-flight submit
    must not have its custody tree re-created underneath it, and the ghost must
    not keep emitting lifecycle events — so the refusal is typed and logged
    rather than swallowed.
    """

    try:
        app.state.resource_processing_store.save_state(record, state)
    except ResourceCustodyGone:
        logger.info(
            "dropped resource processing state reason=resource_custody_gone "
            "workspace=%s resource=%s revision=%s state=%s",
            record.workspace_id,
            record.id,
            record.revision,
            state.state,
        )
        return False
    return True


def persist_completed_processing(
    app: Any,
    record: ResourceRecord,
    state: ResourceProcessingRecord,
    result: dict[str, Any],
) -> ResourceProcessingRecord:
    """Persist a converter result without letting malformed output break reads."""

    try:
        completed = app.state.resource_processing_store.save_result(record, state, result)
    except ResourceCustodyGone:
        logger.info(
            "dropped converter result reason=resource_custody_gone workspace=%s resource=%s",
            record.workspace_id,
            record.id,
        )
        return state
    except ValueError as exc:
        failed = state.model_copy(
            update={
                "state": "failed",
                "failure": {"code": "processor_result_invalid", "detail": str(exc)},
                "updated_at": _now_iso(),
            }
        )
        if _persist(app, record, failed):
            emit_workspace_event(
                app, record.workspace_id, "resource.processing_failed", failed.model_dump()
            )
        return failed
    emit_workspace_event(
        app, record.workspace_id, "resource.processing_completed", completed.model_dump()
    )
    return completed


def _record_poll_failure(
    app: Any,
    record: ResourceRecord,
    state: ResourceProcessingRecord,
    exc: BaseException,
) -> ResourceProcessingRecord:
    """Count a failed status poll and degrade to a typed failure past the bound."""

    failures = state.poll_failures + 1
    threshold = status_poll_failure_threshold()
    if failures < threshold:
        counted = state.model_copy(update={"poll_failures": failures, "updated_at": _now_iso()})
        _persist(app, record, counted)
        return counted
    failed = state.model_copy(
        update={
            "state": "failed",
            "poll_failures": failures,
            "failure": {
                "code": "converter_status_unavailable",
                "processor": state.processor,
                "exception": type(exc).__name__,
                "consecutive_failures": failures,
            },
            "updated_at": _now_iso(),
        }
    )
    logger.warning(
        "resource conversion degraded reason=converter_status_unavailable processor=%s "
        "resource=%s consecutive_failures=%d exception=%s",
        state.processor,
        record.id,
        failures,
        type(exc).__name__,
    )
    if _persist(app, record, failed):
        emit_workspace_event(
            app, record.workspace_id, "resource.processing_failed", failed.model_dump()
        )
    return failed


async def refresh_processing(app: Any, record: ResourceRecord) -> ResourceProcessingRecord:
    """Advance one resource's processing state from its converter."""

    state = app.state.resource_processing_store.state(record)
    if state.state not in {"submitted", "processing"} or not state.job_id:
        return state
    try:
        payload = await app.state.resource_converter_factory.status(state)
    except CONVERTER_TRANSPORT_ERRORS as exc:
        return _record_poll_failure(app, record, state, exc)

    # The status round-trip is a READ. A cancel that landed while it was in
    # flight is the user's decision and outranks whatever the converter says.
    latest = app.state.resource_processing_store.state(record)
    if latest.state not in {"submitted", "processing"}:
        return latest
    state = state.model_copy(update={"poll_failures": 0})

    remote_state = str(payload.get("status") or "processing")
    if remote_state == "complete":
        result = payload.get("result")
        if not isinstance(result, dict):
            failed = state.model_copy(
                update={
                    "state": "failed",
                    "failure": {"code": "processor_result_invalid"},
                    "updated_at": _now_iso(),
                }
            )
            if _persist(app, record, failed):
                emit_workspace_event(
                    app, record.workspace_id, "resource.processing_failed", failed.model_dump()
                )
            return failed
        return persist_completed_processing(app, record, state, result)
    if remote_state in {"failed", "cancelled"}:
        failure = payload.get("failure")
        failed = state.model_copy(
            update={
                "state": "failed",
                "failure": failure if isinstance(failure, dict) else {"code": remote_state},
                "updated_at": _now_iso(),
            }
        )
        if _persist(app, record, failed):
            emit_workspace_event(
                app, record.workspace_id, "resource.processing_failed", failed.model_dump()
            )
        return failed
    progress = payload.get("progress", state.progress)
    updated = state.model_copy(
        update={
            "state": "processing",
            "progress": int(progress) if isinstance(progress, int | float) else state.progress,
            "updated_at": _now_iso(),
        }
    )
    _persist(app, record, updated)
    return updated


async def cancel_remote_job(app: Any, state: ResourceProcessingRecord) -> dict[str, Any]:
    """Best-effort remote cancellation, reporting the failure rather than hiding it."""

    if not state.job_id:
        return {"remote_cancelled": False, "remote_error": "no_remote_job"}
    try:
        return await app.state.resource_converter_factory.cancel(state)
    except CONVERTER_TRANSPORT_ERRORS as exc:
        return {"remote_cancelled": False, "remote_error": type(exc).__name__}


async def submit_processing(
    app: Any,
    record: ResourceRecord,
    *,
    raise_unavailable: bool,
    reprocess: bool = False,
) -> ResourceProcessingRecord:
    """Select and submit through the converter registry, preserving lifecycle events."""

    current = app.state.resource_processing_store.state(record)
    queued_locally = current.state == "submitted" and not current.job_id
    if (current.state in {"submitted", "processing"} and not queued_locally) or (
        current.state == "complete" and not reprocess
    ):
        return current
    if current.state == "cancelled" and not reprocess:
        return current
    try:
        submission = await app.state.resource_converter_factory.submit(
            record,
            app.state.resource_store.content_path(record),
            reprocess=reprocess,
        )
    except ResourceConverterUnavailable as exc:
        if raise_unavailable:
            raise
        failed = current.model_copy(
            update={
                "state": "failed",
                "failure": {
                    "code": "resource_converter_unavailable",
                    "media_type": record.detected_mime,
                    "attempted": [converter_id for converter_id, _error in exc.failures],
                },
                "updated_at": _now_iso(),
            }
        )
        if _persist(app, record, failed):
            emit_workspace_event(
                app, record.workspace_id, "resource.processing_failed", failed.model_dump()
            )
        return failed

    converter = submission.converter
    submitted = submission.payload
    processing = ResourceProcessingRecord(
        workspace_id=record.workspace_id,
        resource_id=record.id,
        resource_revision=record.revision,
        source_sha256=record.sha256,
        processor=converter.id,
        processor_url=converter.endpoint,
        job_id=str(submitted["id"]),
        state="submitted",
        derivatives_available=current.derivatives_available,
    )
    latest = app.state.resource_processing_store.state(record)
    if latest.state == "cancelled" and not reprocess:
        remote = await cancel_remote_job(app, processing)
        cancelled = latest.model_copy(
            update={
                "updated_at": _now_iso(),
                "cancellation": {**latest.cancellation, **remote},
            }
        )
        _persist(app, record, cancelled)
        return cancelled
    if not _persist(app, record, processing):
        return processing
    if not queued_locally:
        emit_workspace_event(
            app, record.workspace_id, "resource.processing_started", processing.model_dump()
        )
    if str(submitted.get("status")) == "complete" and isinstance(submitted.get("result"), dict):
        processing = persist_completed_processing(app, record, processing, submitted["result"])
    return processing


def schedule_processing(app: Any, record: ResourceRecord, background_tasks: Any) -> None:
    """Start automatic conversion only when a registered converter supports the MIME."""

    converter = app.state.resource_converter_factory.get_converter(record)
    if converter is None:
        return
    current = app.state.resource_processing_store.state(record)
    if current.state in {"submitted", "processing", "complete"}:
        return
    queued = ResourceProcessingRecord(
        workspace_id=record.workspace_id,
        resource_id=record.id,
        resource_revision=record.revision,
        source_sha256=record.sha256,
        processor=converter.id,
        processor_url=converter.endpoint,
        state="submitted",
        derivatives_available=current.derivatives_available,
    )
    if not _persist(app, record, queued):
        return
    emit_workspace_event(
        app, record.workspace_id, "resource.processing_started", queued.model_dump()
    )
    background_tasks.add_task(submit_processing, app, record, raise_unavailable=False)


__all__ = [
    "CONVERTER_TRANSPORT_ERRORS",
    "cancel_remote_job",
    "emit_workspace_event",
    "persist_completed_processing",
    "refresh_processing",
    "schedule_processing",
    "status_poll_failure_threshold",
    "submit_processing",
]
