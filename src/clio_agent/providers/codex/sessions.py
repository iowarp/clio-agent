"""Per-CLIO-session state shared by the SSE and WebSocket transports (A.5/A.6).

Keyed by the stable CLIO session id (:func:`clio_agent.gact.context.active_session_id`),
this holds the reasoning items pending carry-over into the next turn's
``input`` (A.5) and, for the WebSocket transport, the delta-continuation
snapshot plus per-session diagnostic counters (A.6). A process-wide registry,
mirroring the established pattern for CLI-routed providers keeping one
long-lived resource per session (``codex_stream.py``'s process-wide client).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CodexSessionState",
    "SessionCounters",
    "all_session_counters",
    "drop_session",
    "get_session",
    "pop_all_ws_connections",
    "reset_sessions_for_tests",
]


@dataclass
class SessionCounters:
    """Diagnostic counters (A.6): exposed via the provider record / a debug route."""

    connections_created: int = 0
    connections_reused: int = 0
    full_requests: int = 0
    delta_requests: int = 0
    fallbacks: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "connections_created": self.connections_created,
            "connections_reused": self.connections_reused,
            "full_requests": self.full_requests,
            "delta_requests": self.delta_requests,
            "fallbacks": self.fallbacks,
        }


@dataclass
class CodexSessionState:
    """One CLIO session's Codex-provider state."""

    session_id: str
    #: Reasoning items from the most recently completed turn, sent back
    #: unchanged in the next turn's ``input`` (A.5).
    reasoning_items: list[dict[str, Any]] = field(default_factory=list)
    #: Whether a prior WebSocket failure (before any event arrived) demoted
    #: this session to SSE-only (A.6). Sticky for the session's lifetime.
    sse_only: bool = False
    #: The WebSocket transport's own connection + delta-continuation snapshot
    #: (``transport_ws.WsConnection`` at runtime; typed ``Any`` here so this
    #: leaf module never imports the WS transport).
    ws: Any = None
    counters: SessionCounters = field(default_factory=SessionCounters)
    lock: threading.Lock = field(default_factory=threading.Lock)


_REGISTRY_LOCK = threading.Lock()
_SESSIONS: dict[str, CodexSessionState] = {}


def get_session(session_id: str) -> CodexSessionState:
    """Return (creating if needed) the state for ``session_id``."""

    with _REGISTRY_LOCK:
        state = _SESSIONS.get(session_id)
        if state is None:
            state = CodexSessionState(session_id=session_id)
            _SESSIONS[session_id] = state
        return state


def drop_session(session_id: str) -> None:
    """Discard a session's state (session end / logout)."""

    with _REGISTRY_LOCK:
        _SESSIONS.pop(session_id, None)


def pop_all_ws_connections() -> list[Any]:
    """Detach and return every session's pooled WebSocket connection.

    Used by a clean server shutdown (``runtime.process_tree``) to close every
    live connection: each session's ``ws`` is cleared here (never left
    dangling) so no later turn tries to reuse a connection that is about to
    be closed out from under it.
    """

    with _REGISTRY_LOCK:
        connections = [state.ws for state in _SESSIONS.values() if state.ws is not None]
        for state in _SESSIONS.values():
            state.ws = None
    return connections


def all_session_counters() -> dict[str, dict[str, int]]:
    """Every live session's counters, for a debug/diagnostics endpoint (A.6)."""

    with _REGISTRY_LOCK:
        return {sid: state.counters.as_dict() for sid, state in _SESSIONS.items()}


def reset_sessions_for_tests() -> None:
    with _REGISTRY_LOCK:
        _SESSIONS.clear()
