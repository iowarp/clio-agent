"""In-memory ledger retention bounds for the GACT server (#770 Wave-C C3).

Several of the server's in-memory ledgers (``command_audit``,
``memory_tool_audit``, ``context_frames``, ``pending_diffs``, ``permissions``,
``turn_attempts``, ``shared_tokens``) were append-only and grew unbounded for
the life of the process. This module gives each a *bound* plus a **structured
eviction reason** so a drop is never silent -- mirroring the ``stream_fallback``
reason-catalog contract in :mod:`clio_agent.gact.streaming` /
:mod:`clio_agent.gact.runtime.capabilities`.

Two eviction shapes are supported:

* **FIFO** (``is_terminal is None``): the oldest entry is evicted first once the
  ledger exceeds ``max_entries``. Used for pure audit trails where every row is
  equal (``command_audit``, ``memory_tool_audit``, per-session ``context_frames``).
* **terminal-first** (``is_terminal`` set): while the ledger exceeds the *soft*
  ``max_entries`` the oldest **terminal** row (resolved / applied / rejected /
  expired) is evicted, so a still-*pending* row is preserved through a slow HITL
  flow. Only when the ledger blows past the *hard* ``hard_cap`` with no terminal
  rows left is the oldest pending row force-evicted -- and that force carries a
  distinct reason so the backlog stays visible.

Every eviction records a typed payload (built from
:data:`_LEDGER_EVICTION_REASON_DEFINITIONS`, unknown reasons rejected) into the
per-app ``app.state.ledger_evictions`` audit deque (itself bounded) and emits a
``trace.event`` so the drop reaches the trace/observability plane.
"""

from __future__ import annotations

import os
from collections import deque
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

# Bound the audit trail of evictions itself so the observability store cannot be
# the new unbounded ledger. Oldest eviction records roll off first (deque maxlen).
_EVICTION_AUDIT_MAXLEN = 512


# --------------------------------------------------------------------------- #
# Typed eviction reason catalog (closed set; unknown reasons are rejected)
# --------------------------------------------------------------------------- #

_LEDGER_EVICTION_REASON_DEFINITIONS: dict[str, dict[str, Any]] = {
    "capacity_fifo": {
        "category": "ledger_retention",
        "policy": "fifo_oldest_first",
        "recovery_actions": ["increase_bound", "persist_to_durable_store"],
        "description": (
            "An in-memory audit ledger reached its retention bound; the oldest "
            "entry was evicted (FIFO). The row is gone from the hot store but was "
            "already published to its event/trace sink when written."
        ),
    },
    "capacity_terminal_first": {
        "category": "ledger_retention",
        "policy": "terminal_oldest_first",
        "recovery_actions": ["increase_bound", "resolve_pending_faster"],
        "description": (
            "An in-memory ledger reached its retention bound; the oldest terminal "
            "(resolved / applied / rejected / expired) entry was evicted while "
            "still-pending entries were preserved."
        ),
    },
    "capacity_forced_pending": {
        "category": "ledger_retention",
        "policy": "forced_oldest_over_hard_cap",
        "recovery_actions": ["increase_bound", "investigate_pending_backlog"],
        "description": (
            "An in-memory ledger exceeded its HARD cap with no terminal entries "
            "left to evict; the oldest still-pending entry was force-evicted to "
            "keep the process bounded. A persistent backlog of pending rows is a "
            "signal worth investigating."
        ),
    },
}


def ledger_eviction_payload(reason: str, *, ledger: str, **extra: Any) -> dict[str, Any]:
    """Build a typed, self-describing eviction payload; reject unknown reasons.

    Mirrors :func:`clio_agent.gact.streaming._stream_fallback_payload`: the
    ``reason`` must be a key of :data:`_LEDGER_EVICTION_REASON_DEFINITIONS`, and
    the returned payload folds in that definition's static metadata plus the
    dynamic ``ledger`` name / ``extra`` provenance (evicted key, session id).
    """

    definition = _LEDGER_EVICTION_REASON_DEFINITIONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown ledger eviction reason: {reason}")
    payload: dict[str, Any] = {
        "reason": reason,
        "ledger": ledger,
        "evicted_at": datetime.now(timezone.utc).isoformat(),
        **{
            key: (list(value) if isinstance(value, list) else value)
            for key, value in definition.items()
        },
    }
    for key, value in extra.items():
        if value != "" and value is not None:
            payload[key] = value
    return payload


