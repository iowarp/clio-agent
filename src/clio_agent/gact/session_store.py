"""Session message-ledger + context-file helpers (#714).

Behavior-preserving extraction from :mod:`clio_agent.gact.app`. This module owns
the cohesive cluster that persists a session's *conversation state* across the
two places it lives:

* **In-memory + durable message store** -- ``app.state.messages`` (hot copy) plus
  the write-through :class:`~clio_agent.gact.messages.MessageStore` on disk. The
  ``_append/_extend/_replace/_delete_session_messages`` helpers keep both in lock
  step.
* **Context-file attachments** -- the ``app.state.context_files`` ledger keyed by
  session id, loaded from / flushed to ``app.state.context_files_path``.

It also exposes ``_release_session_arc`` (drop a closed session's hot ARC
footprint) and ``_compile_session_conversation_history`` (prepend a compact
transcript of prior turns to the current prompt for multi-turn continuity).

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
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

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


def _append_session_message(app: "FastAPI", session_id: str, message: "Message") -> None:
    """Append one chronological message to memory and disk."""

    app.state.messages.setdefault(session_id, []).append(message)
    counters = _metrics_counters(app)
    if counters is not None:
        counters.add_message(session_id, message)
    store = getattr(app.state, "message_store", None)
    if store is not None:
        store.append(session_id, message)


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


def _replace_session_messages(
    app: "FastAPI",
    session_id: str,
    messages: list["Message"],
) -> None:
    """Replace one session's message ledger in memory and disk."""

    app.state.messages[session_id] = list(messages)
    counters = _metrics_counters(app)
    if counters is not None:
        counters.set_session(session_id, messages)
    store = getattr(app.state, "message_store", None)
    if store is not None:
        store.replace_session(session_id, list(messages))


def _delete_session_messages(app: "FastAPI", session_id: str) -> None:
    """Remove one session's message ledger from memory and disk."""

    app.state.messages.pop(session_id, None)
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
# Multi-turn conversation continuity #
# ------------------------------------------------------------------------- #


def _compile_session_conversation_history(
    app: "FastAPI", session_id: str, current_prompt: str
) -> str:
    """Prepend a compact transcript of THIS session's prior turns to the turn's
    prompt so a multi-turn orchestrator can reuse what earlier turns already
    established (the resolved region, ranked stations, staged file paths) instead of
    restarting blind on a follow-up like "now plot it". General to any blueprint and
    a NO-OP on the first turn (no prior messages), so single-turn behaviour is
    unchanged. The orchestrator otherwise receives only the latest user message."""
    messages = list(app.state.messages.get(session_id, []))
    prior = [m for m in messages if getattr(m, "role", "") in {"user", "assistant"}]
    # The current user message is already appended before the turn runs — drop the
    # trailing user message(s) so only PRIOR turns are carried.
    while prior and prior[-1].role == "user":
        prior.pop()
    if not prior:
        return current_prompt
    lines: list[str] = []
    for message in prior:
        # Carry the FULL prior message text verbatim — clio must not heuristically
        # truncate content the orchestrator sees; only an LLM may reduce content.
        text = "\n".join(
            part.text.strip()
            for part in message.parts
            if part.type in {"text", "thinking", "error"} and part.text.strip()
        ).strip()
        if not text:
            continue
        speaker = "User" if message.role == "user" else "Assistant"
        lines.append(f"{speaker}: {text}")
    if not lines:
        return current_prompt
    transcript = "\n".join(lines)
    return (
        "Earlier turns in THIS conversation — reuse what was already resolved "
        "(region/coordinates, ranked stations, staged file paths) rather than "
        "starting over; only the request after the marker is new:\n"
        f"{transcript}\n\n=== Current request ===\n{current_prompt}"
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
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # write+fsync to a temp file, then atomic rename: fsync forces the bytes to
    # disk before the rename publishes them, so a crash can't leave a partial
    # ledger behind.
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"sessions": app.state.context_files}, indent=2, sort_keys=True))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _delete_session_context_files(app: "FastAPI", session_id: str) -> None:
    """Remove one session's context-file ledger from memory and disk."""

    if session_id in app.state.context_files:
        app.state.context_files.pop(session_id, None)
        _flush_context_files(app)
