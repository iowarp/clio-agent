"""Autonomous cross-turn LOOP — self-paced iteration with first-class typed bounds (#1079).

clio has no cross-turn self-paced loop: ``loop_inbox`` injects mid-turn *events*,
``max_iters`` bounds ONE turn's ReAct steps, and ``workflows`` is a one-way DAG with no
cycle edge. This module adds the missing primitive — a loop that **re-drives a session's
turn repeatedly** toward continued work — by **reusing the P4.3 scheduler** (#1081):

* **Self-pace via the scheduler one-shot.** :func:`_arm` schedules the loop's ``prompt``
  as a ``recurring=False`` / ``delay_s`` one-shot on the SAME
  :class:`~clio_agent.gact.scheduler.ScheduleStore` cron drives. When it fires,
  :func:`clio_agent.gact.scheduler_runtime._fire_schedule` stages the prompt as a
  background user turn (the re-drive seam) exactly like a scheduled cron turn — no
  parallel scheduler is built. The ``loop_wakeup`` model tool is the self-pace control
  (the ``ScheduleWakeup`` analog): reschedule yourself after a delay, or ``stop:true``.

* **First-class typed bounds (NOT prose).** :func:`_check_bounds` enforces ``max_iters``,
  ``max_wallclock_s``, ``max_tokens`` / ``max_usd`` (token accounting is available via the
  session rollup), and **no-progress detection** — the same activity signal
  :mod:`clio_agent.gact.workflow_step_watch` trusts
  (:func:`~clio_agent.gact.workflow_step_watch.step_activity_monotonic`), not a wall-clock
  bound. Every stop emits a **typed reason** (:data:`LOOP_STOP_REASONS`, the
  ``stream_fallback`` catalog convention), never a silent halt.

* **Bounded fallback.** If a loop iteration ends calling NEITHER a fresh ``loop_wakeup``
  NOR ``stop:true``, :func:`dispatch_loop_at_finalize` schedules exactly ONE fallback
  wakeup (a fixed delay) and ENDS the loop with ``loop_no_reschedule`` if that iteration
  also does not reschedule — never an unbounded silent wait.

* **Cancel-both.** A ``stop:true``, a tripped bound, ending/cancelling the session
  (:func:`stop_session_loop`) — critically for the daemon that outlives clients — and a
  **restart** (:func:`start_loop` called while a prior loop is still active) all cancel
  the pending wakeup through :func:`clio_agent.gact.cron_tools.cancel_schedule` (store row
  + daemon deferred entry), so no orphan wakeup burns tokens unattended.

Loop state lives on ``session.metadata["loop"]`` (#948 pattern — no fifth store). The
module is a stdlib+leaf: it imports the scheduler cancel helper, the workflow activity
probe, and context; it never imports :mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Optional

from clio_agent.gact import context as _ctx
from clio_agent.gact.cron_tools import cancel_schedule
from clio_agent.gact.workflow_step_watch import step_activity_monotonic
from clio_agent.runtime import trace

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Anti-runaway constants. Every loop ALWAYS carries a finite ceiling so it can  #
# never run away even when the model/user omits a bound (⚑ never-runaway).      #
# --------------------------------------------------------------------------- #
#: ``loop_wakeup`` delay clamp window (seconds) — a min-interval floor + a max
#: ceiling, the field-wide #1 anti-runaway mechanism (Claude ``/loop``: 1-min min).
WAKEUP_MIN_S = 60
WAKEUP_MAX_S = 3600
#: The fixed delay for the single bounded-fallback wakeup.
FALLBACK_DELAY_S = 60
#: Default self-pace interval when none is given at ``/loop`` start.
DEFAULT_INTERVAL_S = 300
#: Hard iteration ceiling applied when ``max_iters`` is unset (0). A loop with no
#: explicit iteration bound still terminates.
DEFAULT_MAX_ITERS = 100
#: Hard wall-clock ceiling (seconds) applied when ``max_wallclock_s`` is unset.
DEFAULT_MAX_WALLCLOCK_S = 24 * 60 * 60
#: Consecutive no-progress iterations tolerated before ``loop_stalled``.
DEFAULT_MAX_NO_PROGRESS = 3

#: Typed stop reasons (the ``stream_fallback`` catalog convention): every loop halt
#: records exactly one so audit/trace branch on a code, never on prose.
LOOP_STOP_REASONS = (
    "loop_max_iters",  # iteration ceiling reached
    "loop_budget",  # wall-clock / token / usd budget exhausted
    "loop_stalled",  # N consecutive iterations made no observable progress
    "loop_user_stopped",  # explicit loop_wakeup(stop=True)
    "loop_goal_met",  # a declared goal predicate held (#1080 seam)
    "loop_no_reschedule",  # the bounded fallback fired once and still no reschedule
    "loop_session_ended",  # session end/cancel cancelled the loop (cancel-both)
    "loop_restarted",  # a new start_loop() superseded a still-active prior loop
)

#: Typed delay-clamp reasons (never a silent clamp).
CLAMP_FLOOR = "loop_delay_clamped_floor"
CLAMP_CEILING = "loop_delay_clamped_ceiling"


class LoopError(ValueError):
    """A ``/loop`` start (or ``loop_wakeup``) was rejected with a machine-readable
    ``reason`` (``loop_missing_prompt``, ``no_active_session``) so callers branch on a
    code rather than string-matching the message — never a silent coercion."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# --------------------------------------------------------------------------- #
