"""The typed reason catalog of the resident set (:mod:`clio_agent.gact.resident_ledgers`).

Every eviction and rehydration the resident set performs is recorded with one of these
reasons (no silent fallback): a closed set, an unknown reason is rejected.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

__all__ = ["resident_ledger_reason_catalog", "resident_ledger_reason_payload"]


# --------------------------------------------------------------------------- #
# Typed eviction / rehydration reason catalog (closed set; unknown rejected)
# --------------------------------------------------------------------------- #

_RESIDENT_LEDGER_REASON_DEFINITIONS: dict[str, dict[str, Any]] = {
    "capacity_bytes": {
        "category": "resident_ledger",
        "policy": "lru_oldest_idle_first",
        "recovery_actions": ["increase_byte_cap", "reduce_idle_ttl"],
        "description": (
            "The transcripts and ARC scopes held in memory exceeded the byte cap; the "
            "oldest idle session's copies were dropped. clio-core holds them, and they "
            "are re-read byte-identically on the next access."
        ),
    },
    "capacity_count": {
        "category": "resident_ledger",
        "policy": "lru_oldest_idle_first",
        "recovery_actions": ["increase_count_cap", "reduce_idle_ttl"],
        "description": (
            "More sessions were held in memory than the session-count cap; the oldest "
            "idle session's transcript and ARC scopes were dropped (clio-core holds "
            "them; re-read on next access)."
        ),
    },
    "idle_ttl": {
        "category": "resident_ledger",
        "policy": "idle_ttl_release",
        "recovery_actions": ["increase_idle_ttl"],
        "description": (
            "A session no view and no running work used sat idle past the TTL; its "
            "transcript and ARC scopes were released from memory. clio-core holds "
            "them, and they are re-read on the next access."
        ),
    },
    "obligation_done": {
        "category": "resident_ledger",
        "policy": "release_when_work_done",
        "recovery_actions": [],
        "description": (
            "An obligation (work resumed after a restart) finished and nobody views the "
            "conversation; its transcript and ARC scopes were released at once rather "
            "than after the idle TTL. clio-core holds them; re-read on the next access."
        ),
    },
    "eviction_skipped_all_active": {
        "category": "resident_ledger",
        "policy": "active_pin",
        "recovery_actions": ["increase_byte_cap", "increase_count_cap"],
        "description": (
            "The resident set is over cap but every resident session is active "
            "(live SSE subscriber or in-flight turn) and therefore pinned; no "
            "eviction was performed. The cap is temporarily soft until a session "
            "goes idle."
        ),
    },
    "rehydrate": {
        "category": "resident_ledger",
        "policy": "lazy_materialize",
        "recovery_actions": [],
        "description": (
            "A session's transcript was materialized into the resident set from "
            "its persisted ledger on first access (lazy rehydration)."
        ),
    },
    "rehydrate_failed": {
        "category": "resident_ledger",
        "policy": "propagate_typed_error",
        "recovery_actions": ["retry_read", "inspect_ledger_file"],
        "description": (
            "A session's persisted ledger exists but could not be read or parsed on "
            "rehydration (disk error or corrupt JSON). NOTHING was cached and the "
            "typed error propagates to the caller instead of silently serving an "
            "empty transcript — the on-disk ledger is untouched and a later read "
            "rehydrates it byte-identically once the transient fault clears."
        ),
    },
}


def resident_ledger_reason_payload(reason: str, *, session_id: str, **extra: Any) -> dict[str, Any]:
    """Build a typed, self-describing resident-ledger audit payload.

    Mirrors :func:`clio_agent.gact.runtime.retention.ledger_eviction_payload`: the
    ``reason`` must be a key of :data:`_RESIDENT_LEDGER_REASON_DEFINITIONS` (unknown
    reasons raise), and the returned payload folds in that definition's static
    metadata plus the dynamic provenance (session id, byte counts).
    """

    definition = _RESIDENT_LEDGER_REASON_DEFINITIONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown resident-ledger reason: {reason}")
    payload: dict[str, Any] = {
        "reason": reason,
        "ledger": "resident_message_ledgers",
        "session_id": session_id,
        "at": datetime.now(timezone.utc).isoformat(),
        **{
            key: (list(value) if isinstance(value, list) else value)
            for key, value in definition.items()
        },
    }
    for key, value in extra.items():
        if value != "" and value is not None:
            payload[key] = value
    return payload


def resident_ledger_reason_catalog() -> dict[str, dict[str, Any]]:
    """Return the audited resident-ledger reason catalog (for capability metadata)."""

    return {
        reason: {
            key: list(value) if isinstance(value, list) else value for key, value in details.items()
        }
        for reason, details in _RESIDENT_LEDGER_REASON_DEFINITIONS.items()
    }
