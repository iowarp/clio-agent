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
from collections.abc import Callable
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
    "reasoning",
    "reasoning_content",
    "rendered_context",
    "rendered_prompt",
    "rendered_system_prompt",
    "response",
    "result",
    "secret",
    "text",
    "token",
    "trajectory",
    "transcript",
    "user_input",
    # Full final assistant message embedded in turn.completed for the durable
    # trace (so the messages store is derivable from the trace); stripped from
    # the SSE projection where the message already streams via message.* events.
    "final_message",
}

# The "body" fields of a SemanticEvent — these carry the rich, potentially
# sensitive payloads that are captured in FULL durably but projected/redacted
# for SSE and hooks. The envelope fields (ids, status, summary, timestamps) are
# never redacted.
_BODY_FIELDS = ("actor", "subject", "blueprint", "provider", "payload")


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

    def to_dict(self, projection: str = "full") -> dict[str, Any]:
        """Serialize the event at the requested projection.

        ``full`` (default) — every body field unredacted; this is what the
        DURABLE canonical trace and live consumers (ARC) receive. ``sse`` —
        body fields redacted per the event's ``detail_level`` (the only
        surviving redaction path; for SSE/UI). ``metadata``/``off`` — body
        fields emptied. The envelope (ids/status/summary/timestamps) is never
        redacted at any projection.
        """
        detail_level = normalize_detail_level(self.detail_level)
        envelope = {
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
            "live_observed": self.live_observed,
            "detail_level": detail_level,
            "occurred_at": self.occurred_at,
        }
        if projection == "full":
            bodies = {
                field_name: _json_safe(getattr(self, field_name)) for field_name in _BODY_FIELDS
            }
        elif projection in ("metadata", "off", "none"):
            bodies = {field_name: {} for field_name in _BODY_FIELDS}
        else:  # "sse" (and any unknown) → honor the event's detail_level
            bodies = {
                field_name: _payload_for_detail(getattr(self, field_name), detail_level)
                for field_name in _BODY_FIELDS
            }
        return {**envelope, **bodies}


# --- Projection registry -----------------------------------------------------
# One canonical event is captured at MAX fidelity; each consumer gets a
# projection. The durable trace + live consumers (ARC) take ``project_full``;
# SSE/hooks take a redacted view; handoff/history/research are explicit views.


def project_full(event: SemanticEvent) -> dict[str, Any]:
    """Unredacted view — durable canonical trace, live consumers, research."""
    return event.to_dict("full")


def project_sse(event: SemanticEvent) -> dict[str, Any]:
    """Redacted view honoring detail_level — for the live SSE/UI stream."""
    return event.to_dict("sse")


def project_hook(event: SemanticEvent, *, full: bool = False) -> dict[str, Any]:
    """View handed to user hooks. Redacted by default (hooks are user code)."""
    return event.to_dict("full" if full else "sse")


def project_research(event: SemanticEvent) -> dict[str, Any]:
    """Full view for research consumers (IO-prefetch, error detection)."""
    return event.to_dict("full")


def project_handoff(event: SemanticEvent, mode: str = "FINAL") -> dict[str, Any]:
    """Expert→parent handoff view (wired at the handoff seam in a later stage).

    ``FINAL`` keeps the answer + tool evidence + workflow_state but strips the
    heavy reasoning/trajectory; ``SUMMARY`` keeps only the answer. Reduction is
    a projection of the full event, never a capture-time loss.
    """
    full = event.to_dict("full")
    payload = dict(full.get("payload") or {})
    if mode.upper() == "SUMMARY":
        keep = {"answer", "result_summary"}
    else:  # FINAL
        keep = {"answer", "result_summary", "tools_called", "workflow_state", "evidence"}
    full["payload"] = {k: v for k, v in payload.items() if k in keep}
    return full


def project_history(event: SemanticEvent) -> dict[str, Any]:
    """dspy.History / KV-rehydration view — DEFERRED (see GitHub issue)."""
    raise NotImplementedError("dspy.History / resume projection is deferred")