# Loop state on session.metadata["loop"] (no fifth store).                      #
# --------------------------------------------------------------------------- #
def _get_loop(app: Any, sid: str) -> dict[str, Any]:
    """Read the loop state dict off ``session.metadata`` (``{}`` when absent)."""

    sess = app.state.sessions.get(sid)
    if sess is None:
        return {}
    meta = getattr(sess, "metadata", None) or {}
    loop = meta.get("loop") if isinstance(meta, Mapping) else None
    return dict(loop) if isinstance(loop, Mapping) else {}


def _put_loop(app: Any, sid: str, loop: dict[str, Any]) -> None:
    """Persist the loop state dict as a whole under ``metadata["loop"]``.

    ``SessionStore.update`` does a SHALLOW merge, so writing the whole ``loop`` dict
    replaces the prior one wholesale (no stale sub-keys)."""

    app.state.sessions.update(sid, metadata_patch={"loop": loop})


def _active() -> tuple[Any, str]:
    """Resolve (app, session_id) for a loop tool call (typed error when absent)."""

    app = _ctx.active_app()
    sid = _ctx.active_session_id()
    if app is None or not sid:
        raise LoopError(
            "loop tools require an active CLIO app/session context.",
            reason="no_active_session",
        )
    return app, sid


def clamp_delay(delay_seconds: int) -> tuple[int, str]:
    """Clamp ``delay_seconds`` into ``[WAKEUP_MIN_S, WAKEUP_MAX_S]``.

    Returns ``(applied, reason)`` where ``reason`` is a typed clamp code when the value
    was clamped (never silent), else ``""``."""

    value = int(delay_seconds or 0)
    if value < WAKEUP_MIN_S:
        return WAKEUP_MIN_S, CLAMP_FLOOR
    if value > WAKEUP_MAX_S:
        return WAKEUP_MAX_S, CLAMP_CEILING
    return value, ""


