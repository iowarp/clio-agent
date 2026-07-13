"""Bounded, lazily-rehydrated resident set for GACT message ledgers (#889).

Boot used to parse *every* ``messages/*.json`` into ``app.state.messages`` before
the port bound (``MessageStore.load_all``), pinning ~2.36 MiB/session in RAM for
the life of the process with no cap (238 MiB → 1.42 GiB at 500 sessions). Reads
were "free" only because the whole corpus was already resident.

This module replaces that eager dict with :class:`ResidentLedgerSet` — a
``MutableMapping[str, list[Message]]`` whose surface is byte-identical to the plain
dict every reader/writer already uses (``get`` / ``setdefault`` / ``[]`` /
``pop`` / iteration), but which:

* **boots on the index only** — no message body is resident after ``build_app``;
* **materializes a session's ledger lazily** on first access (``GET /messages``,
  SSE attach, turn start, any mutation) from the per-session file the store
  already keeps, then caches it in a **bounded LRU** with a **byte cap**, a
  **count cap**, and **idle-TTL** eviction;
* **never evicts an active session** — one with a live SSE subscriber or an
  in-flight turn is pinned until it goes idle;
* **emits a typed audit reason for every eviction and every rehydration** (no
  silent fallback — mirrors the ``stream_fallback`` / ledger-retention catalogs).

Eviction is always safe because the on-disk per-session file is the authoritative
copy: every writer (``session_store._append/_extend/_replace/_delete``) writes
through to disk, so dropping the resident copy loses nothing — the next access
rehydrates byte-identically. The resident copy is a pure cache/projection.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable, Iterator, MutableMapping
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import TYPE_CHECKING, Any, Optional

from clio_agent import conf
from clio_agent.gact.messages import LedgerReadError
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.messages import MessageStore
    from clio_agent.gact.metrics_counters import MetricsCounters
    from clio_agent.gact.types import Message


# --------------------------------------------------------------------------- #
# Typed eviction / rehydration reason catalog (closed set; unknown rejected)
# --------------------------------------------------------------------------- #

_RESIDENT_LEDGER_REASON_DEFINITIONS: dict[str, dict[str, Any]] = {
    "capacity_bytes": {
        "category": "resident_ledger",
        "policy": "lru_oldest_idle_first",
        "recovery_actions": ["increase_byte_cap", "reduce_idle_ttl"],
        "description": (
            "The resident transcript set exceeded its byte cap; the oldest idle "
            "session's in-memory ledger was dropped. The transcript is intact on "
            "disk and rehydrates byte-identically on the next access."
        ),
    },
    "capacity_count": {
        "category": "resident_ledger",
        "policy": "lru_oldest_idle_first",
        "recovery_actions": ["increase_count_cap", "reduce_idle_ttl"],
        "description": (
            "The resident transcript set exceeded its session-count cap; the "
            "oldest idle session's in-memory ledger was dropped (on-disk copy "
            "retained, rehydrates on next access)."
        ),
    },
    "idle_ttl": {
        "category": "resident_ledger",
        "policy": "idle_ttl_release",
        "recovery_actions": ["increase_idle_ttl"],
        "description": (
            "A resident session ledger sat idle past the TTL and was released "
            "from memory. The on-disk copy is retained and rehydrates on access."
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


# --------------------------------------------------------------------------- #
# Configuration (conf.resolve + env; defaults justified from 2.36 MiB/session)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ResidentLedgerConfig:
    """Residency bounds for the transcript LRU.

    Defaults are anchored to the measured ~2.36 MiB/session (#893):

    * ``max_bytes`` 512 MiB ≈ 217 resident sessions — the primary memory guard.
      Unbounded, a 500-session install held ~1.42 GiB and grew linearly; this
      caps resident transcript RAM at O(1) in the *total* session count.
    * ``max_sessions`` 512 — a cheap secondary guard so a flood of tiny sessions
      cannot blow the count even while under the byte cap.
    * ``idle_ttl_s`` 1800 s (30 min) — an idle session's transcript is released
      well before either cap is hit, so a mostly-quiet server trends toward its
      genuinely-active working set.
    """

    max_bytes: int = 512 * 1024 * 1024
    max_sessions: int = 512
    idle_ttl_s: float = 1800.0

    @classmethod
    def from_conf(cls) -> "ResidentLedgerConfig":
        """Resolve the bounds from config file → env → defaults.

        Config values are coherently error-handled: an unparseable value (e.g.
        ``CLIO_RESIDENT_LEDGERS_MAX_BYTES=lots``) or an out-of-domain one (``<= 0``)
        both fall back to the default and emit a **typed trace reason** — never a
        boot crash and never a silent swap (no-silent-fallback, §3.3).
        """

        return cls(
            max_bytes=_resolve_positive_int(
                "gact.resident_ledgers.max_bytes", "CLIO_RESIDENT_LEDGERS_MAX_BYTES", cls.max_bytes
            ),
            max_sessions=_resolve_positive_int(
                "gact.resident_ledgers.max_sessions", "CLIO_RESIDENT_LEDGERS_MAX", cls.max_sessions
            ),
            idle_ttl_s=_resolve_positive_float(
                "gact.resident_ledgers.idle_ttl_s", "CLIO_RESIDENT_LEDGERS_TTL_S", cls.idle_ttl_s
            ),
        )


def _config_fallback(key: str, raw: Any, default: Any, why: str) -> None:
    """Emit a typed trace reason for an invalid residency-config value (no silent swap)."""

    trace.event(
        "RESIDENT-LEDGER",
        "config %s=%r invalid (%s); falling back to default %r",
        key,
        raw,
        why,
        default,
    )


def _resolve_positive_int(key: str, env: str, default: int) -> int:
    """Resolve a positive-int bound; typed-reason fallback to ``default`` on any bad value."""

    raw = conf.resolve(key, env=env, default=default)
    if raw is default:  # neither file nor env set the key — use the default silently
        return default
    try:
        value = conf.as_int(raw)
    except (TypeError, ValueError):
        _config_fallback(key, raw, default, "not an integer")
        return default
    if value <= 0:
        _config_fallback(key, raw, default, "must be > 0")
        return default
    return value


def _resolve_positive_float(key: str, env: str, default: float) -> float:
    """Resolve a positive-float bound; typed-reason fallback to ``default`` on any bad value."""

    default = float(default)
    raw = conf.resolve(key, env=env, default=default)
    if raw is default:  # neither file nor env set the key — use the default silently
        return default
    try:
        value = conf.as_float(raw)
    except (TypeError, ValueError):
        _config_fallback(key, raw, default, "not a number")
        return default
    if value <= 0:
        _config_fallback(key, raw, default, "must be > 0")
        return default
    return value


# --------------------------------------------------------------------------- #
# The resident set
# --------------------------------------------------------------------------- #


@dataclass
class _Entry:
    """One resident session's cached ledger plus its LRU bookkeeping.

    ``nmsgs`` is the message count captured when ``nbytes`` was last measured, so
    an in-place append (via ``setdefault(sid, []).append`` — which never routes
    through ``_install``) is detectable by a length change and re-measured lazily,
    without re-walking every resident ledger on every cache miss.
    """

    messages: list["Message"]
    nbytes: int
    nmsgs: int
    last_access: float


class ResidentLedgerSet(MutableMapping[str, list["Message"]]):
    """Bounded, lazily-rehydrated view over the per-session message store.

    Presents the exact ``dict[str, list[Message]]`` surface every reader/writer
    already relies on. ``get`` / ``setdefault`` / ``[]`` transparently rehydrate a
    missing-but-persisted session from disk; iteration and ``len`` enumerate the
    whole index (resident ∪ on-disk) so whole-index operations (delete-by-id, the
    golden metrics walk) still see every session.

    Most ``MutableMapping`` conveniences (``get``, ``setdefault``, ``values``,
    ``items``, ``keys``) are the stdlib mixins layered on the five primitives below,
    so their semantics match a plain dict — including ``setdefault(sid, []).append(
    msg)`` returning the *resident* list so the append mutates the cached copy.
    ``clear`` / ``popitem`` are overridden (the mixin ``popitem`` would infinite-loop
    over on-disk ids) to operate on the resident cache only, and :meth:`discard`
    drops a resident copy without materializing a to-be-deleted ledger.
    """

    def __init__(
        self,
        store: "MessageStore",
        *,
        config: Optional[ResidentLedgerConfig] = None,
        is_active: Optional[Callable[[str], bool]] = None,
        audit: Optional[Callable[[dict[str, Any]], None]] = None,
        materialize: Optional[Callable[[str], Optional[list["Message"]]]] = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        """Wire the set to its durable store and its residency policy.

        Args:
            store: The per-session durable ledger store (authoritative copy).
            config: Residency bounds; defaults to :meth:`ResidentLedgerConfig.from_conf`.
            is_active: Predicate — is this session pinned (live SSE / in-flight turn)?
                Active sessions are never evicted. Defaults to "nothing is active".
            audit: Sink for typed eviction/rehydration payloads (no silent fallback).
                Defaults to a no-op (tests may inject ``list.append``).
            materialize: The rehydration SOURCE (#737 S5): given a session id, return its
                ``list[Message]`` (``None`` = never persisted, ``[]`` = empty,
                :class:`~clio_agent.gact.messages.LedgerReadError` = propagate). Defaults
                to ``store.load_session`` (today's legacy path); the app wires the
                regime-aware transcript projection so an atoms-regime session rehydrates
                from the canonical log. Only the SOURCE moves — the LRU/TTL/pin mechanics
                are unchanged.
            clock: Monotonic time source (injectable for deterministic TTL tests).
        """

        self._store = store
        self._cfg = config or ResidentLedgerConfig.from_conf()
        self._is_active = is_active or (lambda _sid: False)
        self._audit = audit or (lambda _payload: None)
        self._materialize = materialize or store.load_session
        self._clock = clock
        self._resident: "OrderedDict[str, _Entry]" = OrderedDict()
        self._total_bytes = 0

    # ---- MutableMapping primitives ------------------------------------- #

    def __getitem__(self, sid: str) -> list["Message"]:
        entry = self._resident.get(sid)
        if entry is not None:
            self._resident.move_to_end(sid)
            entry.last_access = self._clock()
            return entry.messages
        try:
            rows = self._materialize(sid)
        except LedgerReadError as exc:
            # The ledger exists on disk but could not be read/parsed. Emit a typed
            # reason and PROPAGATE — never cache an empty list as if the session were
            # empty (that would make GET /messages serve [] for a full transcript,
            # then a partial one after the next write-through append). #889/§3.3.
            self._audit(
                resident_ledger_reason_payload("rehydrate_failed", session_id=sid, error=str(exc))
            )
            raise
        if rows is None:
            raise KeyError(sid)
        self._install(sid, rows, rehydrated=True)
        return self._resident[sid].messages

    def __setitem__(self, sid: str, messages: list["Message"]) -> None:
        # Store the caller's list object (not a copy): whole-ledger replace
        # (compaction / undo / fork / import) hands us the new ledger and expects
        # subsequent reads to see exactly it.
        self._install(sid, messages, rehydrated=False)

    def __delitem__(self, sid: str) -> None:
        entry = self._resident.pop(sid, None)
        if entry is None:
            raise KeyError(sid)
        self._total_bytes -= entry.nbytes

    def __iter__(self) -> Iterator[str]:
        seen: set[str] = set()
        for sid in list(self._resident.keys()):
            seen.add(sid)
            yield sid
        for sid in self._store.session_ids():
            if sid not in seen:
                seen.add(sid)
                yield sid

    def __len__(self) -> int:
        return len(set(self._resident.keys()) | set(self._store.session_ids()))

    def __contains__(self, sid: object) -> bool:
        # Cheap membership: never materializes a ledger just to answer ``in``.
        if not isinstance(sid, str):
            return False
        return sid in self._resident or self._store.has_session(sid)

    # ---- whole-store convenience overrides ----------------------------- #

    def discard(self, sid: str) -> None:
        """Drop the resident (in-memory) copy of a ledger WITHOUT materializing it.

        The delete seam (``_delete_session_messages`` → ``store.delete_session``)
        calls this before unlinking the on-disk ledger. Unlike the ``MutableMapping``
        ``pop`` mixin — implemented as ``self[sid]`` + ``del``, which forces a full
        rehydration (disk read + parse) of a ledger that is about to be deleted, emits
        a misleading ``rehydrate`` audit row, and can transiently push a warm session
        out under cap pressure — this never touches disk and emits no audit row: it is
        not an eviction, just releasing a cache slot for a session being destroyed.
        """

        entry = self._resident.pop(sid, None)
        if entry is not None:
            self._total_bytes -= entry.nbytes

    def clear(self) -> None:
        """Drop every resident (in-memory) ledger; on-disk copies are untouched.

        Overrides the ``MutableMapping`` mixin, whose ``popitem`` loop would spin
        FOREVER here: ``__iter__`` re-yields on-disk session ids that ``__delitem__``
        only removes from memory, so the default ``clear`` never terminates. This
        releases the cache (memory only) — durable ledgers rehydrate on next access;
        deleting the durable store is an explicit per-session ``delete_session``.
        """

        self._resident.clear()
        self._total_bytes = 0

    def popitem(self) -> tuple[str, list["Message"]]:
        """Pop the most-recently-used RESIDENT ledger (memory only), or raise.

        Overrides the mixin default, which would infinite-loop over on-disk ids for
        the same reason :meth:`clear` documents. Operates on the resident cache only.
        """

        if not self._resident:
            raise KeyError("popitem(): no resident ledger")
        sid, entry = self._resident.popitem(last=True)
        self._total_bytes -= entry.nbytes
        return sid, entry.messages

    # ---- residency mechanics ------------------------------------------- #

    def _install(self, sid: str, messages: list["Message"], *, rehydrated: bool) -> None:
        """Cache a session's ledger, refresh accounting, and enforce the caps."""

        old = self._resident.pop(sid, None)
        if old is not None:
            self._total_bytes -= old.nbytes
        nbytes = _estimate_bytes(messages)
        self._resident[sid] = _Entry(messages, nbytes, len(messages), self._clock())
        self._total_bytes += nbytes
        if rehydrated:
            self._audit(resident_ledger_reason_payload("rehydrate", session_id=sid, bytes=nbytes))
        self._sweep_idle(protect=sid)
        self._enforce_caps(protect=sid)

    def _sweep_idle(self, *, protect: str) -> None:
        """Release resident ledgers idle past the TTL (never the active/protected)."""

        now = self._clock()
        ttl = self._cfg.idle_ttl_s
        for sid in list(self._resident.keys()):
            if sid == protect:
                continue
            entry = self._resident.get(sid)
            if entry is None:
                continue
            if now - entry.last_access <= ttl:
                continue
            if self._is_active(sid):
                continue
            self._evict(sid, "idle_ttl")

    def _enforce_caps(self, *, protect: str) -> None:
        """Evict oldest idle ledgers until both the byte and count caps hold."""

        self._reconcile_bytes()
        skipped_bytes = False
        while self._total_bytes > self._cfg.max_bytes:
            victim = self._pick_victim(protect)
            if victim is None:
                skipped_bytes = True
                break
            self._evict(victim, "capacity_bytes")
        skipped_count = False
        while len(self._resident) > self._cfg.max_sessions:
            victim = self._pick_victim(protect)
            if victim is None:
                skipped_count = True
                break
            self._evict(victim, "capacity_count")
        if skipped_bytes or skipped_count:
            self._audit(
                resident_ledger_reason_payload(
                    "eviction_skipped_all_active",
                    session_id=protect,
                    resident=len(self._resident),
                    bytes=self._total_bytes,
                )
            )

    def _pick_victim(self, protect: str) -> Optional[str]:
        """Oldest resident session that is neither protected nor active, else None."""

        for sid in list(self._resident.keys()):  # LRU order: oldest first (snapshot)
            if sid == protect:
                continue
            if self._is_active(sid):
                continue
            return sid
        return None

    def _evict(self, sid: str, reason: str) -> None:
        entry = self._resident.pop(sid, None)
        if entry is None:
            return
        self._total_bytes -= entry.nbytes
        self._audit(resident_ledger_reason_payload(reason, session_id=sid, bytes=entry.nbytes))

    def _reconcile_bytes(self) -> None:
        """Cheaply true up byte accounting on the cap-enforcement hot path.

        In-place writes (``setdefault(sid, []).append``) grow a cached list without
        routing through :meth:`_install`, so a resident entry's ``nbytes`` can go
        stale between installs. Re-measure ONLY the entries whose message count
        changed since they were last measured — an ``O(resident)`` length-check
        (trivial int compares) plus an ``O(changed)`` part walk — instead of the old
        unconditional ``O(total resident bytes)`` re-walk on EVERY cache miss. Writers
        only append (never mutate an existing part's text in place), so a length delta
        catches every drift; a whole-ledger replace re-measures via :meth:`_install`.
        """

        total = 0
        for entry in list(self._resident.values()):
            if len(entry.messages) != entry.nmsgs:
                entry.nbytes = _estimate_bytes(entry.messages)
                entry.nmsgs = len(entry.messages)
            total += entry.nbytes
        self._total_bytes = total

    def _recompute_bytes(self) -> None:
        """Fully re-measure every resident ledger (exact accounting for diagnostics)."""

        total = 0
        for entry in list(self._resident.values()):
            entry.nbytes = _estimate_bytes(entry.messages)
            entry.nmsgs = len(entry.messages)
            total += entry.nbytes
        self._total_bytes = total

    # ---- introspection (tests / diagnostics) --------------------------- #

    @property
    def resident_session_ids(self) -> list[str]:
        """Currently-resident session ids, oldest → newest (LRU order)."""

        return list(self._resident.keys())

    @property
    def resident_count(self) -> int:
        """Number of session ledgers currently held in memory."""

        return len(self._resident)

    @property
    def resident_bytes(self) -> int:
        """Estimated resident transcript bytes (the byte-cap accounting)."""

        self._recompute_bytes()
        return self._total_bytes


def _estimate_bytes(messages: list["Message"]) -> int:
    """Serialized-footprint proxy for a ledger's resident cost.

    Uses each message's JSON serialization (``model_dump_json``) so the byte cap —
    the PRIMARY memory guard — accounts for EVERY payload-bearing field, not just
    top-level ``part.text``: base64 image ``data``, tool ``input`` and nested
    ``content`` parts (where ``tool_result`` bodies live), ``unified_diff`` /
    ``new_content`` file diffs, compaction ``summary``, ``thought``, and per-message
    / per-part ``metadata``. Those heavy payloads are exactly what #889 exists to
    bound; the old text-only estimate under-counted a tool-result-heavy or
    multimodal ledger by orders of magnitude, letting the byte cap be effectively
    blind to the worst sessions. A JSON-length proxy is monotonic in real size and a
    safe bound (the exact object-graph RAM is larger); the next enforce pass trues it.
    """

    total = 0
    for message in messages:
        dump = getattr(message, "model_dump_json", None)
        if dump is None:
            total += 256
            continue
        try:
            total += len(dump())
        except Exception:  # noqa: BLE001 - estimate-only; a bound, never a served value
            total += 256
    return total


# --------------------------------------------------------------------------- #
# Boot wiring
# --------------------------------------------------------------------------- #


def seed_metrics_counters(store: "MessageStore", counters: "MetricsCounters") -> None:
    """Seed the running metrics aggregate WITHOUT keeping any transcript resident.

    Streams the per-session ledgers one at a time (:meth:`MessageStore.iter_session_ledgers`),
    folding each into ``counters`` and dropping the bodies before the next is read.
    The metrics wire (``GET /v1/metrics`` — total, by-role, tool-latency percentiles)
    therefore stays byte-identical across a restart while peak transient boot memory
    is O(largest session), not O(whole corpus), and — crucially — nothing lingers in
    ``app.state.messages`` (the resident LRU boots empty).

    KNOWN DEVIATION (typed, not silent): this streaming walk still PARSES every
    session's bodies at boot, so boot *time* remains O(total messages). Design row
    1.13 / §3.1 call for seeding from the session INDEX (per-session counts) with no
    body walk. Doing so byte-identically requires persisting a per-session metrics
    rollup — including tool-latency SAMPLES for exact cross-restart percentiles —
    on the SessionStore records, which trades against §3.1's "keep the boot index
    small" goal and the row-1.13 "byte-identical metrics across restart" frozen
    surface. That store-schema choice is owner-gated (design §4.2 step 2+; #889's
    delivered win is the bounded resident *memory*, not boot latency, which is
    #891's front). The deviation is recorded here so it is queryable, not silent.
    """

    trace.event(
        "RESIDENT-LEDGER",
        "metrics seeded via streaming corpus walk (bounded memory, O(corpus) boot time); "
        "index-only rollup is owner-gated (design 1.13/§3.1)",
    )
    for sid, rows in store.iter_session_ledgers():
        counters.set_session(sid, rows)


def _session_is_active(app: "FastAPI", sid: str) -> bool:
    """A session is pinned while it has a live SSE subscriber or an in-flight turn."""

    bus = getattr(app.state, "bus", None)
    if bus is not None and bus.subscriber_count(sid) > 0:
        return True
    sessions = getattr(app.state, "sessions", None)
    if sessions is not None:
        record = sessions.get(sid)
        if record is not None and getattr(record, "status", "") in {
            "running",
            "waiting_permission",
            "waiting_user",
        }:
            return True
    return False


# Routine lazy-materialization on first access. Recorded on the trace plane (never
# silent) but kept OUT of the bounded ``ledger_evictions`` API ring: rehydration is
# high-frequency cache traffic (one row per cold ``GET /messages``; a single
# workspace-wide memory search touches every session), so mixing it into the shared
# 512-slot deque would evict the genuine capacity/idle/retention eviction rows the
# ring exists to preserve. Every OTHER reason — including the low-frequency, always-
# important ``rehydrate_failed`` — still populates the ring.
_TRACE_ONLY_REASONS = frozenset({"rehydrate"})


def _record_resident_audit(app: "FastAPI", payload: dict[str, Any]) -> None:
    """Record a typed resident-ledger audit row to the trace plane + eviction deque.

    Every reason is traced (no silent fallback). Genuine eviction-class reasons also
    populate ``app.state.ledger_evictions`` (the same bounded observability surface
    the ledger-retention subsystem writes to, created here if retention init has not
    run yet). Routine ``rehydrate`` rows are trace-only — see :data:`_TRACE_ONLY_REASONS`
    — so cache traffic cannot flush the bounded ring's real eviction history.
    """

    trace.event(
        "RESIDENT-LEDGER",
        "%s session=%s bytes=%s",
        payload.get("reason"),
        payload.get("session_id", ""),
        payload.get("bytes", ""),
    )
    if payload.get("reason") in _TRACE_ONLY_REASONS:
        return
    store = getattr(app.state, "ledger_evictions", None)
    if store is None:
        store = deque(maxlen=512)
        app.state.ledger_evictions = store
    store.append(payload)


def build_resident_ledger_set(app: "FastAPI") -> ResidentLedgerSet:
    """Construct the app's resident transcript set from its message store.

    Wires the active-session pin (bus subscribers + running/​waiting sessions) and
    the typed audit sink. Requires ``app.state.message_store`` to be set.
    """

    from clio_agent.gact.transcript_projection import (  # noqa: PLC0415 - avoid import cycle
        materialize_ledger,
    )

    return ResidentLedgerSet(
        app.state.message_store,
        config=ResidentLedgerConfig.from_conf(),
        is_active=lambda sid: _session_is_active(app, sid),
        audit=lambda payload: _record_resident_audit(app, payload),
        materialize=lambda sid: materialize_ledger(app, sid),
    )