def ledger_eviction_reason_catalog() -> dict[str, dict[str, Any]]:
    """Return the audited eviction reason catalog (for capability metadata)."""

    return {
        reason: {
            key: list(value) if isinstance(value, list) else value for key, value in details.items()
        }
        for reason, details in _LEDGER_EVICTION_REASON_DEFINITIONS.items()
    }


# --------------------------------------------------------------------------- #
# Per-ledger bound registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LedgerBound:
    """Retention policy for one ledger.

    Attributes:
        max_entries: Soft bound. FIFO ledgers are trimmed to exactly this; for
            terminal-first ledgers this is where terminal-eviction starts.
        hard_cap: Absolute ceiling for a terminal-first ledger; once exceeded,
            the oldest pending row is force-evicted. Defaults to ``max_entries``
            (i.e. pure FIFO with no pending grace) when not given.
        is_terminal: Predicate marking a row safe to evict first. ``None`` means
            every row is equal (FIFO).
    """

    max_entries: int
    hard_cap: int | None = None
    is_terminal: Callable[[Any], bool] | None = None

    @property
    def effective_hard_cap(self) -> int:
        return self.hard_cap if self.hard_cap is not None else self.max_entries


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _terminal_permission(row: Any) -> bool:
    """A permission row is terminal once it is no longer awaiting a decision."""

    if not isinstance(row, Mapping):
        return False
    return str(row.get("status") or "pending") not in {"pending", ""}


def _terminal_diff(row: Any) -> bool:
    """A pending-diff row is terminal once applied / rejected / failed."""

    if not isinstance(row, Mapping):
        return False
    return str(row.get("status") or "pending") in {"applied", "rejected", "apply_failed"}


def _terminal_turn_attempt(row: Any) -> bool:
    """A retry attempt is terminal once it has reached a settled status."""

    status = getattr(row, "status", None)
    if status is None and isinstance(row, Mapping):
        status = row.get("status")
    return str(status or "") in {"completed", "failed", "cancelled", "denied", "error"}


def _terminal_shared_token(row: Any) -> bool:
    """A share token is terminal once its TTL has elapsed."""

    if not isinstance(row, Mapping):
        return False
    expires_at = row.get("expires_at") or 0
    try:
        expires = float(expires_at)
    except (TypeError, ValueError):
        return False
    return expires > 0 and datetime.now(timezone.utc).timestamp() > expires


# Generous, age-tolerant defaults. Env-overridable for tuning without a redeploy;
# discovered-not-hardcoded in spirit (the values are a safety ceiling, not a
# behavioural knob). Terminal-first ledgers keep pending rows until the hard cap.
LEDGER_BOUNDS: dict[str, LedgerBound] = {
    "command_audit": LedgerBound(max_entries=_env_int("CLIO_LEDGER_COMMAND_AUDIT_MAX", 2000)),
    "memory_tool_audit": LedgerBound(
        max_entries=_env_int("CLIO_LEDGER_MEMORY_TOOL_AUDIT_MAX", 2000)
    ),
    "context_frames": LedgerBound(max_entries=_env_int("CLIO_LEDGER_CONTEXT_FRAMES_MAX", 200)),
    "pending_diffs": LedgerBound(
        max_entries=_env_int("CLIO_LEDGER_PENDING_DIFFS_MAX", 500),
        hard_cap=_env_int("CLIO_LEDGER_PENDING_DIFFS_HARD", 1000),
        is_terminal=_terminal_diff,
    ),
    "permissions": LedgerBound(
        max_entries=_env_int("CLIO_LEDGER_PERMISSIONS_MAX", 2000),
        hard_cap=_env_int("CLIO_LEDGER_PERMISSIONS_HARD", 4000),
        is_terminal=_terminal_permission,
    ),
    "turn_attempts": LedgerBound(
        max_entries=_env_int("CLIO_LEDGER_TURN_ATTEMPTS_MAX", 2000),
        hard_cap=_env_int("CLIO_LEDGER_TURN_ATTEMPTS_HARD", 4000),
        is_terminal=_terminal_turn_attempt,
    ),
    "shared_tokens": LedgerBound(
        max_entries=_env_int("CLIO_LEDGER_SHARED_TOKENS_MAX", 5000),
        hard_cap=_env_int("CLIO_LEDGER_SHARED_TOKENS_HARD", 10000),
        is_terminal=_terminal_shared_token,
    ),
}


