"""Lightweight scheduler for recurring/one-shot session turns (#21, #1081).

Each schedule pairs a cron expression (or a one-shot ``run_at``/``delay_s``)
with a question template + the session it fires under. A background asyncio
task ticks once a minute (:mod:`clio_agent.gact.scheduler_runtime`), decides
which schedules are due, and POSTs the resulting question through the same
``_start_background_user_turn`` path the regular HTTP handler uses.

No external cron dependency — we parse a 5-field cron expression ourselves
(minute hour day-of-month month day-of-week, each ``* | digit | comma-list |
A-B range | */N | A-B/N step``).

P4.3 (#1081) extends the original UTC-only matcher into a **local-timezone,
DST-correct** next-fire computation, adds one-shot ``run_at``/``delay_s`` with
auto-delete, deterministic anti-runaway clamps (min-interval floor, max-lifetime
ceiling, ``max_fires``/``until``), deterministic id-derived jitter, retry/backoff
on a failed fire, and an explicit overlap policy — each degradation emitting a
structured reason (the ``stream_fallback`` catalog style), never a silent drop.

Timezone / DST approach
-----------------------
:func:`next_fire` iterates the **naive local-wall-clock calendar** of the
schedule's timezone one minute at a time and materialises each matching minute
back to a UTC instant with ``fold=0``. Because the naive calendar lists every
wall-clock minute exactly once, this is DST-safe by construction:

* **Fall-back overlap** (a local minute occurs twice): only the first (``fold=0``)
  occurrence is produced, so the schedule fires exactly **once** — no double-fire.
* **Spring-forward gap** (a local minute does not occur): the naive minute still
  exists in the calendar; ``fold=0`` maps it through the pre-transition offset, so
  its UTC instant lands just after the jump and the schedule still fires — no skip.

The computation takes an **injected reference instant + tz**, so it is fully
unit-testable without touching the wall clock.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clio_agent import conf

logger = logging.getLogger(__name__)

#: How far ahead :func:`next_fire` scans for a match before giving up (a cron that
#: never matches — e.g. Feb 30 — returns ``None`` instead of looping forever).
_HORIZON_MINUTES = 366 * 24 * 60

#: Global anti-runaway floor: no recurring schedule may fire more often than this
#: (config ``scheduler.min_interval_s`` / env ``CLIO_SCHEDULER_MIN_INTERVAL_S``).
#: 60 s == the finest a 5-field cron can express, so a plain ``* * * * *`` is at the
#: floor, not below it.
_DEFAULT_MIN_INTERVAL_S = 60

#: Global anti-runaway ceiling: a recurring schedule with no explicit ``until`` is
#: auto-retired this many seconds after creation (config ``scheduler.max_lifetime_s``
#: / env ``CLIO_SCHEDULER_MAX_LIFETIME_S``). Default 30 days.
_DEFAULT_MAX_LIFETIME_S = 30 * 24 * 60 * 60

#: Deterministic id-derived jitter window (config ``scheduler.jitter_window_s`` / env
#: ``CLIO_SCHEDULER_JITTER_WINDOW_S``). Default 0 == disabled (fires exactly on the
#: minute boundary). When > 0 each schedule's fire instant is offset by a stable,
#: id-derived number of seconds in ``[0, window)`` — a thundering-herd guard for
#: sessions sharing one provider quota.
_DEFAULT_JITTER_WINDOW_S = 0

#: Consecutive failed fires tolerated before a schedule is disabled with a typed
#: reason (config ``scheduler.max_retries`` / env ``CLIO_SCHEDULER_MAX_RETRIES``).
_DEFAULT_MAX_RETRIES = 5

#: Base of the exponential retry backoff, in seconds.
_RETRY_BACKOFF_BASE_S = 60

#: Cap on a single retry backoff, in seconds (1 hour).
_RETRY_BACKOFF_CAP_S = 60 * 60

_OVERLAP_POLICIES = ("queue", "skip")
_CROSS_RUN_MEMORY = ("same_session", "fresh")
_NOTIFY_ON = ("failure", "always")


class CronError(ValueError):
    """A schedule create/update was rejected with a machine-readable ``reason``.

    The ``reason`` is a typed, model-reactable code (``invalid_cron``,
    ``min_interval_below_floor``, ``no_fire_within_horizon``, ``invalid_run_at``,
    ``missing_trigger``, ``ambiguous_trigger``, ``invalid_until``,
    ``invalid_timezone``, ``invalid_overlap_policy``) so callers/audit branch
    without string-matching the message — never a silent coercion.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class Schedule:
    """One scheduled turn. Persisted verbatim to ``schedules.json``.

    ``cron`` XOR ``run_at`` selects the trigger; ``recurring=False`` (implied by a
    ``run_at`` one-shot) auto-deletes the row after it fires once. ``timezone`` is an
    IANA name the 5-field cron is evaluated against (local wall clock). ``next_fire_at``
    is the computed UTC instant the tick compares ``now`` against.
    """

    id: str
    session_id: str
    cron: str = ""
    question: str = ""
    enabled: bool = True
    created_at: str = ""
    last_fired_at: str = ""
    fire_count: int = 0
    # --- P4.3 (#1081) trigger + clamps ---
    timezone: str = "UTC"
    recurring: bool = True
    run_at: str = ""
    next_fire_at: str = ""
    max_fires: int = 0
    until: str = ""
    min_interval_s: int = 0
    overlap_policy: str = "queue"
    # --- failure handling ---
    retry_count: int = 0
    last_error: str = ""
    notify_on: str = "failure"
    disabled_reason: str = ""
    # --- forward-compat seams (NOT wired into firing here) ---
    #: cron+GOAL binding (#1080): fire "until <goal> holds". The goal EVAL lands with
    #: P4.2 — this only stores the predicate + a TODO for the fire→evaluate→stop hook.
    goal: str = ""
    #: cross-run memory toggle (LangGraph thread-bound vs stateless): does a fire resume
    #: the same ARC-tracked session or start fresh? Stored; wiring is a P4.x seam.
    cross_run_memory: str = "same_session"
    #: spawn an AgentTask instead of posting a question (#948 AgentTask). Stored seam.
    spawn_task: bool = False

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso_utc(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 string to a UTC-aware datetime (``None`` on garbage)."""

    text = (value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# Config-resolved knobs (file -> env -> committed-default -> in-code default). #
# --------------------------------------------------------------------------- #
def min_interval_floor_s() -> int:
    return conf.resolve(
        "scheduler.min_interval_s",
        env="CLIO_SCHEDULER_MIN_INTERVAL_S",
        default=_DEFAULT_MIN_INTERVAL_S,
        cast=conf.as_int,
    )


def max_lifetime_s() -> int:
    return conf.resolve(
        "scheduler.max_lifetime_s",
        env="CLIO_SCHEDULER_MAX_LIFETIME_S",
        default=_DEFAULT_MAX_LIFETIME_S,
        cast=conf.as_int,
    )


def jitter_window_s() -> int:
    return conf.resolve(
        "scheduler.jitter_window_s",
        env="CLIO_SCHEDULER_JITTER_WINDOW_S",
        default=_DEFAULT_JITTER_WINDOW_S,
        cast=conf.as_int,
    )


def max_retries() -> int:
    return conf.resolve(
        "scheduler.max_retries",
        env="CLIO_SCHEDULER_MAX_RETRIES",
        default=_DEFAULT_MAX_RETRIES,
        cast=conf.as_int,
    )


def default_timezone_name() -> str:
    """Resolve the server's default schedule timezone (never a silent UTC swap).

    Precedence: explicit config/env (``scheduler.timezone`` / ``CLIO_SCHEDULER_TZ`` /
    ``TZ``) -> the system local IANA zone via :mod:`tzlocal` -> ``UTC`` with a typed
    ``scheduler_tz_fallback_utc`` warning. Returning the true *local* IANA name (not a
    fixed offset) is what makes ``0 9 * * *`` DST-correct on Windows.
    """

    explicit = conf.resolve(
        "scheduler.timezone", env="CLIO_SCHEDULER_TZ", default="", cast=conf.as_str
    )
    if not explicit:
        explicit = (conf.resolve("scheduler.tz", env="TZ", default="", cast=conf.as_str) or "").strip()
    if explicit:
        try:
            ZoneInfo(explicit)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning(
                "scheduler timezone: configured zone unusable "
                "reason=scheduler_tz_config_invalid tz=%s",
                explicit,
            )
        else:
            return explicit
    try:
        import tzlocal  # noqa: PLC0415

        name = str(tzlocal.get_localzone_name() or "").strip()
        if name:
            ZoneInfo(name)
            return name
    except Exception as exc:  # noqa: BLE001 - tzlocal missing/unresolvable -> typed fallback below
        logger.warning(
            "scheduler timezone: local zone unresolved, falling back to UTC "
            "reason=scheduler_tz_fallback_utc error=%r",
            exc,
        )
        return "UTC"
    logger.warning(
        "scheduler timezone: local zone name empty, falling back to UTC "
        "reason=scheduler_tz_fallback_utc"
    )
    return "UTC"


def resolve_timezone(name: str) -> ZoneInfo:
    """Return the ``ZoneInfo`` for ``name`` (raising :class:`CronError` on garbage)."""

    text = (name or "").strip() or default_timezone_name()
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronError(
            f"unknown timezone {text!r} (expected an IANA name like 'America/Chicago')",
            reason="invalid_timezone",
        ) from exc


# --------------------------------------------------------------------------- #
# 5-field cron parsing (with ranges) + DST-safe next-fire.                     #
# --------------------------------------------------------------------------- #
def _match_token(field_value: int, token: str) -> bool:
    """Match one comma-free token: ``*``, ``A``, ``A-B``, ``*/N`` or ``A-B/N``."""

    base, _, step_text = token.partition("/")
    step = 1
    if step_text:
        step = int(step_text)  # ValueError bubbles to _validate/_matches caller
        if step <= 0:
            raise ValueError(f"non-positive cron step in {token!r}")
    if base == "*":
        return step == 1 or (field_value % step == 0)
    if "-" in base:
        lo_text, _, hi_text = base.partition("-")
        lo, hi = int(lo_text), int(hi_text)
        if lo > hi:
            raise ValueError(f"inverted cron range {base!r}")
        return lo <= field_value <= hi and (field_value - lo) % step == 0
    value = int(base)
    if step_text:  # `A/N` == `A-max/N`; treat bare `A/N` as "from A stepping N"
        return field_value >= value and (field_value - value) % step == 0
    return field_value == value


def _matches(field_value: int, expr: str) -> bool:
    """Match a full cron field (a comma list of tokens). Returns ``False`` on any
    unparseable token rather than raising — :func:`validate_cron` is the strict gate."""

    try:
        for token in expr.split(","):
            if _match_token(field_value, token):
                return True
    except ValueError:
        return False
    return False


def cron_matches(cron: str, when: datetime) -> bool:
    """Return True when ``when``'s wall-clock fields match a 5-field cron expression.

    ``when`` is compared by its naive calendar fields (minute/hour/day/month/weekday);
    the caller is responsible for having ``when`` already be in the target timezone.
    Day-of-week is 0=Sun .. 6=Sat (cron convention)."""

    parts = cron.split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    return (
        _matches(when.minute, minute)
        and _matches(when.hour, hour)
        and _matches(when.day, dom)
        and _matches(when.month, month)
        and _matches((when.weekday() + 1) % 7, dow)
    )


_FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


def validate_cron(cron: str) -> None:
    """Raise :class:`CronError(reason='invalid_cron')` when ``cron`` is malformed.

    Checks the 5-field shape and that every token parses and stays within the field's
    numeric bounds (so ``99 * * * *`` or ``abc * * * *`` are rejected up front, not
    silently never-matching)."""

    parts = cron.split()
    if len(parts) != 5:
        raise CronError(
            f"cron must have exactly 5 fields (min hour dom month dow), got {len(parts)}: {cron!r}",
            reason="invalid_cron",
        )
    for expr, (lo, hi) in zip(parts, _FIELD_BOUNDS, strict=True):
        for token in expr.split(","):
            base, _, step_text = token.partition("/")
            try:
                if step_text and int(step_text) <= 0:
                    raise ValueError
                if base == "*":
                    continue
                for edge in base.split("-", 1):
                    val = int(edge)
                    if not (lo <= val <= hi):
                        raise ValueError
                if "-" in base:
                    a, b = (int(x) for x in base.split("-", 1))
                    if a > b:
                        raise ValueError
            except ValueError as exc:
                raise CronError(
                    f"unparseable cron token {token!r} in field {expr!r} (bounds {lo}-{hi})",
                    reason="invalid_cron",
                ) from exc


def jitter_seconds(schedule_id: str, window_s: int) -> int:
    """Deterministic id-derived jitter offset in ``[0, window_s)`` (0 when disabled).

    Pure function of the schedule id — the SAME id always yields the SAME offset, so a
    daemon restart re-derives identical fire instants (no drift), while distinct ids
    spread across the window (thundering-herd guard)."""

    if window_s <= 0:
        return 0
    digest = hashlib.sha256(schedule_id.encode("utf-8")).hexdigest()
    return int(digest, 16) % window_s


def next_fire(
    cron: str,
    ref: datetime,
    tz: ZoneInfo,
    *,
    jitter_s: int = 0,
    horizon_minutes: int = _HORIZON_MINUTES,
) -> Optional[datetime]:
    """The next UTC instant at/after ``ref`` whose wall clock in ``tz`` matches ``cron``.

    ``ref`` must be a UTC-aware datetime (the injected reference — no wall-clock read).
    Returns ``None`` when no minute in ``horizon_minutes`` matches. DST-safe: see the
    module docstring. ``jitter_s`` is added to the matched minute boundary."""

    naive = ref.astimezone(tz).replace(second=0, microsecond=0, tzinfo=None)
    for _ in range(horizon_minutes):
        if cron_matches(cron, naive):
            fire = naive.replace(tzinfo=tz, fold=0).astimezone(timezone.utc)
            return fire + timedelta(seconds=jitter_s)
        naive += timedelta(minutes=1)
    return None


class ScheduleStore:
    """Thread-safe schedule registry with optional JSON persistence."""

    def __init__(self, *, path: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._path = path
        self._schedules: dict[str, Schedule] = {}
        self._field_names = {f.name for f in fields(Schedule)}
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            import json  # noqa: PLC0415

            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - corrupt store restarts empty, but say so
            logger.warning(
                "schedule store: unreadable persistence restarted empty "
                "reason=schedule_store_corrupt path=%s error=%s",
                self._path,
                exc,
            )
            return
        for row in data.get("schedules", []):
            try:
                # Filter unknown keys so a forward-version row (or a rollback) drops
                # extra fields instead of crashing the whole load; a missing REQUIRED
                # field (id/session_id) still raises -> that one row is dropped below.
                known = {k: v for k, v in row.items() if k in self._field_names}
                self._schedules[known["id"]] = Schedule(**known)
            except Exception as exc:  # noqa: BLE001 - drop only the bad row, but say so
                logger.warning(
                    "schedule store: dropping malformed row "
                    "reason=schedule_row_invalid schedule_id=%s error=%s",
                    row.get("id") if isinstance(row, dict) else None,
                    exc,
                )
                continue

    def _flush(self) -> None:
        if self._path is None:
            return
        import json  # noqa: PLC0415

        self._path.parent.mkdir(parents=True, exist_ok=True)
        rows = [s.to_wire() for s in self._schedules.values()]
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"schedules": rows}, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # -- creation ---------------------------------------------------------- #
    def create(
        self,
        *,
        session_id: str,
        question: str,
        cron: str = "",
        run_at: str = "",
        delay_s: int = 0,
        recurring: bool = True,
        timezone_name: str = "",
        max_fires: int = 0,
        until: str = "",
        min_interval_s: int = 0,
        overlap_policy: str = "queue",
        notify_on: str = "failure",
        cross_run_memory: str = "same_session",
        spawn_task: bool = False,
        goal: str = "",
        now: Optional[datetime] = None,
    ) -> Schedule:
        """Validate + register a schedule, computing its first ``next_fire_at``.

        Raises :class:`CronError` (mutating nothing) on any typed rejection: a
        malformed cron, an absent/ambiguous trigger, a sub-floor interval, a bad
        timezone/until/overlap-policy. One-shot (``run_at``/``delay_s``) forces
        ``recurring=False``; ``now`` is injectable for deterministic tests."""

        ref = (now or _utcnow()).astimezone(timezone.utc)
        tz = resolve_timezone(timezone_name)
        overlap = (overlap_policy or "queue").strip().lower()
        if overlap not in _OVERLAP_POLICIES:
            raise CronError(
                f"overlap_policy must be one of {list(_OVERLAP_POLICIES)}, got {overlap!r}",
                reason="invalid_overlap_policy",
            )
        cron = (cron or "").strip()
        run_at = (run_at or "").strip()
        one_shot = bool(run_at) or delay_s > 0
        if one_shot:
            recurring = False

        # Resolve the trigger -> first fire instant (with jitter).
        sid = "sched_" + uuid.uuid4().hex[:12]
        window = jitter_window_s()
        jitter = jitter_seconds(sid, window)
        first_fire, resolved_run_at = self._resolve_first_fire(
            sid, cron, run_at, delay_s, ref, tz, jitter
        )

        # Clamp: min-interval floor (recurring cron only — a one-shot has no interval).
        effective_floor = max(min_interval_floor_s(), int(min_interval_s or 0))
        if recurring and cron:
            second = next_fire(cron, first_fire + timedelta(minutes=1), tz, jitter_s=jitter)
            if second is not None:
                gap = (second - first_fire).total_seconds()
                if gap < effective_floor:
                    raise CronError(
                        f"schedule interval {int(gap)}s is below the min-interval floor "
                        f"{effective_floor}s (anti-runaway clamp) — coarsen the cron",
                        reason="min_interval_below_floor",
                    )

        # Clamp: max-lifetime ceiling. Explicit `until` wins; otherwise a recurring
        # schedule is auto-retired after the configured lifetime.
        until_dt = self._resolve_until(until, ref, recurring)

        sch = Schedule(
            id=sid,
            session_id=session_id,
            cron=cron,
            question=question,
            created_at=_iso(ref),
            timezone=tz.key,
            recurring=recurring,
            run_at=resolved_run_at,
            next_fire_at=_iso(first_fire),
            max_fires=int(max_fires or 0),
            until=_iso(until_dt) if until_dt else "",
            min_interval_s=int(min_interval_s or 0),
            overlap_policy=overlap,
            notify_on=(notify_on or "failure").strip().lower()
            if (notify_on or "").strip().lower() in _NOTIFY_ON
            else "failure",
            cross_run_memory=(cross_run_memory or "same_session").strip().lower()
            if (cross_run_memory or "").strip().lower() in _CROSS_RUN_MEMORY
            else "same_session",
            spawn_task=bool(spawn_task),
            goal=(goal or "").strip(),
        )
        with self._lock:
            self._schedules[sid] = sch
            self._flush()
        return sch

    def _resolve_first_fire(
        self,
        sid: str,
        cron: str,
        run_at: str,
        delay_s: int,
        ref: datetime,
        tz: ZoneInfo,
        jitter: int,
    ) -> tuple[datetime, str]:
        """Compute the first fire instant + normalized run_at for a create()."""

        triggers = sum(bool(x) for x in (cron, run_at, delay_s > 0))
        if triggers == 0:
            raise CronError(
                "a schedule needs a trigger: a cron expression, a run_at instant, or a delay_s",
                reason="missing_trigger",
            )
        if triggers > 1:
            raise CronError(
                "a schedule takes exactly ONE trigger (cron XOR run_at XOR delay_s)",
                reason="ambiguous_trigger",
            )
        if delay_s > 0:
            fire = ref + timedelta(seconds=int(delay_s))
            return fire, _iso(fire)
        if run_at:
            try:
                parsed = datetime.fromisoformat(run_at.strip())
            except ValueError as exc:
                raise CronError(f"unparseable run_at {run_at!r}", reason="invalid_run_at") from exc
            # A naive run_at is interpreted in the schedule's timezone (local wall clock).
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=tz)
            fire = parsed.astimezone(timezone.utc)
            return fire, _iso(fire)
        validate_cron(cron)
        cron_fire = next_fire(cron, ref, tz, jitter_s=jitter)
        if cron_fire is None:
            raise CronError(
                f"cron {cron!r} has no fire time within {_HORIZON_MINUTES // (24 * 60)} days",
                reason="no_fire_within_horizon",
            )
        return cron_fire, ""

    def _resolve_until(self, until: str, ref: datetime, recurring: bool) -> Optional[datetime]:
        """Resolve the effective end instant (explicit until, else lifetime ceiling)."""

        if until.strip():
            parsed = _parse_iso_utc(until)
            if parsed is None:
                raise CronError(f"unparseable until {until!r}", reason="invalid_until")
            return parsed
        if recurring:
            return ref + timedelta(seconds=max_lifetime_s())
        return None

    def add(self, *, session_id: str, cron: str, question: str) -> Schedule:
        """Legacy convenience: a recurring cron schedule with server defaults (#21).

        Delegates to :meth:`create` so the HTTP route and older callers get the P4.3
        timezone + clamp + next-fire behaviour without re-implementing it."""

        return self.create(session_id=session_id, cron=cron, question=question)

    # -- reads ------------------------------------------------------------- #
    def get(self, sid: str) -> Optional[Schedule]:
        with self._lock:
            return self._schedules.get(sid)

    def list(self, *, session_id: Optional[str] = None) -> list[Schedule]:
        with self._lock:
            rows = list(self._schedules.values())
        if session_id is not None:
            rows = [r for r in rows if r.session_id == session_id]
        return sorted(rows, key=lambda s: s.created_at)

    def delete(self, sid: str) -> bool:
        with self._lock:
            existed = sid in self._schedules
            self._schedules.pop(sid, None)
            self._flush()
        return existed

    # -- fire lifecycle ---------------------------------------------------- #
    def mark_fired(self, sid: str, *, now: Optional[datetime] = None) -> None:
        """Record a successful fire and advance/retire the schedule.

        One-shot (``recurring=False``) rows auto-delete; a ``max_fires`` ceiling
        disables with a typed reason; otherwise the next fire is recomputed from the
        moment of firing (DST-safe). ``now`` is injectable for tests."""

        ref = (now or _utcnow()).astimezone(timezone.utc)
        with self._lock:
            sch = self._schedules.get(sid)
            if sch is None:
                return
            sch.last_fired_at = _iso(ref)
            sch.fire_count += 1
            sch.retry_count = 0
            sch.last_error = ""
            if not sch.recurring:
                self._schedules.pop(sid, None)
                self._flush()
                return
            if sch.max_fires and sch.fire_count >= sch.max_fires:
                sch.enabled = False
                sch.next_fire_at = ""
                sch.disabled_reason = "max_fires_reached"
                logger.info(
                    "scheduler: schedule retired reason=max_fires_reached "
                    "schedule_id=%s fire_count=%s",
                    sid,
                    sch.fire_count,
                )
                self._flush()
                return
            self._recompute_next_fire(sch, ref)
            self._flush()

    def record_fire_failure(self, sid: str, error: str, *, now: Optional[datetime] = None) -> None:
        """Deferred-not-dropped retry/backoff for a failed fire (never silent).

        Increments the retry counter, schedules the next attempt after an exponential
        backoff, and — once :func:`max_retries` is exhausted — disables the schedule
        with a typed ``max_retries_exceeded`` reason instead of hammering forever."""

        ref = (now or _utcnow()).astimezone(timezone.utc)
        with self._lock:
            sch = self._schedules.get(sid)
            if sch is None:
                return
            sch.retry_count += 1
            sch.last_error = str(error)[:500]
            ceiling = max_retries()
            if sch.retry_count > ceiling:
                sch.enabled = False
                sch.next_fire_at = ""
                sch.disabled_reason = "max_retries_exceeded"
                logger.warning(
                    "scheduler: schedule disabled reason=max_retries_exceeded "
                    "schedule_id=%s retries=%s error=%s",
                    sid,
                    sch.retry_count,
                    sch.last_error,
                )
                self._flush()
                return
            backoff = min(
                _RETRY_BACKOFF_CAP_S, _RETRY_BACKOFF_BASE_S * (2 ** (sch.retry_count - 1))
            )
            sch.next_fire_at = _iso(ref + timedelta(seconds=backoff))
            logger.info(
                "scheduler: fire retry scheduled reason=schedule_fire_retry "
                "schedule_id=%s attempt=%s backoff_s=%s",
                sid,
                sch.retry_count,
                backoff,
            )
            self._flush()

    def _recompute_next_fire(self, sch: Schedule, ref: datetime) -> None:
        """Advance ``sch.next_fire_at`` to the next matching instant strictly after a fire."""

        if not sch.cron:
            # A recurring row with no cron cannot recur; retire it truthfully.
            sch.enabled = False
            sch.next_fire_at = ""
            sch.disabled_reason = "no_recurring_trigger"
            return
        tz = resolve_timezone(sch.timezone)
        jitter = jitter_seconds(sch.id, jitter_window_s())
        nxt = next_fire(sch.cron, ref + timedelta(minutes=1), tz, jitter_s=jitter)
        sch.next_fire_at = _iso(nxt) if nxt else ""
        if nxt is None:
            sch.enabled = False
            sch.disabled_reason = "no_fire_within_horizon"

    def _retire_if_expired(self, sch: Schedule, when: datetime) -> bool:
        """Disable + typed-reason a schedule past its ``until``/lifetime. Returns True
        (skip firing) when retired. Caller holds the lock."""

        until_dt = _parse_iso_utc(sch.until)
        if until_dt is not None and when >= until_dt:
            sch.enabled = False
            sch.next_fire_at = ""
            sch.disabled_reason = "until_reached"
            logger.info(
                "scheduler: schedule retired reason=until_reached schedule_id=%s until=%s",
                sch.id,
                sch.until,
            )
            return True
        return False

    def due_now(self, when: datetime) -> Iterable[Schedule]:
        """Yield every enabled schedule whose ``next_fire_at`` has arrived.

        Retires (disables + typed reason) any schedule past its ``until``/lifetime
        instead of firing it. ``when`` is the injected/observed UTC instant."""

        when = when.astimezone(timezone.utc)
        due: list[Schedule] = []
        dirty = False
        with self._lock:
            for sch in self._schedules.values():
                if not sch.enabled:
                    continue
                fire_at = _parse_iso_utc(sch.next_fire_at)
                if fire_at is None or when < fire_at:
                    continue
                if self._retire_if_expired(sch, when):
                    dirty = True
                    continue
                due.append(sch)
            if dirty:
                self._flush()
        yield from due
