"""Session message-ledger + context-file helpers (#714).

Behavior-preserving extraction from :mod:`clio_agent.gact.app`. This module owns
the cohesive cluster that persists a session's *conversation state* across the
two places it lives:

* **In-memory + durable message store** -- ``app.state.messages`` (hot copy) plus
  the write-through :class:`~clio_agent.gact.messages.MessageStore` on disk (absent
  with ``transcript.file`` off, :mod:`clio_agent.gact.transcript_file`) and the
  ``message_part`` atoms. The ``_append/_extend/_replace/_delete_session_messages``
  helpers keep them in lock step.
* **Context-file attachments** -- the ``app.state.context_files`` ledger keyed by
  session id, loaded from / flushed to ``app.state.context_files_path``.

It also exposes ``_release_session_arc`` (drop a closed session's hot ARC
footprint). Earlier turns reach the model through the agent's clio-core projection
(:mod:`clio_agent.gact.agents.clio_react_record`), never through the prompt.

The reader-less per-workspace session/message mirror was DELETED in #771 (zero
readers in ``src/`` or gact-tui; #737 direction is fewer materializations, not
more). ``resolve_workspace_storage_root`` still resolves the ``storage_root``
wire field in :mod:`clio_agent.gact.workspaces`; nothing is written under it.

The module imports only leaves (stdlib + :mod:`clio_agent.gact.messages` for the
durable store); it never imports :mod:`clio_agent.gact.app` at module top. ``app``
is always passed explicitly so handlers never close over ``build_app`` locals.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.platform_paths import atomic_write_text
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import Message

logger = logging.getLogger(__name__)
# ------------------------------------------------------------------------- #
# Session message ledger (in-memory + durable) #
# ------------------------------------------------------------------------- #


def _metrics_counters(app: "FastAPI") -> Any:
    """Return the running metrics aggregate, or ``None`` when not wired.

    #770 C3: the four message write seams below keep this aggregate current so
    ``GET /v1/metrics`` reads a running counter instead of re-walking history.
    """

    return getattr(app.state, "metrics_counters", None)


def _append_session_message(
    app: "FastAPI", session_id: str, message: "Message", *, atoms_minted: bool = False
) -> None:
    """Append one chronological message to memory and disk.

    ``atoms_minted=True`` (#1334): the caller persists the message's ARC atoms itself,
    off the loop thread (the turn's minter / off-loop setup); only the in-memory ledger
    and the local message store are written here. Explicit, never inferred.
    """

    app.state.messages.setdefault(session_id, []).append(message)
    counters = _metrics_counters(app)
    if counters is not None:
        counters.add_message(session_id, message)
    store = getattr(app.state, "message_store", None)
    if store is not None:
        store.append(session_id, message)
    # #737 S5: the single append-one persist seam mints the message's ``message_part``
    # atoms onto the canonical ARC log -- the transcript's source of truth (must-succeed,
    # §3.4). With ``transcript.file`` off they are the only durable copy, so a failed
    # mint takes the message back out of the in-memory ledger before the error propagates.
    from clio_agent.gact.transcript_file import (  # noqa: PLC0415 - lazy: keep leaf
        forget_unminted_on_failure,
    )
    from clio_agent.gact.transcript_projection import (  # noqa: PLC0415 - lazy: keep leaf
        on_message_appended,
    )

    with forget_unminted_on_failure(app, session_id, message):
        on_message_appended(app, session_id, message, atoms_minted=atoms_minted)
    from clio_agent.gact.storage.drafts import retain_message_sources

    retain_message_sources(app, message)


def _interrupted_assistant_row(
    app: "FastAPI", session_id: str, last_user_message: "Message"
) -> "Message":
    """Build the typed interrupted-turn row a crashed-mid-turn session gets.

    #1334 review round (Fable, F2): a synthetic empty row here would DESTROY a
    partial answer the crashed turn already streamed and sealed onto the atom
    lane (owner rule: deleting a vehicle keeps the feature — reload already
    surfaces that partial as a typed ``stop_reason="incomplete"`` message,
    ``metadata.transcript_incomplete`` named, via
    :func:`~clio_agent.gact.part_atoms.reproduce_message_wire`). When the lane
    holds that trailing incomplete assistant message, this reuses it VERBATIM
    (id/turn_id/created_at/parts/metadata — ``transcript_incomplete`` stays) and
    only overlays the restart's ``stop_reason``/``error_info``, so the streamed
    text survives. Only a session whose lane has no such message (the crash hit
    before the first part ever sealed) falls back to the empty synthetic row.
    """

    from clio_agent.gact.runtime.globals import _iso_from_epoch, _new_message_id  # noqa: PLC0415
    from clio_agent.gact.transcript_projection import (  # noqa: PLC0415 - lazy: keep leaf
        assemble_session_messages,
        has_atoms,
    )
    from clio_agent.gact.types import ErrorInfo  # noqa: PLC0415

    now = time.time()
    turn_id = last_user_message.turn_id or last_user_message.id
    error_info = ErrorInfo(
        error="server_restart_interrupted",
        message=(
            "The agent service restarted before this response completed. "
            "Your request was preserved and can be retried."
        ),
        details={
            "reason": "server_restart_interrupted",
            "session_id": session_id,
            "turn_id": turn_id,
        },
        recoverable=True,
    )

    arc = getattr(app.state, "arc", None)
    if arc is not None and getattr(arc, "_segments", None) is not None:
        try:
            lane_has_atoms = has_atoms(arc, session_id)
        except OSError:
            lane_has_atoms = False
        if lane_has_atoms:
            assembled = assemble_session_messages(arc, session_id)
            trailing = assembled[-1] if assembled else None
            if (
                trailing is not None
                and trailing.role == "assistant"
                and trailing.stop_reason == "incomplete"
            ):
                return trailing.model_copy(
                    update={"stop_reason": "error", "error_info": error_info}
                )

    from clio_agent.gact.types import Message  # noqa: PLC0415

    return Message(
        id=_new_message_id("asst"),
        turn_id=turn_id,
        session_id=session_id,
        role="assistant",
        created_at=_iso_from_epoch(now),
        updated_at=_iso_from_epoch(now),
        stop_reason="error",
        error_info=error_info,
    )


def _reconcile_restart_interrupted_sessions(app: "FastAPI") -> None:
    """Settle persisted running sessions whose process-local executor is gone.

    A user message is durable before its assistant turn starts. If the process
    exits mid-turn, the session registry can therefore retain ``running`` and an
    older message count even though no :class:`TurnRunner` task can survive the
    restart. Reconcile only those stale running rows, reading one ledger at a
    time so ordinary historical sessions remain lazily materialized. The ledger is
    the durable transcript: the ``messages/`` file, or the atoms with
    ``transcript.file`` off (:func:`~clio_agent.gact.transcript_file.load_durable_transcript`).
    """

    from clio_agent.gact.transcript_file import (  # noqa: PLC0415 - lazy: keep leaf
        has_durable_transcript_store,
        load_durable_transcript,
    )

    sessions = getattr(app.state, "sessions", None)
    if sessions is None or not has_durable_transcript_store(app):
        return
    for session in sessions.list():
        if session.status != "running":
            continue
        try:
            messages = load_durable_transcript(app, session.id) or []
        except OSError as exc:
            logger.error(
                "restart interruption reconciliation failed session=%s error=%r",
                session.id,
                exc,
            )
            continue
        durable_messages = list(messages)
        last_message = durable_messages[-1] if durable_messages else None
        if last_message is not None and last_message.role == "user":
            interrupted_message = _interrupted_assistant_row(app, session.id, last_message)
            durable_messages.append(interrupted_message)
            try:
                # #1334 review round (Fable, F2): through the sanctioned replace
                # seam (the same one undo/rewind uses) so the atom lane is
                # re-materialized to match -- file and lane agree by
                # construction and materialize_ledger's divergence repair never
                # fires on this session's first post-restart read. Reconciliation
                # runs at boot (no loop) or, with transcript.file off and the ARC
                # attached later, on the ARC construction worker -- so
                # on_ledger_replaced's mint runs inline, off any server loop thread.
                _replace_session_messages(app, session.id, durable_messages)
            except OSError as exc:
                logger.error(
                    "restart interruption boundary write failed session=%s error=%r",
                    session.id,
                    exc,
                )
                continue
        sessions.update(
            session.id,
            status="error",
            message_count=len(durable_messages),
            metadata_patch={
                "restart_interruption": {
                    "reason": "server_restart_interrupted",
                    "previous_status": "running",
                }
            },
        )
        trace.event(
            "SESSION",
            "restart_interrupted session=%s messages=%s",
            session.id,
            len(durable_messages),
        )


def _extend_session_messages(
    app: "FastAPI",
    session_id: str,
    messages: list["Message"],
) -> None:
    """Append several chronological messages to memory and disk."""

    if not messages:
        return
    app.state.messages.setdefault(session_id, []).extend(messages)
    counters = _metrics_counters(app)
    if counters is not None:
        counters.add_messages(session_id, messages)
    store = getattr(app.state, "message_store", None)
    if store is not None:
        store.extend(session_id, messages)
    # #737 S5: mirror the extend onto the canonical atom lane (no-op under legacy).
    from clio_agent.gact.transcript_projection import (  # noqa: PLC0415 - lazy: keep leaf
        on_messages_extended,
    )

    on_messages_extended(app, session_id, messages)


def _replace_session_messages(
    app: "FastAPI",
    session_id: str,
    messages: list["Message"],
    *,
    atoms_minted: bool = False,
) -> None:
    """Replace one session's message ledger in memory and disk.

    ``atoms_minted=True`` (#1334): the caller re-materializes the atom lane itself, off
    the loop thread; see :func:`_append_session_message`.
    """

    app.state.messages[session_id] = list(messages)
    counters = _metrics_counters(app)
    if counters is not None:
        counters.set_session(session_id, messages)
    store = getattr(app.state, "message_store", None)
    if store is not None:
        store.replace_session(session_id, list(messages))
    # #737 S5: re-materialize the atom lane to the replaced ledger (undo/rewind/fork/
    # compact/import). Transcript-projection-scoped only; ARC memory untouched. No-op
    # under legacy.
    from clio_agent.gact.transcript_projection import (  # noqa: PLC0415 - lazy: keep leaf
        on_ledger_replaced,
    )

    if not atoms_minted:
        on_ledger_replaced(app, session_id, list(messages))


def _delete_session_messages(app: "FastAPI", session_id: str) -> None:
    """Remove one session's message ledger from clio-core, memory and disk.

    clio-core goes FIRST (#737 S5: the canonical atom lane and its op lane; ARC memory
    is untouched, ``gact_visible_transcript_only``). When it cannot finish, the typed
    :class:`~clio_agent.gact.transcript_file.TranscriptEraseError` propagates before
    anything else is removed: the caller keeps the session and a retry finishes the
    erase -- the transcript is never left behind in clio-core silently.

    Then the resident set's non-materializing :meth:`~clio_agent.gact.resident_ledgers.ResidentLedgerSet.discard`
    when available, so deleting an EVICTED session does not rehydrate its (possibly
    huge) ledger from disk just to drop it — which would also emit a misleading
    ``rehydrate`` audit row and transiently count the doomed ledger against the byte
    cap, evicting warm sessions to make room for one deleted immediately after. Falls
    back to ``pop`` for a plain-dict ``app.state.messages`` (older/test wiring).

    Raises:
        TranscriptEraseError: clio-core did not finish erasing the transcript.
    """

    from clio_agent.gact.transcript_projection import (  # noqa: PLC0415 - lazy: keep leaf
        on_ledger_deleted,
    )

    on_ledger_deleted(app, session_id)
    messages = app.state.messages
    discard = getattr(messages, "discard", None)
    if callable(discard):
        discard(session_id)
    else:
        messages.pop(session_id, None)
    counters = _metrics_counters(app)
    if counters is not None:
        counters.remove_session(session_id)
    store = getattr(app.state, "message_store", None)
    if store is not None:
        store.delete_session(session_id)


def _release_session_arc(app: "FastAPI", session_id: str) -> None:
    """Release a closed session's hot footprint from ARC (best-effort).

    Persistence is write-through, so this only drops the in-memory cache/index
    copies; it never deletes durable records. Keeps an idle server from pinning
    every closed session's objects in the never-evicted hot path.
    """

    arc = getattr(app.state, "arc", None)
    if arc is None:
        return
    release = getattr(arc, "release_session", None)
    if release is None:
        return
    try:
        release(session_id)
    except Exception as exc:  # noqa: BLE001 - lifecycle cleanup must never fail a request
        trace.event(
            "SESSION-ARC",
            "release_session(%s) failed (best-effort): %s",
            session_id,
            exc,
        )


# ------------------------------------------------------------------------- #
# Context-file attachments ledger #
# ------------------------------------------------------------------------- #


def _load_context_files(path: Path | None) -> dict[str, dict[str, dict[str, Any]]]:
    """Load persisted context-file attachments keyed by session id."""

    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable session store yields empty state
        return {}
    sessions = data.get("sessions", {}) if isinstance(data, Mapping) else {}
    if not isinstance(sessions, Mapping):
        return {}
    loaded: dict[str, dict[str, dict[str, Any]]] = {}
    for sid, rows in sessions.items():
        if not isinstance(rows, Mapping):
            continue
        bucket: dict[str, dict[str, Any]] = {}
        for path_key, row in rows.items():
            if not isinstance(row, Mapping):
                continue
            path_value = str(row.get("path") or path_key or "").strip()
            if not path_value:
                continue
            bucket[path_value] = dict(row) | {"path": path_value}
        if bucket:
            loaded[str(sid)] = bucket
    return loaded


def _flush_context_files(app: "FastAPI") -> None:
    """Persist the current context-file ledger, if persistence is configured."""

    path = getattr(app.state, "context_files_path", None)
    if path is None:
        return
    # Staged, fsynced, then replaced: a crash can't leave a partial ledger.
    atomic_write_text(
        Path(path), json.dumps({"sessions": app.state.context_files}, indent=2, sort_keys=True)
    )


def _inherit_session_context_files(
    app: "FastAPI", parent_session_id: str, child_session_id: str
) -> None:
    """Copy a parent's immutable attachment ledger into a new child session."""

    parent_files = app.state.context_files.get(parent_session_id, {}) or {}
    if not parent_files:
        return
    app.state.context_files[child_session_id] = {
        str(path): dict(row) for path, row in parent_files.items()
    }
    _flush_context_files(app)


def _delete_session_context_files(app: "FastAPI", session_id: str) -> None:
    """Remove one session's context-file ledger from memory and disk."""

    if session_id in app.state.context_files:
        app.state.context_files.pop(session_id, None)
        _flush_context_files(app)
