"""Transcript-backed A2UI persistence and publication facade."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock, RLock
from typing import TYPE_CHECKING, Any, Callable, Mapping

from clio_agent.gact.a2ui import (
    A2UISurfaceRecord,
    A2UITranscriptFrozenError,
    A2UIValidationError,
    apply_batch,
    project_a2ui_parts,
    utcnow_iso,
)
from clio_agent.gact.events import Event, EventBus
from clio_agent.gact.protocol.constants import A2UI_V091

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.parts import Part

_PersistPart = Callable[["Part"], bool]


@dataclass(frozen=True)
class A2UIBatchOutcome:
    """Server truth about one applied batch, read under the session lock.

    ``created_surface_ids`` is the fold's own verdict — the surfaces this batch
    brought into existence — not a caller's belief about what it sent. A
    producer that picks its own operation (``create_a2ui_surface`` emits
    ``createSurface`` only when the id is absent or deleted) cannot otherwise
    tell a brand-new surface from a revision, because both end on the same
    revision/state shape.

    Attributes:
        surfaces: One record per applied message, in batch order.
        created_surface_ids: Ids this batch created, in creation order.
        session_surface_ids: Every live (non-deleted) surface id in the session
            after the batch, oldest first.
    """

    surfaces: list[A2UISurfaceRecord]
    created_surface_ids: tuple[str, ...]
    session_surface_ids: tuple[str, ...]


def _live_surface_ids(surfaces: Mapping[tuple[str, str], A2UISurfaceRecord]) -> tuple[str, ...]:
    """Return the projection's non-deleted surface ids, oldest first.

    Mirrors :meth:`A2UIStore.list_wire`'s ordering so a producer's registry
    view and a client's snapshot enumerate the same session in the same order.

    Args:
        surfaces: A folded projection keyed by ``(session_id, surface_id)``.

    Returns:
        The live surface ids ordered by creation time.
    """

    rows = sorted(surfaces.values(), key=lambda row: row.created_at)
    return tuple(row.id for row in rows if row.state != "deleted")


class A2UIStore:
    """Compatibility facade over the transcript-owned A2UI projection."""

    def __init__(self, *, app: "FastAPI", bus: EventBus) -> None:
        self._app = app
        self._bus = bus
        self._session_locks: dict[str, RLock] = {}
        self._session_locks_guard = Lock()

    def _session_lock(self, session_id: str) -> RLock:
        """Return the per-session lock serializing this store's producers.

        The HTTP routes run on the event loop while ``create_a2ui_surface`` runs
        on the turn executor thread, so the read-project -> validate -> persist
        sequence needs a real lock to stay atomic between them.

        Args:
            session_id: Session whose A2UI state is being folded.

        Returns:
            The reentrant lock owned by that session.
        """

        with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = RLock()
                self._session_locks[session_id] = lock
            return lock

    def _all_part_ids(self, session_id: str) -> set[str]:
        """Return every part id already recorded for ``session_id``."""

        ids: set[str] = set()
        for message in self._app.state.messages.get(session_id, []) or []:
            for part in getattr(message, "parts", []) or []:
                ids.add(str(getattr(part, "id", "") or ""))
        for part in (getattr(self._app.state, "live_assistant_parts", {}) or {}).get(
            session_id, []
        ):
            ids.add(str(getattr(part, "id", "") or ""))
        ids.discard("")
        return ids

    def _parts(self, session_id: str) -> list[Any]:
        """Return this session's A2UI parts in causal (recorded) order.

        The persisted ledger and the in-flight live parts are two views of one
        turn: a tool-produced surface lives only in ``live_assistant_parts``
        until the turn finalizes, so folding every persisted part first would
        order an HTTP part written mid-turn *before* the createSurface it
        depends on. Every A2UI part carries the ``recorded_at`` stamp minted in
        :meth:`apply_batch`, so ordering by it keeps the fold causal regardless
        of which writer landed first.

        Args:
            session_id: Session to project.

        Returns:
            The session's deduplicated A2UI parts, oldest first.
        """

        candidates: list[Any] = []
        for message in self._app.state.messages.get(session_id, []) or []:
            candidates.extend(getattr(message, "parts", []) or [])
        candidates.extend(
            (getattr(self._app.state, "live_assistant_parts", {}) or {}).get(session_id, [])
        )
        stamped: list[tuple[str, int, Any]] = []
        seen: set[str] = set()
        carried = ""
        for part in candidates:
            if getattr(part, "type", "") != "a2ui":
                continue
            part_id = str(getattr(part, "id", "") or "")
            if part_id and part_id in seen:
                continue
            if part_id:
                seen.add(part_id)
            # An unstamped part (a legacy or hand-built record) inherits its
            # predecessor's stamp so the stable sort leaves it where it arrived.
            carried = str((getattr(part, "metadata", None) or {}).get("recorded_at") or carried)
            stamped.append((carried, len(stamped), part))
        stamped.sort(key=lambda row: (row[0], row[1]))
        return [row[2] for row in stamped]

    def _project(
        self, session_id: str
    ) -> tuple[dict[tuple[str, str], A2UISurfaceRecord], list[dict[str, str]]]:
        return project_a2ui_parts(self._parts(session_id), session_id)

    @property
    def load_degradation(self) -> dict[str, str] | None:
        """Return the typed notice for a superseded pre-release sidecar."""

        value = getattr(self._app.state, "a2ui_ledger_degradation", None)
        return dict(value) if isinstance(value, Mapping) else None

    def projection_degradations(self, session_id: str) -> list[dict[str, str]]:
        """Return typed quarantine reasons for unreadable persisted parts."""

        return self._project(session_id)[1]

    def get(self, session_id: str, surface_id: str) -> A2UISurfaceRecord | None:
        """Return a session-scoped surface derived from transcript parts."""

        return self._project(session_id)[0].get((session_id, surface_id))

    def list_wire(self, session_id: str) -> list[dict[str, Any]]:
        """Return transcript-derived surfaces ordered by creation time."""

        return self.list_wire_with_degradations(session_id)[0]

    def list_wire_with_degradations(
        self, session_id: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Return the surfaces AND why any persisted part was quarantined.

        One projection pass for both. A caller that wanted the rows and the
        reasons had to call :meth:`list_wire` and
        :meth:`projection_degradations` separately, walking the session's whole
        message ledger twice — and the pending-interaction projection, which
        needs both, took the first walk and dropped the reasons on the floor.
        """

        surfaces, degradations = self._project(session_id)
        rows = sorted(surfaces.values(), key=lambda row: row.created_at)
        return [row.to_wire() for row in rows], list(degradations)

    def announce_ledger_clear(self, session_id: str, reason: str) -> list[str]:
        """Publish a typed lifecycle deletion for every surface a wipe removes.

        A2UI state is transcript-owned, so clearing a session's ledger destroys
        its surfaces. Callers that intend that destruction (plan-exit
        ``clear_context``) announce it here so a connected client stops
        rendering a surface the server no longer has, with the reason carried on
        the event instead of the surface silently disappearing on reconnect.

        Args:
            session_id: Session whose ledger is about to be replaced.
            reason: Typed reason recorded on each published deletion.

        Returns:
            The ids of the surfaces announced as deleted, in creation order.
        """

        announced: list[str] = []
        with self._session_lock(session_id):
            for record in sorted(self._project(session_id)[0].values(), key=lambda r: r.created_at):
                if record.state == "deleted":
                    continue
                announced.append(record.id)
                self._bus.publish(
                    Event(
                        type="a2ui.surface.deleted",
                        session_id=session_id,
                        payload={"surface_id": record.id, "reason": reason},
                    )
                )
        return announced

    def _persist_part(self, session_id: str, part: "Part") -> bool:
        from clio_agent.gact.session_store import _append_session_message  # noqa: PLC0415
        from clio_agent.gact.types import Message  # noqa: PLC0415

        now = utcnow_iso()
        message = Message(
            id=f"msg_a2ui_{part.id}",
            session_id=session_id,
            role="assistant",
            created_at=now,
            updated_at=now,
            parts=[part],
            metadata={"a2ui_protocol_version": A2UI_V091},
        )
        _append_session_message(self._app, session_id, message)
        return True

    def apply(
        self,
        session_id: str,
        message: Mapping[str, Any],
        **kwargs: Any,
    ) -> A2UISurfaceRecord:
        """Persist and publish one ordered server message."""

        return self.apply_batch(session_id, [message], **kwargs)[0]

    def apply_batch(
        self,
        session_id: str,
        messages: list[Mapping[str, Any]],
        *,
        run_id: str = "",
        message_id: str = "",
        part_id: str = "",
        persist_part: _PersistPart | None = None,
    ) -> list[A2UISurfaceRecord]:
        """Atomically validate, append one transcript part, and publish a batch."""

        return self.apply_batch_outcome(
            session_id,
            messages,
            run_id=run_id,
            message_id=message_id,
            part_id=part_id,
            persist_part=persist_part,
        ).surfaces

    def apply_batch_outcome(
        self,
        session_id: str,
        messages: list[Mapping[str, Any]],
        *,
        run_id: str = "",
        message_id: str = "",
        part_id: str = "",
        persist_part: _PersistPart | None = None,
    ) -> A2UIBatchOutcome:
        """Apply a batch and report what it created plus the session's registry.

        Args:
            session_id: Session the batch belongs to.
            messages: The ordered official server messages.
            run_id: Correlated run id recorded on a created surface.
            message_id: Correlated message id recorded on a created surface.
            part_id: Transcript part id; minted when empty.
            persist_part: Writer for the transcript part; the session message
                log is used when omitted.

        Returns:
            The applied records plus the created/live-registry truth, all read
            inside the session lock that made the fold atomic.

        Raises:
            A2UIValidationError: If any message is rejected, or the correlation
                part id is already recorded in this session.
            A2UITranscriptFrozenError: If the ledger can no longer accept the
                part; nothing is persisted or published.
        """

        from uuid import uuid4  # noqa: PLC0415

        from clio_agent.gact.parts import Part  # noqa: PLC0415

        with self._session_lock(session_id):
            persisted_part_id = part_id or f"a2ui_{uuid4().hex}"
            if persisted_part_id in self._all_part_ids(session_id):
                # A reused id would be dropped by the projection's part dedupe,
                # silently discarding this batch after a 200. Refuse it instead.
                raise A2UIValidationError(
                    f"A2UI correlation part_id is already recorded in this session: "
                    f"{persisted_part_id}"
                )
            current, _ = self._project(session_id)
            timestamp = utcnow_iso()
            folded, applied = apply_batch(
                current,
                session_id,
                messages,
                run_id=run_id,
                message_id=message_id,
                part_id=persisted_part_id,
                observed_at=timestamp,
            )
            first_surface = applied[0][1]
            existing = current.get((session_id, first_surface))
            part = Part(
                id=persisted_part_id,
                type="a2ui",
                surface_id=first_surface,
                a2ui_protocol_version=A2UI_V091,
                a2ui_messages=[dict(message) for message in messages],
                metadata={
                    "recorded_at": timestamp,
                    "run_id": run_id,
                    "message_id": message_id,
                    "projection_only": bool(existing is not None and existing.state != "deleted"),
                },
            )
            writer = persist_part or (lambda candidate: self._persist_part(session_id, candidate))
            if not writer(part):
                raise A2UITranscriptFrozenError(
                    "A2UI transcript is frozen; no surface state was persisted"
                )
            for operation, surface_id, surface in applied:
                event_type = (
                    "a2ui.surface.deleted"
                    if operation == "deleteSurface"
                    else "a2ui.surface.upserted"
                )
                payload = (
                    {"surface_id": surface_id}
                    if operation == "deleteSurface"
                    else surface.to_wire()
                )
                self._bus.publish(Event(type=event_type, session_id=session_id, payload=payload))
            return A2UIBatchOutcome(
                surfaces=[surface for _, _, surface in applied],
                created_surface_ids=tuple(
                    surface_id
                    for operation, surface_id, _ in applied
                    if operation == "createSurface"
                ),
                session_surface_ids=_live_surface_ids(folded),
            )


__all__ = ["A2UIBatchOutcome", "A2UIStore"]