def _parse_iso(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
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
# Arm / end (both reuse the P4.3 scheduler + its cancel-both).                  #
# --------------------------------------------------------------------------- #
def _arm(app: Any, sid: str, loop: dict[str, Any], prompt: str, delay_s: int) -> str:
    """Schedule the loop's ``prompt`` as a one-shot wakeup ``delay_s`` from now.

    REUSE of the P4.3 scheduler: a ``recurring=False`` / ``delay_s`` one-shot on
    ``app.state.schedules`` (:class:`~clio_agent.gact.scheduler.ScheduleStore`). When it
    fires, :func:`clio_agent.gact.scheduler_runtime._fire_schedule` stages ``prompt`` as a
    background user turn (the cross-turn re-drive seam) — no parallel scheduler. Cancels
    any prior pending wakeup first (cancel-both) so at most one loop wakeup is ever armed.
    Returns the next-fire UTC instant. Persists the mutated ``loop``."""

    old = str(loop.get("pending_schedule_id") or "")
    if old:
        cancel_schedule(app, old)
    sch = app.state.schedules.create(
        session_id=sid,
        question=prompt,
        delay_s=int(delay_s),
        recurring=False,
    )
    loop["pending_schedule_id"] = sch.id
    loop["armed"] = True
    loop["prompt"] = prompt
    loop["fallback_pending"] = False
    _put_loop(app, sid, loop)
    return str(sch.next_fire_at or "")


def end_loop(
    app: Any,
    sid: str,
    *,
    reason: str,
    loop: Optional[dict[str, Any]] = None,
    detail: str = "",
) -> None:
    """End the loop with a typed ``reason`` and cancel its pending wakeup (cancel-both).

    Idempotent: a no-op when there is no loop or it is already stopped. Cancelling the
    pending schedule closes the orphan window (store row + daemon deferred entry)."""

    if loop is None:
        loop = _get_loop(app, sid)
    if not loop or loop.get("stopped"):
        return
    pending = str(loop.get("pending_schedule_id") or "")
    if pending:
        cancel_schedule(app, pending)  # cancel-both
    loop["active"] = False
    loop["stopped"] = True
    loop["stop_reason"] = reason
    loop["armed"] = False
    loop["pending_schedule_id"] = ""
    loop["fallback_pending"] = False
    if app.state.sessions.get(sid) is not None:
        _put_loop(app, sid, loop)
    logger.info(
        "loop stopped reason=%s loop_id=%s iteration=%s detail=%s",
        reason,
        loop.get("loop_id"),
        loop.get("iteration"),
        detail,
    )
    trace.event(
        "LOOP",
        "loop %s stopped reason=%s iteration=%s",
        loop.get("loop_id"),
        reason,
        loop.get("iteration"),
    )


def stop_session_loop(app: Any, sid: str, *, reason: str = "loop_session_ended") -> None:
    """Cancel-both entry for session end / user cancel — the daemon outlives clients.

    Ending or cancelling a session MUST cancel its background loop wakeup, or an
    orphaned re-fire keeps burning tokens unattended (the ``ScheduleWakeup``-survived-
    Ctrl+C incident; clio is more exposed). Delegates to :func:`end_loop` (idempotent)."""

    end_loop(app, sid, reason=reason)


# --------------------------------------------------------------------------- #
# Typed bounds (first-class, enforced deterministically — NOT prose).          #
# --------------------------------------------------------------------------- #
def _loop_goal_met(app: Any, sid: str, loop: dict[str, Any]) -> bool:
    """Seam for the #1080 goal stop-condition — always ``False`` here.

    Goal EVALUATION (the LLM first-pass + deterministic hard gate) lands with P4.2
    (#1080); this is the compose point so a satisfied goal ends the loop with the typed
    ``loop_goal_met`` reason.
    TODO(#1080): evaluate the declared goal predicate against the transcript here.
    """

    return False


def _check_bounds(app: Any, sid: str, loop: dict[str, Any]) -> str:
    """Return the typed stop reason for the FIRST tripped bound, or ``""``.

    Evaluated against ``loop``'s already-incremented ``iteration`` /
    ``no_progress_count`` and the live session token/cost rollup. Order: goal seam ->
    iters -> wall-clock -> tokens/usd -> stall."""

    if _loop_goal_met(app, sid, loop):
        return "loop_goal_met"
    if int(loop.get("iteration", 0)) >= int(loop.get("max_iters", DEFAULT_MAX_ITERS)):
        return "loop_max_iters"
    created = _parse_iso(loop.get("created_at"))
    wall = float(loop.get("max_wallclock_s") or 0.0)
    if wall > 0 and created is not None:
        if (datetime.now(timezone.utc) - created).total_seconds() >= wall:
            return "loop_budget"
    sess = app.state.sessions.get(sid)
    if sess is not None:
        max_tokens = int(loop.get("max_tokens") or 0)
        if max_tokens > 0:
            used = int(getattr(sess, "tokens_input", 0)) + int(getattr(sess, "tokens_output", 0))
            tokens_at_start = int(loop.get("tokens_at_start") or 0)
            if used - tokens_at_start >= max_tokens:
                return "loop_budget"
        max_usd = float(loop.get("max_usd") or 0.0)
        if max_usd > 0:
            cost_at_start = float(loop.get("cost_at_start") or 0.0)
            if float(getattr(sess, "cost_usd", 0.0)) - cost_at_start >= max_usd:
                return "loop_budget"
    if int(loop.get("no_progress_count", 0)) >= int(loop.get("max_no_progress", DEFAULT_MAX_NO_PROGRESS)):
        return "loop_stalled"
    return ""


def _record_progress(app: Any, sid: str, loop: dict[str, Any]) -> None:
    """Fold the iteration that just ran into the no-progress counter.

    Uses the SAME activity probe the declared-workflow step watch trusts
    (:func:`~clio_agent.gact.workflow_step_watch.step_activity_monotonic`): the session's
    bus heartbeat (plus an in-flight LM call), NOT a wall-clock bound — a legitimately
    heavy iteration that keeps publishing is not stalled. When the heartbeat has not
    advanced since the previous iteration, the iteration made no observable progress."""

    activity = step_activity_monotonic(app, sid, now=time.monotonic())
    last = float(loop.get("last_activity_monotonic") or 0.0)
    if activity > last:
        loop["no_progress_count"] = 0
    else:
        loop["no_progress_count"] = int(loop.get("no_progress_count", 0)) + 1
    loop["last_activity_monotonic"] = activity


# --------------------------------------------------------------------------- #
# Start (the /loop command + a model self-initiated loop).                      #
# --------------------------------------------------------------------------- #
def start_loop(
    app: Any,
    sid: str,
    *,
    prompt: str,
    interval_s: int = 0,
    max_iters: int = 0,
    max_wallclock_s: float = 0.0,
    max_tokens: int = 0,
    max_usd: float = 0.0,
    max_no_progress: int = 0,
) -> dict[str, Any]:
    """Initialise loop state on ``session.metadata`` and arm the first wakeup.

    Typed bounds are settable here (the ``/loop`` start); an unset bound (0) resolves to
    a finite hard default so the loop can never run away. If a PRIOR loop is still active
    on this session (a restart — a user re-issuing ``/loop``, or a model self-initiate
    racing an existing loop), its pending wakeup is cancelled first (``loop_restarted``,
    cancel-both) so the old one-shot never survives as an orphan. ``max_tokens`` /
    ``max_usd`` bound the tokens/cost this loop itself spends: the session's cumulative
    rollup is snapshotted here (``tokens_at_start`` / ``cost_at_start``) so
    :func:`_check_bounds` compares the DELTA, not the session's lifetime total. Raises
    :class:`LoopError` on an empty prompt. Returns a summary dict (loop_id, next_fire_at,
    resolved bounds)."""

    text = (prompt or "").strip()
    if not text:
        raise LoopError(
            "a loop needs a prompt to re-drive each iteration", reason="loop_missing_prompt"
        )
    delay, _clamp = clamp_delay(interval_s or DEFAULT_INTERVAL_S)
    eff_iters = int(max_iters) if int(max_iters or 0) > 0 else DEFAULT_MAX_ITERS
    eff_wall = float(max_wallclock_s) if float(max_wallclock_s or 0.0) > 0 else DEFAULT_MAX_WALLCLOCK_S
    eff_stall = int(max_no_progress) if int(max_no_progress or 0) > 0 else DEFAULT_MAX_NO_PROGRESS
    loop_id = "loop_" + uuid.uuid4().hex[:12]

    # Restarting a loop (a fresh /loop or a model self-initiate racing an existing loop)
    # must cancel the PRIOR loop's pending wakeup before arming a new one, or the old
    # one-shot survives in the schedule store and fires unattended later (orphaned
    # wakeup) — violating the "at most one loop wakeup is ever armed" invariant
    # (cancel-both). end_loop is idempotent, so this is a safe no-op when no prior loop
    # is active.
    prior = _get_loop(app, sid)
    if prior and prior.get("active") and not prior.get("stopped"):
        end_loop(app, sid, reason="loop_restarted", loop=prior)

    sess = app.state.sessions.get(sid)
    tokens_at_start = 0
    cost_at_start = 0.0
    if sess is not None:
        tokens_at_start = int(getattr(sess, "tokens_input", 0)) + int(getattr(sess, "tokens_output", 0))
        cost_at_start = float(getattr(sess, "cost_usd", 0.0))

    loop: dict[str, Any] = {
        "loop_id": loop_id,
        "active": True,
        "prompt": text,
        "iteration": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "max_iters": eff_iters,
        "max_wallclock_s": eff_wall,
        "max_tokens": int(max_tokens or 0),
        "max_usd": float(max_usd or 0.0),
        "tokens_at_start": tokens_at_start,
        "cost_at_start": cost_at_start,
        "max_no_progress": eff_stall,
        "no_progress_count": 0,
        "last_activity_monotonic": app.state.bus.last_publish_monotonic(sid),
        "interval_s": delay,
        "pending_schedule_id": "",
        "armed": False,
        "fallback_pending": False,
        "stopped": False,
        "stop_reason": "",
        "clamp_reason": "",
    }
    _put_loop(app, sid, loop)
    next_fire = _arm(app, sid, loop, text, delay)
    logger.info(
        "loop started loop_id=%s interval_s=%s max_iters=%s max_wallclock_s=%s "
        "max_tokens=%s max_no_progress=%s next_fire_at=%s",
        loop_id,
        delay,
        eff_iters,
        eff_wall,
        int(max_tokens or 0),
        eff_stall,
        next_fire,
    )
    return {
        "loop_id": loop_id,
        "next_fire_at": next_fire,
        "interval_s": delay,
        "max_iters": eff_iters,
        "max_wallclock_s": eff_wall,
        "max_tokens": int(max_tokens or 0),
        "max_no_progress": eff_stall,
        "stopped": False,
    }


# --------------------------------------------------------------------------- #
# loop_wakeup — the model self-pace primitive (the ScheduleWakeup analog).      #
# --------------------------------------------------------------------------- #
def loop_wakeup_impl(
    delay_seconds: int = WAKEUP_MIN_S,
    prompt: str = "",
    reason: str = "",
    stop: bool = False,
) -> dict[str, Any]:
    """Reschedule this session's loop after a delay, or ``stop:true`` to end it.

    The model's control door (⚑ RULE 1 — the model decides; clio enforces). On a
    non-stop call: clamps ``delay_seconds`` (typed reason if clamped), folds the
    iteration that just ran into the no-progress counter, increments the iteration, and —
    if no typed bound trips — arms the next wakeup (reusing the scheduler one-shot). When
    a bound trips OR ``stop:true``, the loop ends with a typed reason and the pending
    wakeup is cancelled (cancel-both). Returns ``{loop_id, next_fire_at, stopped}``."""

    app, sid = _active()
    loop = _get_loop(app, sid)

    # No active loop: the model self-initiates one (the /loop command is the other door).
    if not loop or not loop.get("active"):
        if stop:
            return {"loop_id": "", "next_fire_at": "", "stopped": True}
        started = start_loop(app, sid, prompt=prompt, interval_s=delay_seconds)
        return {
            "loop_id": started["loop_id"],
            "next_fire_at": started["next_fire_at"],
            "stopped": False,
        }

    loop_id = str(loop.get("loop_id") or "")
    if stop:
        end_loop(app, sid, reason="loop_user_stopped", loop=loop, detail=reason)
        return {"loop_id": loop_id, "next_fire_at": "", "stopped": True}

    delay, clamp_reason = clamp_delay(delay_seconds)
    if clamp_reason:
        loop["clamp_reason"] = clamp_reason
        logger.info(
            "loop delay clamped reason=%s loop_id=%s requested=%s applied=%s",
            clamp_reason,
            loop_id,
            delay_seconds,
            delay,
        )

    _record_progress(app, sid, loop)
    loop["iteration"] = int(loop.get("iteration", 0)) + 1

    stop_reason = _check_bounds(app, sid, loop)
    if stop_reason:
        end_loop(app, sid, reason=stop_reason, loop=loop, detail=reason)
        return {"loop_id": loop_id, "next_fire_at": "", "stopped": True}

    next_prompt = (prompt or "").strip() or str(loop.get("prompt") or "")
    next_fire = _arm(app, sid, loop, next_prompt, delay)
    logger.info(
        "loop wakeup armed loop_id=%s iteration=%s delay_s=%s reason=%s",
        loop_id,
        loop.get("iteration"),
        delay,
        reason,
    )
    return {"loop_id": loop_id, "next_fire_at": next_fire, "stopped": False}


# --------------------------------------------------------------------------- #
# Bounded fallback — the turn-end hook (never a silent hang).                    #
# --------------------------------------------------------------------------- #
def dispatch_loop_at_finalize(app: Any, *, session_id: str, turn_id: str = "") -> None:
    """Bounded-fallback hook, fired once per turn from ``finalize_turn`` (never raises).

    Detection is store-state, needing no turn-start hook: a loop's pending wakeup is a
    one-shot the scheduler POPS (``mark_fired``) the moment it fires. So at a loop
    iteration's turn end, the fired wakeup's id is GONE from the store UNLESS the model
    armed a replacement this turn (a ``loop_wakeup`` reschedule installs a fresh live id).
    Thus:

    * pending id still resolves -> the model rescheduled (or this ending turn was not the
      loop iteration and the future wakeup is still queued) -> nothing to decide;
    * pending id is gone -> the iteration ran and the model armed nothing. If the single
      fallback was already spent, END with ``loop_no_reschedule``; otherwise arm exactly
      ONE fixed-delay fallback wakeup (still bound-checked) so a stalled loop gets one
      bounded retry, never an unbounded silent wait."""

    try:
        loop = _get_loop(app, session_id)
        if not loop or not loop.get("active") or loop.get("stopped") or not loop.get("armed"):
            return
        pending_id = str(loop.get("pending_schedule_id") or "")
        if pending_id and app.state.schedules.get(pending_id) is not None:
            # The armed wakeup is still queued (a fresh reschedule, or a not-yet-fired
            # future wakeup on an unrelated turn). Nothing to decide.
            return
        if loop.get("fallback_pending"):
            # The one fallback wakeup fired and STILL no reschedule — end (bounded).
            end_loop(app, session_id, reason="loop_no_reschedule", loop=loop)
            return
        # A loop iteration ran and the model armed no replacement: grant ONE bounded
        # fallback turn, but only if no typed bound trips first.
        loop["iteration"] = int(loop.get("iteration", 0)) + 1
        stop_reason = _check_bounds(app, session_id, loop)
        if stop_reason:
            end_loop(app, session_id, reason=stop_reason, loop=loop)
            return
        next_fire = _arm(app, session_id, loop, str(loop.get("prompt") or ""), FALLBACK_DELAY_S)
        loop["fallback_pending"] = True
        _put_loop(app, session_id, loop)
        logger.info(
            "loop fallback armed reason=loop_no_reschedule loop_id=%s next_fire_at=%s",
            loop.get("loop_id"),
            next_fire,
        )
    except Exception:  # noqa: BLE001 - the finalize hook must never crash a turn
        logger.warning("loop finalize hook error", exc_info=True)


# --------------------------------------------------------------------------- #
# /loop command parsing (owner-module logic; catalog route stays thin).         #
# --------------------------------------------------------------------------- #
def _parse_interval(token: str) -> Optional[int]:
    """Parse an interval token to seconds: ``30s`` / ``5m`` / ``1h`` / bare ``300``.

    Returns ``None`` when the token is not an interval (so the first word of a ``/loop``
    invocation is treated as prompt text, not swallowed as a bad interval)."""

    text = str(token or "").strip().lower()
    if not text:
        return None
    unit = 1
    if text[-1] in "smh":
        unit = {"s": 1, "m": 60, "h": 3600}[text[-1]]
        text = text[:-1]
    if not text.isdigit():
        return None
    return int(text) * unit


def parse_loop_command(request_body: Mapping[str, Any]) -> tuple[int, str, dict[str, Any]]:
    """Parse ``/loop [interval] <prompt>`` (+ optional ``args`` bounds) from a request.

    Returns ``(interval_s, prompt, bounds)``. A leading interval token is consumed off the
    text; typed bounds come from an ``args`` mapping (``max_iters`` / ``max_wallclock_s`` /
    ``max_tokens`` / ``max_usd`` / ``max_no_progress``)."""

    text = str(
        request_body.get("input")
        or request_body.get("text")
        or request_body.get("prompt")
        or ""
    ).strip()
    raw_args = request_body.get("args")
    args: Mapping[str, Any] = raw_args if isinstance(raw_args, Mapping) else {}

    interval_s = 0
    if text:
        first, _, rest = text.partition(" ")
        parsed = _parse_interval(first)
        if parsed is not None:
            interval_s = parsed
            text = rest.strip()
    if not interval_s and args.get("interval"):
        interval_s = _parse_interval(str(args.get("interval"))) or 0
    prompt = text or str(args.get("prompt") or "").strip()

    bounds: dict[str, Any] = {}
    for key in ("max_iters", "max_wallclock_s", "max_tokens", "max_usd", "max_no_progress"):
        if key in args:
            bounds[key] = args[key]
    return interval_s, prompt, bounds


def run_loop_command(app: Any, sid: str, request_body: Mapping[str, Any]) -> str:
    """Execute the ``/loop`` user command: start a loop, return the system-message body.

    The whole parse + start + message logic lives here so the catalog dispatch route stays
    a thin one-liner (no-accretion — the route file is a ratcheted god-file)."""

    interval_s, prompt, bounds = parse_loop_command(request_body)
    if not prompt:
        return (
            "usage: /loop [interval] <prompt> — a prompt is required to re-drive each "
            "iteration (e.g. /loop 5m keep triaging open PRs)"
        )
    try:
        started = start_loop(app, sid, prompt=prompt, interval_s=interval_s, **bounds)
    except LoopError as exc:
        return f"/loop rejected: {exc} (reason={exc.reason})"
    return (
        f"loop {started['loop_id']} started — re-driving every {started['interval_s']}s "
        f"(next wake {started['next_fire_at']}); bounds max_iters={started['max_iters']}, "
        f"max_wallclock_s={int(started['max_wallclock_s'])}, "
        f"max_tokens={started['max_tokens']}, max_no_progress={started['max_no_progress']}. "
        f"The loop stops on any bound, on loop_wakeup(stop=true), or when this session ends."
    )


# --------------------------------------------------------------------------- #
# Model tool (auto-attached, like the cron triad).                             #
# --------------------------------------------------------------------------- #
def build_loop_wakeup_tool() -> Any:
    """Build the ``loop_wakeup`` dspy.Tool (auto-attached; self-pace control)."""

    import dspy  # noqa: PLC0415

    def loop_wakeup(
        delay_seconds: int = WAKEUP_MIN_S,
        prompt: str = "",
        reason: str = "",
        stop: bool = False,
    ) -> dict:
        """Continue OR end this session's autonomous loop (self-paced iteration).

        Call this at the END of a loop iteration to decide what happens next:
        - to KEEP looping, pass ``delay_seconds`` (how long to wait before the next
          iteration; clamped to [60, 3600] — a shorter/longer value is clamped with a
          logged reason) and the ``prompt`` to re-run (defaults to the loop's prompt).
        - to END the loop, pass ``stop=True``. Ending is EXPLICIT — the loop does not stop
          just because you go quiet (a bounded fallback would fire once, then end).

        ``reason`` is a short note logged to the trace (why you continued/stopped). If no
        loop is active yet, a non-stop call STARTS one with default bounds. The loop also
        stops on its own when a typed bound trips: max iterations, wall-clock / token /
        cost budget, or no-progress detection. Returns ``{loop_id, next_fire_at, stopped}``.
        """

        return loop_wakeup_impl(
            delay_seconds=delay_seconds, prompt=prompt, reason=reason, stop=stop
        )

    return dspy.Tool(
        func=loop_wakeup,
        name="loop_wakeup",
        desc=loop_wakeup.__doc__,
        args={
            "delay_seconds": {
                "type": "integer",
                "description": "Seconds to wait before the next iteration (clamped to [60, 3600]).",
            },
            "prompt": {
                "type": "string",
                "description": "The prompt to re-run next iteration (defaults to the loop's prompt).",
            },
            "reason": {
                "type": "string",
                "description": "A short note logged to the trace (why you continued or stopped).",
            },
            "stop": {
                "type": "boolean",
                "description": "True to END the loop now (explicit self-termination).",
            },
        },
    )
