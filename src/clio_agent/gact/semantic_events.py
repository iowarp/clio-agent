"""Research-grade semantic execution events for GACT.

Semantic events are higher-level than the existing wire events. A
``message.part.delta`` tells the TUI that text arrived; a semantic event
tells a researcher that CLIO started a turn, called a tool, delegated to
an expert, accessed memory, or settled a turn. The same event object feeds
live SSE, durable trace logging, and user hooks.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from clio_agent.gact.events import Event, EventBus

SCHEMA_VERSION = "clio.semantic_event.v1"
DEFAULT_DETAIL_LEVEL = "semantic"
DETAIL_LEVELS = {"off", "metadata", "semantic", "full_debug"}
REDACTED_VALUE = "[redacted]"
SENSITIVE_KEYS = {
    "api_key",
    "args",
    "arguments",
    "content",
    "input",
    "new_content",
    "output",
    "password",
    "prompt",
    "question",
    "raw",
    "rendered_context",
    "rendered_prompt",
    "rendered_system_prompt",
    "response",
    "result",
    "secret",
    "text",
    "token",
    "transcript",
    "user_input",
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_id() -> str:
    return f"sem_{uuid.uuid4().hex[:16]}"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, set):
        return sorted(_json_safe(v) for v in value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_safe(model_dump(exclude_none=True))
        except TypeError:
            return _json_safe(model_dump())
    return str(value)


def normalize_detail_level(value: str) -> str:
    normalized = (value or DEFAULT_DETAIL_LEVEL).strip().lower()
    return normalized if normalized in DETAIL_LEVELS else DEFAULT_DETAIL_LEVEL


def _redact_value(value: Any) -> Any:
    if value in (None, "", [], {}):
        return value
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return f"{REDACTED_VALUE}:{len(value)} chars"
    return REDACTED_VALUE


def _semantic_safe(value: Any) -> Any:
    value = _json_safe(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_s = str(key)
            if key_s.lower() in SENSITIVE_KEYS:
                result[key_s] = _redact_value(item)
            else:
                result[key_s] = _semantic_safe(item)
        return result
    if isinstance(value, list):
        return [_semantic_safe(item) for item in value]
    return value


def _payload_for_detail(value: dict[str, Any], detail_level: str) -> dict[str, Any]:
    detail_level = normalize_detail_level(detail_level)
    if detail_level in {"off", "metadata"}:
        return {}
    if detail_level == "semantic":
        return _semantic_safe(value)
    return _json_safe(value)


@dataclass
class SemanticEvent:
    """Single semantic observation from a CLIO run."""

    event_type: str
    session_id: str
    trace_id: str
    turn_id: str = ""
    workspace_id: str = ""
    span_id: str = field(default_factory=_event_id)
    parent_span_id: str = ""
    status: str = "completed"
    summary: str = ""
    actor: dict[str, Any] = field(default_factory=dict)
    subject: dict[str, Any] = field(default_factory=dict)
    blueprint: dict[str, Any] = field(default_factory=dict)
    provider: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    live_observed: bool = True
    detail_level: str = DEFAULT_DETAIL_LEVEL
    occurred_at: str = field(default_factory=_utcnow_iso)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        detail_level = normalize_detail_level(self.detail_level)
        return {
            "schema_version": self.schema_version,
            "event_id": self.span_id,
            "event_type": self.event_type,
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "trace_id": self.trace_id,
            "turn_id": self.turn_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "status": self.status,
            "summary": self.summary,
            "actor": _payload_for_detail(self.actor, detail_level),
            "subject": _payload_for_detail(self.subject, detail_level),
            "blueprint": _payload_for_detail(self.blueprint, detail_level),
            "provider": _payload_for_detail(self.provider, detail_level),
            "payload": _payload_for_detail(self.payload, detail_level),
            "live_observed": self.live_observed,
            "detail_level": detail_level,
            "occurred_at": self.occurred_at,
        }


class SemanticTraceBackend(Protocol):
    """Durable sink for semantic events."""

    name: str

    def emit(self, event: SemanticEvent) -> None:
        ...


class NoopSemanticTraceBackend:
    name = "none"

    def emit(self, event: SemanticEvent) -> None:
        return


class FileSemanticTraceBackend:
    """Append semantic events as JSONL.

    If ``path`` is a directory, events are split into
    ``<session_id>.semantic.jsonl`` files. If it is a file path, all
    events append to that file.
    """

    name = "file"

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _path_for(self, event: SemanticEvent) -> Path:
        if self.path.suffix:
            return self.path
        return self.path / f"{event.session_id}.semantic.jsonl"

    def emit(self, event: SemanticEvent) -> None:
        path = self._path_for(event)
        line = json.dumps(event.to_dict(), sort_keys=True)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.write("\n")


def build_trace_backend(default_root: Path) -> SemanticTraceBackend:
    """Build the configured durable trace backend.

    ``CLIO_SEMANTIC_TRACE_BACKEND`` accepts ``file``, ``factory``, or ``none``.
    ``CLIO_SEMANTIC_TRACE_PATH`` may point at either a JSONL file or a
    directory. Durable tracing is opt-in; live semantic SSE remains on
    by default.
    """

    backend = os.environ.get("CLIO_SEMANTIC_TRACE_BACKEND", "none").strip().lower()
    if backend in {"", "none", "off", "disabled"}:
        return NoopSemanticTraceBackend()
    if backend == "file":
        raw_path = os.environ.get("CLIO_SEMANTIC_TRACE_PATH", "").strip()
        path = Path(raw_path).expanduser() if raw_path else default_root
        return FileSemanticTraceBackend(path)
    if backend in {"factory", "python_factory", "custom"}:
        factory_path = os.environ.get("CLIO_SEMANTIC_TRACE_FACTORY", "").strip()
        if not factory_path:
            raise ValueError(
                "CLIO_SEMANTIC_TRACE_FACTORY is required when "
                "CLIO_SEMANTIC_TRACE_BACKEND=factory"
            )
        factory = _load_factory(factory_path)
        raw_config = os.environ.get("CLIO_SEMANTIC_TRACE_CONFIG", "").strip()
        config = json.loads(raw_config) if raw_config else {}
        result = factory(default_root=default_root, config=config)
        if not callable(getattr(result, "emit", None)):
            raise TypeError("semantic trace factory must return an object with emit(event)")
        if not getattr(result, "name", ""):
            result.name = "factory"
        return result
    raise ValueError(f"unsupported semantic trace backend: {backend}")


def _load_factory(path: str) -> Any:
    module_name, sep, attr = path.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError("factory path must be 'module.submodule:function'")
    import importlib

    module = importlib.import_module(module_name)
    factory = getattr(module, attr)
    if not callable(factory):
        raise TypeError(f"semantic trace factory is not callable: {path}")
    return factory


class SemanticEventSink:
    """Fan out semantic events to SSE, durable traces, and hooks."""

    def __init__(
        self,
        *,
        bus: EventBus,
        trace_backend: SemanticTraceBackend,
        detail_level: str = DEFAULT_DETAIL_LEVEL,
    ) -> None:
        self.bus = bus
        self.trace_backend = trace_backend
        self.detail_level = normalize_detail_level(detail_level)

    @property
    def trace_backend_name(self) -> str:
        return self.trace_backend.name

    def emit(self, event: SemanticEvent) -> dict[str, Any]:
        event.detail_level = normalize_detail_level(event.detail_level or self.detail_level)
        if event.detail_level == "off":
            return {}
        payload = event.to_dict()
        self.trace_backend.emit(event)
        self.bus.publish(
            Event(
                type="semantic.event",
                session_id=event.session_id,
                payload=payload,
            )
        )
        try:
            from clio_agent.runtime.hooks import fire as _fire_hook

            _fire_hook("semantic_event", payload)
        except Exception:
            # Semantic hooks are observability side-effects. They should
            # never mutate or crash the turn being observed.
            pass
        return payload
