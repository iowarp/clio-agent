"""Recover the visible transcript projection from the immutable ARC trace."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.arc.replay import reconstruct_arc_segments
from clio_agent.arc.schema import Segment
from clio_agent.gact.part_atoms import MESSAGE_PART_SCOPE

if TYPE_CHECKING:
    from fastapi import FastAPI

_TRACE_FILE_SUFFIXES = frozenset({".jsonl", ".json", ".ndjson", ".log"})


class TranscriptTraceReadError(RuntimeError):
    """The configured immutable trace could not be read exactly."""


def _session_trace_paths(app: "FastAPI", session_id: str) -> list[Path]:
    backend = getattr(app.state, "semantic_trace_backend", None)
    roots = getattr(backend, "replay_paths", ())
    paths: list[Path] = []
    for raw_root in roots:
        root = raw_root if isinstance(raw_root, Path) else Path(str(raw_root))
        candidate = (
            root
            if root.suffix.lower() in _TRACE_FILE_SUFFIXES
            else root / (f"{session_id}.semantic.jsonl")
        )
        if candidate not in paths:
            paths.append(candidate)
    return paths


def load_message_part_segments_from_trace(app: "FastAPI", session_id: str) -> list[Segment] | None:
    """Replay live message atoms, or return ``None`` when replay is not configured."""

    backend = getattr(app.state, "semantic_trace_backend", None)
    flush = getattr(backend, "flush", None)
    if callable(flush):
        flush()

    paths = _session_trace_paths(app, session_id)
    if not paths:
        return None

    events: list[dict[str, Any]] = []
    seen_event_ids: set[str] = set()
    for path in paths:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise TranscriptTraceReadError(f"cannot read transcript trace {path}: {exc}") from exc
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TranscriptTraceReadError(
                    f"invalid transcript trace JSON at {path}:{line_number}: {exc.msg}"
                ) from exc
            if str(event.get("session_id") or "") != session_id:
                continue
            payload = event.get("payload") or {}
            if event.get("event_type") != "arc.op" or payload.get("scope") != MESSAGE_PART_SCOPE:
                continue
            event_id = str(event.get("event_id") or "")
            if event_id and event_id in seen_event_ids:
                continue
            if event_id:
                seen_event_ids.add(event_id)
            events.append(event)
    return reconstruct_arc_segments(events, scope_filter=MESSAGE_PART_SCOPE)
