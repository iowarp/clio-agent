"""The one residency owner of the GACT server: message ledgers and ARC scopes (#889, R6).

clio-core is the record; everything this module tracks is a read cache of it.
:class:`ResidentLedgerSet` is a ``MutableMapping[str, list[Message]]`` with the plain
dict surface every transcript reader/writer uses, and it is also the
:class:`~clio_agent.arc.segment_cache.ScopeResidency` ARC reports every cached scope to.
So one budget bounds both copies of a session:

* **nothing is resident at boot**; a session's ledger materializes on first access and
  its ARC scopes load on first use, each counted against the session;
* **bounded**: a byte cap and a session-count cap over ledgers + ARC scopes together,
  oldest idle session first, and an **idle TTL**: a session no view (live SSE
  subscriber) and no running work (turn, obligation) uses is released;
* **never evicts an active session**;
* the periodic sweeper (:mod:`clio_agent.gact.resident_sweeper`) applies the TTL while
  the server is idle, and ARC asks for a sweep as soon as it pushes the set over a cap;
* **a typed audit reason for every eviction and rehydration** (no silent fallback).

Eviction loses nothing: every writer commits to clio-core first (the message atoms
before the ledger, ARC's ``ScopeCache._commit_scope`` before its cache), so the next
access rehydrates byte-identically. Search and recall read through :meth:`peek`, which
makes nothing resident.
"""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Iterator, MutableMapping
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any, Optional

from clio_agent import conf
from clio_agent.arc.segment_cache import transient_reads
from clio_agent.gact.messages import LedgerReadError
from clio_agent.gact.resident_reasons import (
    resident_ledger_reason_catalog as resident_ledger_reason_catalog,
)
from clio_agent.gact.resident_reasons import resident_ledger_reason_payload
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.messages import MessageStore
    from clio_agent.gact.types import Message


