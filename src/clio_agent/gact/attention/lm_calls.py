"""Find the ``lm.call`` records behind a transcript turn.

``lm.call`` is CLIO's durable per-call record (messages, output, response id,
attention declaration). It is read from whichever configured provenance store
holds it, in this order:

1. the native JSONL journal (always carries the full payload);
2. Flowcept ``ai_model_invocation`` tasks (payload present unless the Flowcept
   privacy mode is ``metadata``; the response id and attention declaration are
   metadata and survive every mode).

No store -> ``lm_call_store_unavailable``; it is never guessed from elsewhere.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LmCall:
    """One recorded model call, normalized across stores."""

    event_id: str
    session_id: str
    turn_id: str
    occurred_at: str
    model: str
    response_id: str
    messages: list[dict[str, Any]] | None
    content: str | None
    declaration: dict[str, Any] | None
    source: str
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def has_payload(self) -> bool:
        """Whether the input messages and output text were recorded."""
        return self.messages is not None and self.content is not None


def _from_payload(
    *,
    event_id: str,
    session_id: str,
    turn_id: str,
    occurred_at: str,
    payload: dict[str, Any] | None,
    metadata: dict[str, Any],
    source: str,
) -> LmCall:
    payload = payload if isinstance(payload, dict) else None
    messages = payload.get("messages") if payload else None
    content = payload.get("content") if payload else None
    declaration = (payload or {}).get("attention") or metadata.get("attention")
    return LmCall(
        event_id=event_id,
        session_id=session_id,
        turn_id=turn_id,
        occurred_at=occurred_at,
        model=str((payload or {}).get("model") or metadata.get("model") or ""),
        response_id=str((payload or {}).get("response_id") or metadata.get("response_id") or ""),
        messages=messages if isinstance(messages, list) else None,
        content=str(content) if isinstance(content, str) else None,
        declaration=declaration if isinstance(declaration, dict) else None,
        source=source,
    )


def lm_calls_from_jsonl(path: Path, session_id: str) -> list[LmCall]:
    """``lm.call`` events of one session from a native JSONL journal file."""
    if not path.is_file():
        return []
    calls: list[LmCall] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event_type") != "lm.call" or event.get("session_id") != session_id:
            continue
        calls.append(
            _from_payload(
                event_id=str(event.get("event_id") or ""),
                session_id=session_id,
                turn_id=str(event.get("turn_id") or ""),
                occurred_at=str(event.get("occurred_at") or ""),
                payload=event.get("payload"),
                metadata={},
                source="native",
            )
        )
    return calls


def lm_calls_from_flowcept_rows(rows: list[dict[str, Any]], session_id: str) -> list[LmCall]:
    """Normalize Flowcept ``ai_model_invocation`` task rows for one session."""
    calls: list[LmCall] = []
    for row in rows:
        clio = (row.get("custom_metadata") or {}).get("clio") or {}
        if clio.get("event_type") != "lm.call" or clio.get("session_id") != session_id:
            continue
        calls.append(
            _from_payload(
                event_id=str(clio.get("event_id") or row.get("task_id") or ""),
                session_id=session_id,
                turn_id=str(clio.get("turn_id") or ""),
                occurred_at=str(row.get("started_at") or ""),
                payload=clio.get("payload"),
                metadata=clio,
                source="flowcept",
            )
        )
    return calls


def flowcept_lm_call_filter(session_id: str) -> dict[str, Any]:
    """Flowcept query for one session's ``lm.call`` tasks."""
    return {
        "subtype": "ai_model_invocation",
        "custom_metadata.clio.session_id": session_id,
    }


def turn_calls(calls: list[LmCall], turn_id: str) -> list[LmCall]:
    """Calls of one turn, in recorded order."""
    return sorted(
        (c for c in calls if c.turn_id == turn_id), key=lambda c: (c.occurred_at, c.event_id)
    )