class SemanticTraceBackend(Protocol):
    """Durable sink for semantic events."""

    name: str

    def emit(self, event: SemanticEvent) -> None: ...


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
        # The durable canonical trace always captures the FULL event; redaction
        # is a per-consumer projection applied elsewhere, never here.
        line = json.dumps(event.to_dict("full"), sort_keys=True)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.write("\n")


def build_trace_backend(default_root: Path) -> SemanticTraceBackend:
    """Build the configured durable trace backend.

    ``CLIO_SEMANTIC_TRACE_BACKEND`` accepts ``file``, ``factory``, or ``none``.
    ``CLIO_SEMANTIC_TRACE_PATH`` may point at either a JSONL file or a
    directory. The durable file backend is OPT-IN (default ``none``): it is the
    substrate the unified memory underbelly is built on (ARC live view,
    re-extract repair, research replay), but the current ``emit`` performs
    synchronous file I/O on the turn's event loop, which can destabilize turn
    cancellation/timing under load. Flipping the default on is gated on moving
    durable writes off the loop -- see the "make file backend safe as default"
    issue. Enable explicitly with ``CLIO_SEMANTIC_TRACE_BACKEND=file`` (the grind
    harness and research runs do). Live semantic SSE is independent and always on.
    """

    from clio_agent import conf

    backend = (
        conf.resolve(
            "trace.backend",
            env="CLIO_SEMANTIC_TRACE_BACKEND",
            default="none",
            cast=conf.as_str,
        )
        .strip()
        .lower()
    )
    if backend in {"", "none", "off", "disabled"}:
        return NoopSemanticTraceBackend()
    if backend == "file":
        raw_path = conf.resolve(
            "trace.path", env="CLIO_SEMANTIC_TRACE_PATH", default="", cast=conf.as_str
        ).strip()
        path = Path(raw_path).expanduser() if raw_path else default_root
        return FileSemanticTraceBackend(path)
    if backend in {"factory", "python_factory", "custom"}:
        factory_path = os.environ.get("CLIO_SEMANTIC_TRACE_FACTORY", "").strip()
        if not factory_path:
            raise ValueError(
                "CLIO_SEMANTIC_TRACE_FACTORY is required when CLIO_SEMANTIC_TRACE_BACKEND=factory"
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
        capture: bool = True,
        hooks_full: bool = False,
        live_consumers: list[Callable[[SemanticEvent], None]] | None = None,
    ) -> None:
        self.bus = bus
        self.trace_backend = trace_backend
        self.detail_level = normalize_detail_level(detail_level)
        # ``capture`` gates the DURABLE canonical write (an SSE ``detail_level``
        # of "off" must NOT blind the canonical store — that is an SSE-only knob).
        self.capture = capture
        self.hooks_full = hooks_full
        self.live_consumers: list[Callable[[SemanticEvent], None]] = list(live_consumers or [])

    @property
    def trace_backend_name(self) -> str:
        return self.trace_backend.name

    def add_live_consumer(self, consumer: Callable[[SemanticEvent], None]) -> None:
        """Register a live consumer (e.g. ARC) that folds the RAW full event."""
        self.live_consumers.append(consumer)

    def emit(self, event: SemanticEvent) -> dict[str, Any]:
        event.detail_level = normalize_detail_level(event.detail_level or self.detail_level)
        # Durable canonical store + live consumers (ARC) ALWAYS get the FULL
        # event, gated only on ``capture`` — never on detail_level. Projection /
        # redaction happens per-consumer below.
        if self.capture:
            self.trace_backend.emit(event)
        for consumer in self.live_consumers:
            try:
                consumer(event)  # raw SemanticEvent, pre-projection (ARC folds this)
            except Exception:
                # Live consumers are observability side-effects; never crash a turn.
                pass
        full = project_full(event)
        # SSE + hooks get projected (redacted) views; SSE honors "off".
        if event.detail_level != "off":
            self.bus.publish(
                Event(
                    type="semantic.event",
                    session_id=event.session_id,
                    payload=project_sse(event),
                )
            )
            try:
                from clio_agent.runtime.hooks import fire as _fire_hook

                _fire_hook("semantic_event", project_hook(event, full=self.hooks_full))
            except Exception:
                # Semantic hooks are observability side-effects. They should
                # never mutate or crash the turn being observed.
                pass
        return full