# --------------------------------------------------------------------------- #
# Configuration (conf.resolve + env; defaults justified from 2.36 MiB/session)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ResidentLedgerConfig:
    """Residency bounds for ledgers + ARC scopes (one budget per session).

    * ``max_bytes`` 512 MiB — the primary memory guard over transcript + ARC bytes,
      O(1) in the total session count (unbounded, 500 sessions held ~1.42 GiB).
    * ``max_sessions`` 512 — a secondary guard against a flood of tiny sessions.
    * ``idle_ttl_s`` 60 s — a session nobody views (a viewed conversation holds an
      SSE subscriber and is pinned) and nothing runs in is released a minute after
      its last use, so memory follows the open views and running work, not history.
    * ``sweep_interval_s`` 15 s — how often the idle sweep runs on its own.
    """

    max_bytes: int = 512 * 1024 * 1024
    max_sessions: int = 512
    idle_ttl_s: float = 60.0
    sweep_interval_s: float = 15.0

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
            sweep_interval_s=_resolve_positive_float(
                "gact.resident_ledgers.sweep_interval_s",
                "CLIO_RESIDENT_LEDGERS_SWEEP_S",
                cls.sweep_interval_s,
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


@dataclass
class _ArcEntry:
    """One session's ARC scopes held in process: encoded bytes per scope."""

    scopes: dict[str, int]
    last_access: float

    @property
    def nbytes(self) -> int:
        return sum(self.scopes.values())


class ResidentLedgerSet(MutableMapping[str, list["Message"]]):
    """The one residency owner: message ledgers and ARC scopes, bounded together.

    Presents the exact ``dict[str, list[Message]]`` surface every reader/writer
    already relies on. ``get`` / ``setdefault`` / ``[]`` transparently rehydrate a
    missing-but-persisted session; iteration and ``len`` enumerate the whole index
    (resident ∪ stored) so whole-index operations still see every session.

    It is also ARC's :class:`~clio_agent.arc.segment_cache.ScopeResidency`: every scope
    ARC caches is counted against its session, so the byte cap, the session-count cap
    and the idle TTL bound the transcript AND the ARC copies of a session as one unit.
    Evicting a session drops its ledger and calls ``on_evict``, which releases its ARC
    scopes (and the copies derived from it). :meth:`sweep` applies the idle TTL and
    the caps; it runs on every cache install and from the periodic sweeper.

    ``clear`` / ``popitem`` operate on the resident cache only (the mixins would loop
    forever over stored ids), and :meth:`discard` drops a resident copy without
    materializing a to-be-deleted ledger. :meth:`peek` reads a session without making
    it resident (search, recall).
    """

    def __init__(
        self,
        store: "MessageStore",
        *,
        config: Optional[ResidentLedgerConfig] = None,
        is_active: Optional[Callable[[str], bool]] = None,
        audit: Optional[Callable[[dict[str, Any]], None]] = None,
        materialize: Optional[Callable[[str], Optional[list["Message"]]]] = None,
        on_rehydrate: Optional[Callable[[str, list["Message"]], object]] = None,
        on_evict: Optional[Callable[[str], object]] = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        """Wire the set to its durable store and its residency policy.

        Args:
            store: The session index (which sessions have a transcript).
            config: Residency bounds; defaults to :meth:`ResidentLedgerConfig.from_conf`.
            is_active: Predicate — is this session pinned (live SSE / in-flight turn)?
                Active sessions are never evicted. Defaults to "nothing is active".
            audit: Sink for typed eviction/rehydration payloads (no silent fallback).
            materialize: The rehydration source: a session id to its ``list[Message]``
                (``None`` = never persisted, ``[]`` = empty, ``LedgerReadError`` =
                propagate). Defaults to ``store.load_session``.
            on_rehydrate: Once-per-materialization hook fired AFTER a cache-miss install
                (the lazy stale-handoff reconcile sweep); never on a cache hit.
            on_evict: Hook fired with a session id AFTER this set evicts it (capacity or
                idle TTL) — never for ``discard``/``clear``/``popitem``. The app wires it
                to release the session's ARC scopes, A2UI projection, task tree and
                model clients, so nothing derived from the session outlives it.
            clock: Monotonic time source (injectable for deterministic TTL tests).
        """

        self._store = store
        self._cfg = config or ResidentLedgerConfig.from_conf()
        self._is_active = is_active or (lambda _sid: False)
        self._audit = audit or (lambda _payload: None)
        self._materialize = materialize or store.load_session
        self._on_rehydrate = on_rehydrate
        self._on_evict = on_evict
        self._clock = clock
        self._resident: "OrderedDict[str, _Entry]" = OrderedDict()
        self._total_bytes = 0
        # ARC scopes per session. ONE lock guards both maps and is never held while
        # calling out (ARC reports into this set while holding its own scope locks).
        self._arc: dict[str, _ArcEntry] = {}
        self._arc_bytes = 0
        self._lock = threading.RLock()
        self.on_over_budget: Optional[Callable[[], object]] = None

    @property
    def config(self) -> ResidentLedgerConfig:
        """The residency bounds in force."""
        return self._cfg

    # ---- MutableMapping primitives ------------------------------------- #

    def __getitem__(self, sid: str) -> list["Message"]:
        with self._lock:
            entry = self._resident.get(sid)
            if entry is not None:
                self._resident.move_to_end(sid)
                entry.last_access = self._clock()
                return entry.messages
        try:
            rows = self._materialize(sid)
        except LedgerReadError as exc:
            # The ledger exists but could not be read/parsed: a typed reason, and the
            # error PROPAGATES — never cache an empty list as if the session were empty.
            self._audit(
                resident_ledger_reason_payload("rehydrate_failed", session_id=sid, error=str(exc))
            )
            raise
        if rows is None:
            raise KeyError(sid)
        self._install(sid, rows, rehydrated=True)
        if self._on_rehydrate is not None:
            # Once per cache-miss materialization, AFTER install, so a hook that
            # replaces the ledger sees the cached entry instead of re-materializing.
            installed = self.get_if_resident(sid)
            self._on_rehydrate(sid, installed if installed is not None else rows)
        current = self.get_if_resident(sid)
        return current if current is not None else rows

    def __setitem__(self, sid: str, messages: list["Message"]) -> None:
        # Store the caller's list object (not a copy): whole-ledger replace hands us
        # the new ledger and expects subsequent reads to see exactly it.
        self._install(sid, messages, rehydrated=False)

    def __delitem__(self, sid: str) -> None:
        with self._lock:
            entry = self._resident.pop(sid, None)
            if entry is None:
                raise KeyError(sid)
            self._total_bytes -= entry.nbytes

    def __iter__(self) -> Iterator[str]:
        seen: set[str] = set()
        for sid in self.resident_session_ids:
            seen.add(sid)
            yield sid
        for sid in self._store.session_ids():
            if sid not in seen:
                seen.add(sid)
                yield sid

    def __len__(self) -> int:
        return len(set(self.resident_session_ids) | set(self._store.session_ids()))

    def __contains__(self, sid: object) -> bool:
        # Cheap membership: never materializes a ledger just to answer ``in``.
        if not isinstance(sid, str):
            return False
        return sid in self._resident or self._store.has_session(sid)

    # ---- whole-store convenience overrides ----------------------------- #

    def discard(self, sid: str) -> None:
        """Drop the resident copy of a ledger WITHOUT materializing it (session delete).

        Unlike the ``pop`` mixin (``self[sid]`` + ``del``, a full rehydration of a ledger
        about to be deleted) this never touches the store and emits no audit row.
        """

        with self._lock:
            entry = self._resident.pop(sid, None)
            if entry is not None:
                self._total_bytes -= entry.nbytes

    def get_if_resident(self, sid: str) -> Optional[list["Message"]]:
        """Return a resident ledger without attempting durable materialization."""

        with self._lock:
            entry = self._resident.get(sid)
            if entry is None:
                return None
            self._resident.move_to_end(sid)
            entry.last_access = self._clock()
            return entry.messages

    def peek(self, sid: str) -> Optional[list["Message"]]:
        """Read a session's ledger without making it (or its ARC scopes) resident.

        A resident ledger is returned as is; otherwise the ledger is read from the
        record inside :func:`~clio_agent.arc.segment_cache.transient_reads`, so neither
        this set nor ARC keeps a copy. ``None`` when the session was never persisted.
        Search and recall read every session through this.
        """

        with self._lock:
            entry = self._resident.get(sid)
            if entry is not None:
                return entry.messages
        with transient_reads():
            return self._materialize(sid)

    def clear(self) -> None:
        """Drop every resident ledger (memory only; stored copies are untouched)."""

        with self._lock:
            self._resident.clear()
            self._total_bytes = 0

    def popitem(self) -> tuple[str, list["Message"]]:
        """Pop the most-recently-used RESIDENT ledger (memory only), or raise."""

        with self._lock:
            if not self._resident:
                raise KeyError("popitem(): no resident ledger")
            sid, entry = self._resident.popitem(last=True)
            self._total_bytes -= entry.nbytes
            return sid, entry.messages

    # ---- ARC residency (ScopeResidency) -------------------------------- #

    def scope_resident(self, session_id: str, scope: str, nbytes: int) -> None:
        """ARC cached (or re-committed) a scope of ``session_id``: count it."""

        with self._lock:
            entry = self._arc.get(session_id)
            if entry is None:
                entry = self._arc[session_id] = _ArcEntry({}, self._clock())
            self._arc_bytes += nbytes - entry.scopes.get(scope, 0)
            entry.scopes[scope] = nbytes
            entry.last_access = self._clock()
            over = self._over_caps()
        if over and self.on_over_budget is not None:
            self.on_over_budget()

    def scope_dropped(self, session_id: str, scope: str) -> None:
        """ARC dropped one cached scope."""

        with self._lock:
            entry = self._arc.get(session_id)
            if entry is not None:
                self._arc_bytes -= entry.scopes.pop(scope, 0)
                if not entry.scopes:
                    del self._arc[session_id]

    def session_released(self, session_id: str) -> None:
        """ARC dropped every cached scope of ``session_id``."""

        with self._lock:
            entry = self._arc.pop(session_id, None)
            if entry is not None:
                self._arc_bytes -= entry.nbytes

    def scope_touched(self, session_id: str) -> None:
        """A cached ARC scope of ``session_id`` was read."""

        entry = self._arc.get(session_id)
        if entry is not None:
            entry.last_access = self._clock()

    # ---- residency mechanics ------------------------------------------- #

    def _install(self, sid: str, messages: list["Message"], *, rehydrated: bool) -> None:
        """Cache a session's ledger, refresh accounting, and enforce the bounds."""

        nbytes = _estimate_bytes(messages)
        with self._lock:
            old = self._resident.pop(sid, None)
            if old is not None:
                self._total_bytes -= old.nbytes
            self._resident[sid] = _Entry(messages, nbytes, len(messages), self._clock())
            self._total_bytes += nbytes
        if rehydrated:
            self._audit(resident_ledger_reason_payload("rehydrate", session_id=sid, bytes=nbytes))
        self.sweep(protect=sid)

    def sweep(self, *, protect: str = "") -> int:
        """Release idle sessions, then the oldest idle ones until both caps hold.

        Sizes and ages are snapshotted under the lock; the activity checks and the
        evictions (``on_evict`` releases ARC) run after it is released, so this set never
        holds its lock while calling out. Returns how many sessions were evicted.
        """

        with self._lock:
            self._reconcile_bytes()
            sizes = {sid: e.nbytes for sid, e in self._resident.items()}
            ages = {sid: e.last_access for sid, e in self._resident.items()}
            for sid, arc in self._arc.items():
                sizes[sid] = sizes.get(sid, 0) + arc.nbytes
                ages[sid] = max(ages.get(sid, arc.last_access), arc.last_access)
        victims, over = self._pick_victims(sizes, ages, protect)
        for sid, reason in victims:
            self._evict(sid, reason)
        if over:
            self._audit(
                resident_ledger_reason_payload(
                    "eviction_skipped_all_active",
                    session_id=protect,
                    resident=len(sizes) - len(victims),
                    bytes=sum(sizes.values()),
                )
            )
        return len(victims)

    def _over_caps(self) -> bool:
        count = len(self._resident.keys() | self._arc.keys())
        return (
            self._total_bytes + self._arc_bytes > self._cfg.max_bytes
            or count > self._cfg.max_sessions
        )

    def _pick_victims(
        self, sizes: dict[str, int], ages: dict[str, float], protect: str
    ) -> tuple[list[tuple[str, str]], bool]:
        """Idle-TTL victims, then capacity victims oldest first; and whether the caps
        still do not hold because every remaining session is active."""

        now = self._clock()
        ordered = sorted(ages, key=ages.__getitem__)
        chosen: dict[str, str] = {}
        for sid in ordered:
            if sid == protect or now - ages[sid] <= self._cfg.idle_ttl_s:
                continue
            if not self._is_active(sid):
                chosen[sid] = "idle_ttl"
        total = sum(sizes.values()) - sum(sizes[sid] for sid in chosen)
        count = len(sizes) - len(chosen)
        for sid in ordered:
            if total <= self._cfg.max_bytes and count <= self._cfg.max_sessions:
                break
            if sid in chosen or sid == protect or self._is_active(sid):
                continue
            chosen[sid] = "capacity_bytes" if total > self._cfg.max_bytes else "capacity_count"
            total -= sizes[sid]
            count -= 1
        over = total > self._cfg.max_bytes or count > self._cfg.max_sessions
        return list(chosen.items()), over

    def _evict(self, sid: str, reason: str) -> None:
        with self._lock:
            entry = self._resident.pop(sid, None)
            arc = self._arc.pop(sid, None)
            released = (entry.nbytes if entry else 0) + (arc.nbytes if arc else 0)
            if entry is not None:
                self._total_bytes -= entry.nbytes
            if arc is not None:
                self._arc_bytes -= arc.nbytes
        if entry is None and arc is None:
            return
        self._audit(
            resident_ledger_reason_payload(
                reason, session_id=sid, bytes=released, arc_bytes=arc.nbytes if arc else 0
            )
        )
        if self._on_evict is not None:
            self._on_evict(sid)

    def _reconcile_bytes(self) -> None:
        """True up ledger accounting for in-place appends (under the lock).

        ``setdefault(sid, []).append`` grows a cached list without routing through
        :meth:`_install`; only entries whose length changed are re-measured.
        """

        total = 0
        for entry in self._resident.values():
            if len(entry.messages) != entry.nmsgs:
                entry.nbytes = _estimate_bytes(entry.messages)
                entry.nmsgs = len(entry.messages)
            total += entry.nbytes
        self._total_bytes = total

    # ---- introspection (tests / diagnostics) --------------------------- #

    @property
    def resident_session_ids(self) -> list[str]:
        """Sessions with a resident ledger, oldest → newest (LRU order)."""

        with self._lock:
            return list(self._resident.keys())

    @property
    def resident_count(self) -> int:
        """Number of session ledgers currently held in memory."""

        return len(self._resident)

    @property
    def resident_bytes(self) -> int:
        """Estimated resident transcript bytes (exact re-measure, for diagnostics)."""

        with self._lock:
            for entry in self._resident.values():
                entry.nbytes = _estimate_bytes(entry.messages)
                entry.nmsgs = len(entry.messages)
            self._total_bytes = sum(e.nbytes for e in self._resident.values())
            return self._total_bytes

    @property
    def arc_bytes(self) -> int:
        """Encoded bytes of the ARC scopes held in process (all sessions)."""

        return self._arc_bytes

    @property
    def arc_session_ids(self) -> list[str]:
        """Sessions with ARC scopes held in process."""

        with self._lock:
            return list(self._arc)


def _estimate_bytes(messages: list["Message"]) -> int:
    """Serialized-footprint proxy for a ledger's resident cost.

    Uses each message's JSON serialization (``model_dump_json``) so the byte cap —
    the PRIMARY memory guard — accounts for EVERY payload-bearing field, not just
    top-level ``part.text``: base64 image ``data``, tool ``input`` and nested
    ``content`` parts (where ``tool_result`` bodies live), ``unified_diff``
    file diffs, compaction ``summary``, ``thought``, and per-message
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


def _session_is_active(app: "FastAPI", sid: str) -> bool:
    """A session is pinned while it has a live SSE subscriber or running work."""

    bus = getattr(app.state, "bus", None)
    if bus is not None and bus.subscriber_count(sid) > 0:
        return True
    runner = getattr(app.state, "turn_runner", None)
    if runner is not None and runner.busy(sid):
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

    Also stamps the session's cumulative EventBus subscriber-queue-full drop count
    (#1214), reading the SAME ``app.state.bus`` :func:`_session_is_active` already
    consults for ``subscriber_count``. Piggybacking here — rather than a new route —
    makes "this session was evicted from resident memory while also dropping SSE
    events" visible in the existing ``ledger_evictions`` diagnostics ring; omitted
    when zero so a healthy session's rows stay unchanged.
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
    sid = str(payload.get("session_id") or "")
    bus = getattr(app.state, "bus", None) if sid else None
    dropped = bus.dropped_total(sid) if bus is not None else 0
    if dropped:
        payload = {**payload, "bus_dropped_total": dropped}
    store = getattr(app.state, "ledger_evictions", None)
    if store is None:
        store = deque(maxlen=512)
        app.state.ledger_evictions = store
    store.append(payload)


def build_resident_ledger_set(app: "FastAPI") -> ResidentLedgerSet:
    """Construct the app's resident set from its session index.

    Wires the active-session pin, the typed audit sink, the eviction hook (which
    releases the session's ARC scopes and the copies derived from it) and the periodic
    sweeper. ARC is attached later, once it exists (:func:`attach_arc_residency`).
    """

    from clio_agent.gact.evicted_session import (  # noqa: PLC0415 - avoid import cycle
        forget_evicted_session,
    )
    from clio_agent.gact.resident_sweeper import ResidentSweeper  # noqa: PLC0415
    from clio_agent.gact.transcript_projection import (  # noqa: PLC0415 - avoid import cycle
        materialize_ledger,
    )

    resident = ResidentLedgerSet(
        app.state.transcript_index,
        config=ResidentLedgerConfig.from_conf(),
        is_active=lambda sid: _session_is_active(app, sid),
        audit=lambda payload: _record_resident_audit(app, payload),
        materialize=lambda sid: materialize_ledger(app, sid),
        on_evict=lambda sid: forget_evicted_session(
            app, sid, is_active=lambda: _session_is_active(app, sid)
        ),
    )
    sweeper = ResidentSweeper(
        resident.sweep,
        interval_s=resident.config.sweep_interval_s,
        loop=lambda: getattr(app.state, "mcp_app_loop", None),
    )
    resident.on_over_budget = sweeper.request
    app.state.resident_sweeper = sweeper
    return resident


def attach_arc_residency(app: "FastAPI", arc: Any) -> None:
    """Report every scope ``arc`` caches to the app's resident set (the one owner)."""

    resident = getattr(app.state, "messages", None)
    segments = getattr(arc, "_segments", None)
    if isinstance(resident, ResidentLedgerSet) and segments is not None:
        segments.set_residency(resident)
        sweeper = getattr(app.state, "resident_sweeper", None)
        if sweeper is not None:
            sweeper.request()  # starts the periodic sweep on the server loop