# --------------------------------------------------------------------------- #
# State init + eviction recording
# --------------------------------------------------------------------------- #


def init_retention_state(app: "FastAPI") -> None:
    """Initialise the bounded eviction-audit deque on ``app.state``."""

    app.state.ledger_evictions = deque(maxlen=_EVICTION_AUDIT_MAXLEN)


def _record_eviction(app: "FastAPI", payload: dict[str, Any]) -> None:
    """Persist a typed eviction reason to the audit deque and the trace plane."""

    store = getattr(app.state, "ledger_evictions", None)
    if store is None:
        store = deque(maxlen=_EVICTION_AUDIT_MAXLEN)
        app.state.ledger_evictions = store
    store.append(payload)
    trace.event(
        "LEDGER-EVICT",
        "%s evicted (%s) key=%s",
        payload.get("ledger"),
        payload.get("reason"),
        payload.get("key", ""),
    )


# --------------------------------------------------------------------------- #
# Bound enforcement
# --------------------------------------------------------------------------- #


def enforce_list_bound(
    app: "FastAPI",
    ledger: list[Any],
    name: str,
    *,
    session_id: str = "",
) -> None:
    """Trim a list ledger in place to its configured bound, recording each drop.

    FIFO ledgers are trimmed to ``max_entries`` (oldest first). Terminal-first
    ledgers evict the oldest terminal row down to ``max_entries``, then force the
    oldest row only past ``hard_cap``.
    """

    bound = LEDGER_BOUNDS.get(name)
    if bound is None:
        return

    if bound.is_terminal is not None:
        while len(ledger) > bound.max_entries:
            idx = _first_terminal_index(ledger, bound.is_terminal)
            if idx is None:
                break
            row = ledger.pop(idx)
            _record_eviction(
                app,
                ledger_eviction_payload(
                    "capacity_terminal_first",
                    ledger=name,
                    session_id=session_id,
                    key=_row_key(row),
                ),
            )
        forced_reason = "capacity_forced_pending"
    else:
        forced_reason = "capacity_fifo"

    while len(ledger) > bound.effective_hard_cap:
        row = ledger.pop(0)
        _record_eviction(
            app,
            ledger_eviction_payload(
                forced_reason,
                ledger=name,
                session_id=session_id,
                key=_row_key(row),
            ),
        )


def enforce_dict_bound(
    app: "FastAPI",
    ledger: MutableMapping[str, Any],
    name: str,
    *,
    session_id: str = "",
) -> None:
    """Trim a dict ledger (keyed by id, insertion-ordered) to its bound in place.

    Insertion order is the age order (Python dicts preserve it). Same policy as
    :func:`enforce_list_bound`: terminal-first down to ``max_entries``, then
    forced-oldest past ``hard_cap``.
    """

    bound = LEDGER_BOUNDS.get(name)
    if bound is None:
        return

    if bound.is_terminal is not None:
        while len(ledger) > bound.max_entries:
            victim = _first_terminal_key(ledger, bound.is_terminal)
            if victim is None:
                break
            ledger.pop(victim, None)
            _record_eviction(
                app,
                ledger_eviction_payload(
                    "capacity_terminal_first",
                    ledger=name,
                    session_id=session_id,
                    key=victim,
                ),
            )
        forced_reason = "capacity_forced_pending"
    else:
        forced_reason = "capacity_fifo"

    while len(ledger) > bound.effective_hard_cap:
        victim = next(iter(ledger))
        ledger.pop(victim, None)
        _record_eviction(
            app,
            ledger_eviction_payload(
                forced_reason,
                ledger=name,
                session_id=session_id,
                key=victim,
            ),
        )


def _first_terminal_index(ledger: list[Any], is_terminal: Callable[[Any], bool]) -> int | None:
    for idx, row in enumerate(ledger):
        if is_terminal(row):
            return idx
    return None


def _first_terminal_key(
    ledger: Mapping[str, Any], is_terminal: Callable[[Any], bool]
) -> str | None:
    for key, row in ledger.items():
        if is_terminal(row):
            return key
    return None


def _row_key(row: Any) -> str:
    """Best-effort provenance id for an evicted row (for the audit payload)."""

    if isinstance(row, Mapping):
        return str(row.get("id") or "")
    return str(getattr(row, "id", "") or "")
