"""The transcript FILE copy switch: ``transcript.file`` (owner module).

Every gact message is minted as ``message_part`` atoms on clio-core's canonical
``_events/m`` lane -- the transcript's source of truth
(:mod:`clio_agent.gact.transcript_projection`). Next to the atoms the server also
keeps a whole-ledger JSON copy per session under ``messages/<sid>.json``
(:class:`~clio_agent.gact.messages.MessageStore`), which today is the boot index,
the repair/backfill source, and History mode's only durable transcript.

``transcript.file`` (env ``CLIO_TRANSCRIPT_FILE``, default ``true``) switches that
file copy. It is resolved ONCE, at app build, by :func:`boot_transcript_store`:

* **on** -- exactly the pre-switch behaviour: the file is written and read as before.
* **off** -- clio-core is the ONLY transcript store. No ``messages/`` file is
  written or read:

  - the session index (``in`` / iteration / ``len`` of ``app.state.messages`` and
    ``has_session``) comes from the session registry (:class:`RegistryTranscriptIndex`),
    so a registered session with no messages reads as ``[]``;
  - a cold read assembles the atoms only (:func:`materialize_from_atoms`): no
    repair or backfill from a file. A busy lane is waited for off the loop, and is
    a typed retryable :class:`TranscriptNotReadyError` on a loop thread (never a
    blocked loop -- ``arc.loop_guard``, #1334);
  - the restart reconciliation, the metrics seed and the interaction time of a
    session row older than ``last_interaction_at`` (its last atom message, else its
    creation) read the atoms, at build when the ARC is already bound, else right
    after the process ARC attaches (:func:`on_process_arc_bound`); ``GET
    /v1/metrics`` is a typed retryable error until then;
  - a whole-transcript replace (undo, rewind, fork, compact, import, clear) is
    atomic: a new lane generation, then one pointer switch
    (:mod:`clio_agent.arc.lane_generations`); when it fails the previous transcript
    stays and the session's in-memory ledger is dropped
    (:func:`reload_resident_on_failure`) so reads serve what clio-core kept;
  - an atom mint is the durable write: when it fails the message is taken back
    out of the in-memory ledger (:func:`forget_unminted_on_failure`) and the
    typed error propagates.

  History mode (no clio-core binding) keeps the transcript only in the file, so
  ``off`` there is a typed boot error (:class:`TranscriptFileRequiredError`).

The switch exists so the file copy can be retired once clio-core writes its
content down as files itself; until then it stays on by default.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from clio_agent import conf
from clio_agent.errors import ClioError, ConfigError
from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.sessions import SessionStore
    from clio_agent.gact.types import Message

logger = logging.getLogger(__name__)

TRANSCRIPT_FILE_KEY = "transcript.file"
TRANSCRIPT_FILE_ENV = "CLIO_TRANSCRIPT_FILE"

#: Typed reasons carried by :class:`TranscriptNotReadyError`.
LANE_BUSY_REASON = "transcript_lane_busy"
STORE_NOT_ATTACHED_REASON = "transcript_store_not_attached"
STORE_UNAVAILABLE_REASON = "transcript_store_unavailable"
BOOT_FAILED_REASON = "transcript_boot_failed"


class TranscriptFileRequiredError(ConfigError):
    """``transcript.file: false`` was asked for in History mode (no clio-core)."""

    reason = "transcript_file_required"

    def __init__(self) -> None:
        super().__init__(
            "History mode keeps the transcript only in a file, so it cannot run with "
            "transcript.file: false. Set transcript.file: true (or unset "
            "CLIO_TRANSCRIPT_FILE), or install clio-core for this platform.",
            details={
                "reason": self.reason,
                "key": TRANSCRIPT_FILE_KEY,
                "env": TRANSCRIPT_FILE_ENV,
                "context_mode": "history",
            },
        )


class TranscriptNotReadyError(ClioError):
    """A transcript read cannot be served right now; retrying it will succeed.

    Raised only with the file copy off, where clio-core is the one store: on a
    loop thread while another writer holds the session's lane, or while the
    process ARC is not attached yet. Served as a 503 with ``recoverable: true``.
    """

    def __init__(self, session_id: str, reason: str, message: str) -> None:
        super().__init__(
            message,
            error_type="transcript_not_ready",
            details={"session_id": session_id, "reason": reason},
        )
        self.session_id = session_id
        self.reason = reason


def resolve_transcript_file() -> bool:
    """Resolve ``transcript.file`` (config file -> ``CLIO_TRANSCRIPT_FILE`` -> ``true``).

    The ONE read of the switch; :func:`boot_transcript_store` calls it once per app
    and stores the answer on ``app.state.transcript_file``.

    Raises:
        ConfigError: The value is not a boolean.
    """

    try:
        return conf.resolve(
            "transcript.file", env="CLIO_TRANSCRIPT_FILE", default=True, cast=conf.as_bool
        )
    except ValueError as exc:
        raise ConfigError(
            f"transcript.file must be true or false: {exc}",
            details={"key": TRANSCRIPT_FILE_KEY, "env": TRANSCRIPT_FILE_ENV},
        ) from exc


def file_transcript_enabled(app: Any) -> bool:
    """Whether this app keeps the ``messages/`` file copy.

    Reads the value :func:`boot_transcript_store` resolved. An app that was not
    built by ``build_app`` (minimal test wiring) has no such value and keeps the
    file regime it was written for.
    """

    return bool(getattr(getattr(app, "state", None), "transcript_file", True))


class RegistryTranscriptIndex:
    """The transcript index with the file copy off: the session registry.

    Answers the two index questions :class:`~clio_agent.gact.resident_ledgers.
    ResidentLedgerSet` asks of its store -- which sessions have a transcript, and
    does this one -- without reading a message body. Every registered session has
    a transcript (possibly empty); its body lives on clio-core.
    """

    def __init__(self, sessions: "SessionStore") -> None:
        self._sessions = sessions

    def session_ids(self) -> list[str]:
        """Every registered session id."""

        return [session.id for session in self._sessions.list()]

    def has_session(self, session_id: str) -> bool:
        """Whether ``session_id`` is a registered session."""

        return self._sessions.get(session_id) is not None


def _loop_running_here() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _canonical_log_arc(app: "FastAPI", session_id: str) -> Any:
    """The ARC holding the atoms, attaching the process ARC off the loop if needed."""

    from clio_agent.gact.transcript_projection import canonical_log_arc  # noqa: PLC0415

    arc = canonical_log_arc(app)
    if arc is not None:
        return arc
    if _loop_running_here():
        raise TranscriptNotReadyError(
            session_id,
            STORE_NOT_ATTACHED_REASON,
            "The conversation is stored in clio-core, which is still attaching. Retry shortly.",
        )
    from clio_agent.gact.server_boot import process_arc  # noqa: PLC0415 - import cycle

    process_arc(app)  # the one construction door; joins an in-flight attach
    arc = canonical_log_arc(app)
    if arc is None:
        raise TranscriptNotReadyError(
            session_id,
            STORE_UNAVAILABLE_REASON,
            "The conversation is stored in clio-core, and no clio-core store is bound.",
        )
    return arc


def materialize_from_atoms(app: "FastAPI", session_id: str) -> Optional[list["Message"]]:
    """Read one session's transcript from its atoms only (file copy off).

    ``None`` when the session is neither registered nor has atoms (the resident
    set's cache-miss ``KeyError``); ``[]`` for a registered session with no
    messages yet. A lane held by a writer is waited for off the loop (the lock is
    released when the in-flight append lands); on a loop thread it is a typed
    :class:`TranscriptNotReadyError` instead -- the read is never served empty.
    """

    from clio_agent.gact.part_atoms import MESSAGE_PART_SCOPE  # noqa: PLC0415
    from clio_agent.gact.transcript_projection import (  # noqa: PLC0415 - import cycle
        assemble_session_messages,
        has_atoms,
    )

    arc = _canonical_log_arc(app, session_id)
    lane_lock = arc._segments._lock_for(session_id, MESSAGE_PART_SCOPE)
    if not lane_lock.acquire(blocking=False):
        if _loop_running_here():
            stream_audit(
                "transcript.lane_busy_retryable",
                session_id=session_id,
                reason=LANE_BUSY_REASON,
                source="materialize_from_atoms",
            )
            raise TranscriptNotReadyError(
                session_id,
                LANE_BUSY_REASON,
                "The conversation is being written right now. Retry shortly.",
            )
        lane_lock.acquire()  # off the loop: wait for the in-flight append to land
    try:
        if has_atoms(arc, session_id):
            return assemble_session_messages(arc, session_id)
    finally:
        lane_lock.release()
    return [] if app.state.transcript_index.has_session(session_id) else None


def load_durable_transcript(app: "FastAPI", session_id: str) -> Optional[list["Message"]]:
    """The durable transcript of one session: the file when on, the atoms when off."""

    if file_transcript_enabled(app):
        store = getattr(app.state, "message_store", None)
        return None if store is None else store.load_session(session_id)
    return materialize_from_atoms(app, session_id)


def has_durable_transcript_store(app: "FastAPI") -> bool:
    """Whether this app has a durable transcript to read (always, with the file off)."""

    if file_transcript_enabled(app):
        return getattr(app.state, "message_store", None) is not None
    return True


@contextmanager
def forget_unminted_on_failure(
    app: "FastAPI", session_id: str, message: "Message"
) -> Iterator[None]:
    """Wrap a message's atom mint; with the file off, a failed mint un-appends it.

    With no file copy the atoms are the only durable record, so a message whose
    mint failed must not stay in the in-memory ledger (it would vanish on the next
    eviction). It is removed -- by identity -- and the mint's error propagates. With
    the file on this is a pass-through.
    """

    try:
        yield
    except BaseException:
        if not file_transcript_enabled(app):
            _forget_in_memory(app, session_id, message)
        raise


@contextmanager
def reload_resident_on_failure(app: "FastAPI", session_id: str) -> Iterator[None]:
    """Wrap a durable transcript write; with the file off, a failure drops memory.

    The replace / extend seams update the in-memory ledger before (or apart from) the
    clio-core write. With no file copy the atoms are the only durable record, so when
    that write fails the session's resident ledger is dropped and its metrics
    re-seeded from what clio-core holds -- the next read serves the durable
    transcript, never rows that were not stored. The write's error propagates
    unchanged. With the file on this is a pass-through.
    """

    try:
        yield
    except BaseException as failure:
        if not file_transcript_enabled(app):
            _reload_resident(app, session_id, failure)
        raise


def _reload_resident(app: "FastAPI", session_id: str, failure: BaseException) -> None:
    messages = app.state.messages
    discard = getattr(messages, "discard", None)
    if callable(discard):
        discard(session_id)  # non-materializing: the next read assembles the atoms
    else:
        messages.pop(session_id, None)
    counters = getattr(app.state, "metrics_counters", None)
    if counters is None:
        return
    try:
        durable = materialize_from_atoms(app, session_id) or []
    except (ClioError, OSError, RuntimeError) as reseed_error:
        # The write's own typed error is what the caller must see; the metrics
        # re-seed failing too is named on it and logged, never dropped.
        failure.add_note(f"metrics for session {session_id} not re-seeded: {reseed_error!r}")
        logger.error(
            "transcript metrics not re-seeded reason=transcript_metrics_reseed_failed "
            "session=%s error=%r",
            session_id,
            reseed_error,
        )
        return
    counters.set_session(session_id, durable)


def _forget_in_memory(app: "FastAPI", session_id: str, message: "Message") -> None:
    messages = app.state.messages
    get_if_resident = getattr(messages, "get_if_resident", None)
    rows = get_if_resident(session_id) if callable(get_if_resident) else messages.get(session_id)
    if rows is None:
        return
    rows[:] = [row for row in rows if row is not message]
    counters = getattr(app.state, "metrics_counters", None)
    if counters is not None:
        counters.set_session(session_id, rows)


def seeded_metrics_counters(app: "FastAPI") -> Any:
    """The metrics aggregate once its boot seed ran, else a typed retryable error."""

    ready = getattr(app.state, "transcript_boot_ready", None)
    if ready is not None and not ready.is_set():
        failure = getattr(app.state, "transcript_boot_error", "")
        if failure:
            raise TranscriptNotReadyError(
                "",
                BOOT_FAILED_REASON,
                "Message metrics could not be loaded from clio-core at startup "
                f"({failure}); restart the server once clio-core is healthy.",
            )
        raise TranscriptNotReadyError(
            "",
            STORE_NOT_ATTACHED_REASON,
            "Message metrics are loaded from clio-core, which is still attaching. Retry shortly.",
        )
    return app.state.metrics_counters


def _last_interaction(rows: list["Message"], created_at: str) -> str:
    """When a legacy row was last used: its last message's time, else its creation."""

    if not rows:
        return created_at
    last = rows[-1]
    return str(last.updated_at or last.created_at or created_at)


def _seed_from_atoms(app: "FastAPI") -> None:
    """Metrics seed + legacy interaction times, from ONE atom read per session.

    A session row persisted before ``last_interaction_at`` existed gets it from its
    last atom message (``created_at`` when it has none) -- the file-off counterpart
    of the ``messages/`` file mtime the file-on boot reads. ``messages/`` is never
    touched.
    """

    counters = app.state.metrics_counters
    sessions = app.state.sessions
    unknown = {row.id: row.created_at for row in sessions.sessions_without_interaction_time()}
    times: dict[str, str] = {}
    for session_id in app.state.transcript_index.session_ids():
        rows = materialize_from_atoms(app, session_id) or []
        counters.set_session(session_id, rows)
        if session_id in unknown:
            times[session_id] = _last_interaction(rows, unknown[session_id])
    sessions.settle_interaction_times(times)


def _settle_interaction_from_files(app: "FastAPI") -> None:
    """File on: a legacy row's interaction time is its ``messages/`` file's mtime."""

    store = app.state.message_store
    sessions = app.state.sessions
    sessions.settle_interaction_times(
        {
            row.id: store.modified_at(row.id) or row.created_at
            for row in sessions.sessions_without_interaction_time()
        }
    )


def _atom_boot(app: "FastAPI") -> None:
    """Restart reconciliation + metrics seed from the atoms (file off, ARC bound)."""

    from clio_agent.gact.session_store import (  # noqa: PLC0415 - import cycle
        _reconcile_restart_interrupted_sessions,
    )

    _reconcile_restart_interrupted_sessions(app)
    _seed_from_atoms(app)
    app.state.transcript_boot_ready.set()


def on_process_arc_bound(app: "FastAPI") -> None:
    """Run the deferred atom boot once the process ARC is published (file off).

    Called by ``server_boot`` on the worker thread that constructed the ARC, inside
    the single-flight construction, so a caller awaiting the ARC also awaits this. A
    failure is recorded on ``app.state.transcript_boot_error`` (``GET /v1/metrics``
    names it) and re-raised to the construction's own typed error handling.
    """

    ready = getattr(app.state, "transcript_boot_ready", None)
    if ready is None or ready.is_set() or file_transcript_enabled(app):
        return
    try:
        _atom_boot(app)
    except Exception as exc:
        app.state.transcript_boot_error = repr(exc)
        raise


def _refuse_history_mode(app: "FastAPI") -> None:
    if getattr(app.state, "arc", None) is not None:
        return
    from clio_agent.arc import history_mode  # noqa: PLC0415

    if history_mode.resolve().is_history:
        raise TranscriptFileRequiredError()


def install_transcript_error_handler(app: "FastAPI") -> None:
    """Serve the transcript store's typed errors as GACT envelopes.

    :class:`TranscriptNotReadyError` is a retryable 503. A failed whole-transcript
    replace (:class:`~clio_agent.arc.lane_generations.LaneReplaceError`) is a
    retryable 503 when the conversation is unchanged, and a non-retryable 500 when
    the change landed but the old copy is still there (repeating it would apply it
    twice); the message says which, in plain words.
    """

    from fastapi.responses import JSONResponse  # noqa: PLC0415

    from clio_agent.arc.lane_generations import LaneReplaceError  # noqa: PLC0415
    from clio_agent.gact.types import ErrorEnvelope, ErrorInfo  # noqa: PLC0415

    @app.exception_handler(TranscriptNotReadyError)
    async def _transcript_not_ready(_request: object, exc: TranscriptNotReadyError) -> JSONResponse:
        info = ErrorInfo(
            error=exc.error_type, message=exc.message, details=exc.details, recoverable=True
        )
        return JSONResponse(
            status_code=503,
            content=ErrorEnvelope(error=info).model_dump(exclude_none=True),
            headers={"Retry-After": "1"},
        )

    @app.exception_handler(LaneReplaceError)
    async def _transcript_replace_failed(_request: object, exc: LaneReplaceError) -> JSONResponse:
        if exc.committed:
            message = (
                "The conversation was changed, but clio-core could not remove its "
                "previous version yet; the next change removes it. Do not repeat the action."
            )
        else:
            message = (
                "The conversation could not be changed: clio-core did not store the new "
                "version, so it is exactly as it was. Retry shortly."
            )
        info = ErrorInfo(
            error=exc.error_type,
            message=message,
            details={**exc.details, "detail": exc.message},
            recoverable=not exc.committed,
        )
        return JSONResponse(
            status_code=500 if exc.committed else 503,
            content=ErrorEnvelope(error=info).model_dump(exclude_none=True),
            headers={} if exc.committed else {"Retry-After": "1"},
        )


def boot_transcript_store(app: "FastAPI", root: Path) -> None:
    """Wire the app's transcript store, index, metrics seed and resident set.

    Resolves ``transcript.file`` once (``app.state.transcript_file``). With it on,
    the sequence is the pre-switch one: the ``messages/`` store under ``root``, the
    restart reconciliation, the retention state, the metrics seed, the resident set.
    With it off there is no file store (``app.state.message_store`` is ``None``), the
    index is the session registry, and the reconciliation + seed read the atoms
    (now when the ARC is bound, else from :func:`on_process_arc_bound`).

    Raises:
        TranscriptFileRequiredError: Off, with no ARC bound, in History mode.
        ConfigError: ``transcript.file`` is not a boolean.
    """

    from clio_agent.gact.messages import MessageStore  # noqa: PLC0415
    from clio_agent.gact.metrics_counters import MetricsCounters  # noqa: PLC0415
    from clio_agent.gact.resident_ledgers import (  # noqa: PLC0415
        build_resident_ledger_set,
        seed_metrics_counters,
    )
    from clio_agent.gact.runtime.retention import init_retention_state  # noqa: PLC0415
    from clio_agent.gact.session_store import (  # noqa: PLC0415 - import cycle
        _reconcile_restart_interrupted_sessions,
    )
    from clio_agent.gact.transcript_projection import canonical_log_arc  # noqa: PLC0415

    file_on = resolve_transcript_file()
    if not file_on:
        _refuse_history_mode(app)
    app.state.transcript_file = file_on
    app.state.transcript_boot_ready = threading.Event()
    install_transcript_error_handler(app)
    # #1334 F2: placeholder for the reconciliation's _replace_session_messages write.
    app.state.messages = {}
    if file_on:
        # Durable per-session message log: per-session JSON ledgers so adapter
        # deletion/redeploy preserves transcripts.
        app.state.message_store = MessageStore(path=root / "messages")
        app.state.transcript_index = app.state.message_store
        _settle_interaction_from_files(app)
        _reconcile_restart_interrupted_sessions(app)
    else:
        app.state.message_store = None
        app.state.transcript_index = RegistryTranscriptIndex(app.state.sessions)
    # #770 C3: bounded eviction-audit trail (init before the resident set).
    init_retention_state(app)
    # #770 C3 / #889: running metrics aggregate, seeded by a streaming parse-and-
    # DISCARD walk so the metrics wire stays byte-identical across a restart WITHOUT
    # pinning every transcript in RAM.
    app.state.metrics_counters = MetricsCounters()
    if file_on:
        seed_metrics_counters(app.state.message_store, app.state.metrics_counters)
        app.state.transcript_boot_ready.set()
    elif canonical_log_arc(app) is not None:
        _atom_boot(app)
    # #889: BOUNDED (LRU + byte cap + idle-TTL) resident projection over the store --
    # boots empty (index only), materializes lazily. See gact.resident_ledgers.
    app.state.messages = build_resident_ledger_set(app)


__all__ = [
    "BOOT_FAILED_REASON",
    "LANE_BUSY_REASON",
    "STORE_NOT_ATTACHED_REASON",
    "STORE_UNAVAILABLE_REASON",
    "TRANSCRIPT_FILE_ENV",
    "TRANSCRIPT_FILE_KEY",
    "RegistryTranscriptIndex",
    "TranscriptFileRequiredError",
    "TranscriptNotReadyError",
    "boot_transcript_store",
    "file_transcript_enabled",
    "forget_unminted_on_failure",
    "has_durable_transcript_store",
    "install_transcript_error_handler",
    "load_durable_transcript",
    "materialize_from_atoms",
    "on_process_arc_bound",
    "reload_resident_on_failure",
    "resolve_transcript_file",
    "seeded_metrics_counters",
]
