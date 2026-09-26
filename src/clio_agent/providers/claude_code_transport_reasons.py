"""Typed transport-failure reason catalog for the claude_code SDK pool.

Pure data carved out of :mod:`clio_agent.providers.claude_code_sessions`
(#775 no-accretion); that module re-exports both names so existing importers
keep working.
"""

from __future__ import annotations

from typing import Any

__all__ = ["TRANSPORT_FAILURE_REASONS", "transport_failure_payload"]

# --------------------------------------------------------------------------- #
# Typed transport-failure reason catalog (no silent divergence — #775 ground
# rule). Same shape/discipline as providers.resolver.HANDSHAKE_FALLBACK_REASONS
# and gact.streaming._stream_fallback_payload: a connection drop is queryable
# structured data, never an invisible re-key.
# --------------------------------------------------------------------------- #
TRANSPORT_FAILURE_REASONS: dict[str, dict[str, Any]] = {
    "send_failed": {
        "category": "session_transport_error",
        "description": (
            "A query on the pooled Claude CLI connection failed mid-flight (the "
            "subprocess died or the stream broke). The poisoned client is dropped and "
            "the failure surfaces as a typed transient error so the LM retry layer "
            "re-issues the call on a fresh connection."
        ),
    },
    "idle_reaped": {
        "category": "session_idle_reap",
        "description": (
            "A session's pooled connection sat connected but unused past the idle "
            "TTL. Proactively dropped to bound resident claude-sdk-cli subprocess "
            "count; the session's next call reconnects fresh."
        ),
    },
    "config_change_requires_restart": {
        "category": "session_reconnect",
        "description": (
            "A thinking/system_prompt/cwd change on an existing session's connection "
            "cannot be applied live in this SDK version (no set_effort/set_thinking/"
            "set_cwd control request exists — only set_model and set_permission_mode "
            "mutate a connected client). The client reconnected with the new config."
        ),
    },
    "dead_client_replaced": {
        "category": "session_health",
        "description": (
            "A session's connection ended abnormally (a transport error, a timed-out "
            "query, or the caller abandoning the stream mid-flight). The dead client "
            "was replaced with a fresh connect, so the session's next call is never "
            "handed a poisoned client."
        ),
    },
}


def transport_failure_payload(reason: str, message: str = "") -> dict[str, Any]:
    """Build a structured transport-failure reason payload (catalog style).

    Mirrors :func:`clio_agent.gact.streaming._stream_fallback_payload`: looks
    ``reason`` up in :data:`TRANSPORT_FAILURE_REASONS`, copies its audited
    metadata, and appends an optional free-text ``message``. Raises ``ValueError``
    on an unknown reason so a typo cannot silently produce an empty reason.
    """
    definition = TRANSPORT_FAILURE_REASONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown transport failure reason: {reason}")
    payload: dict[str, Any] = {"reason": reason, **definition}
    if message:
        payload["message"] = message
    return payload
