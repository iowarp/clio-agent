"""Transcript-backed A2UI persistence and publication facade."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Mapping

from clio_agent.gact.a2ui import (
    A2UISurfaceRecord,
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


class A2UIStore:
    """Compatibility facade over the transcript-owned A2UI projection."""

    def __init__(self, *, app: "FastAPI", bus: EventBus) -> None:
        self._app = app
        self._bus = bus

    def _parts(self, session_id: str) -> list[Any]:
        parts: list[Any] = []
        seen: set[str] = set()
        for message in self._app.state.messages.get(session_id, []) or []:
            for part in getattr(message, "parts", []) or []:
                part_id = str(getattr(part, "id", "") or "")
                if part_id and part_id in seen:
                    continue
                if part_id:
                    seen.add(part_id)
                parts.append(part)
        for part in (getattr(self._app.state, "live_assistant_parts", {}) or {}).get(
            session_id, []
        ):
            part_id = str(getattr(part, "id", "") or "")
            if part_id and part_id in seen:
                continue
            if part_id:
                seen.add(part_id)
            parts.append(part)
        return parts

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

        rows = list(self._project(session_id)[0].values())
        rows.sort(key=lambda row: row.created_at)
        return [row.to_wire() for row in rows]

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

        from uuid import uuid4  # noqa: PLC0415

        from clio_agent.gact.parts import Part  # noqa: PLC0415

        current, _ = self._project(session_id)
        persisted_part_id = part_id or f"a2ui_{uuid4().hex}"
        timestamp = utcnow_iso()
        _, applied = apply_batch(
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
            raise A2UIValidationError("A2UI transcript is frozen; no surface state was persisted")
        for operation, surface_id, surface in applied:
            event_type = (
                "a2ui.surface.deleted" if operation == "deleteSurface" else "a2ui.surface.upserted"
            )
            payload = (
                {"surface_id": surface_id} if operation == "deleteSurface" else surface.to_wire()
            )
            self._bus.publish(Event(type=event_type, session_id=session_id, payload=payload))
        return [surface for _, _, surface in applied]


__all__ = ["A2UIStore"]
