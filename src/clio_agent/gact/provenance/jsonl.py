"""Native append-only JSONL provenance provider."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from clio_agent.gact.provenance.normalization import normalize_semantic_events
from clio_agent.gact.provenance.protocol import ProviderReceipt
from clio_agent.gact.semantic_events import FileSemanticTraceBackend, SemanticEvent


class JsonlProvenanceProvider:
    """Preserve the existing trace JSONL bytes behind the provider contract."""

    name = "jsonl"
    durable = True
    queryable = True

    def __init__(self, path: Path) -> None:
        self.path = path
        self._backend = FileSemanticTraceBackend(path)

    def emit(self, event: SemanticEvent) -> ProviderReceipt:
        """Append the event using the established off-loop writer."""
        self._backend.emit(event)
        return ProviderReceipt.ACCEPTED

    def close(self) -> None:
        """Drain pending JSONL writes."""
        self._backend.close()

    def query_execution(
        self,
        *,
        session_id: str,
        child_session_ids: list[str],
        limit: int,
    ) -> dict[str, Any]:
        """Read released native history from the append-only journal."""
        self._backend.flush()
        session_ids = {session_id, *child_session_ids}
        events: list[dict[str, Any]] = []
        if self.path.suffix.lower() in FileSemanticTraceBackend._FILE_SUFFIXES:
            paths = [self.path]
        else:
            paths = [self.path / f"{sid}.semantic.jsonl" for sid in sorted(session_ids)]
        for path in paths:
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(event.get("session_id") or "") in session_ids:
                    events.append(event)
        return normalize_semantic_events(
            events,
            provider="native",
            session_id=session_id,
            limit=limit,
        )
