"""Model-callable cron triad — ``cron_create`` / ``cron_list`` / ``cron_delete`` (#1081).

Owner module for the three infrastructure tools every dynamic react expert gets
auto-attached (via :mod:`clio_agent.gact.agents.auto_tools`), NOT counted against the
5-7 curated domain-tool budget (RULE 5) — the field-wide convergent scheduling shape
(Claude Code ``CronCreate/CronList/CronDelete``, mcp-cron ``add_task/list_tasks/
remove_task``).

Contract (survey §3, ⚑ RULE 1 — the MODEL decides, clio is the deterministic enforcer):

* ``cron_create(cron, prompt, recurring=True, run_at="", delay_s=0, timezone="")`` —
  returns a **stable, server-generated ``schedule_id`` in the RESULT ONLY** (never echoed
  from an input field). ``recurring=False`` (or any ``run_at``/``delay_s``) is a one-shot
  that auto-deletes after firing once. A clamp trip / bad cron raises a TYPED
  :class:`~clio_agent.gact.scheduler.CronError` the model can read + repair against.
* ``cron_list()`` — the **read-back tool that prevents double-arming** (every system with
  a create tool ships one): ``[{id, cron, prompt, recurring, next_fire_at, timezone}]`` for
  the active session.
* ``cron_delete(schedule_id)`` — **cancel-both**: removes the store row AND discards any
  daemon-side deferred entry, so no orphan tick survives.

All three resolve the active app + session through :mod:`clio_agent.gact.context`, so a
call outside a live turn raises a typed ``no_active_session`` error rather than acting on
nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from clio_agent.gact import context as _ctx
from clio_agent.gact.scheduler import CronError, Schedule

logger = logging.getLogger(__name__)


def _active() -> tuple[Any, str]:
    """Resolve (app, session_id) for a cron tool call (typed error when absent)."""

    app = _ctx.active_app()
    sid = _ctx.active_session_id()
    if app is None or not sid:
        raise CronError(
            "cron tools require an active CLIO app/session context.",
            reason="no_active_session",
        )
    return app, sid


def cancel_schedule(app: Any, schedule_id: str) -> bool:
    """Delete a schedule AND cancel its daemon-side deferred entry (cancel-both).

    Shared by the ``cron_delete`` tool and the HTTP delete route so both close the same
    orphan window: the store drop stops the due scan, and discarding the id from
    ``deferred_schedules`` stops a queued busy-retry from resurrecting it."""

    existed = app.state.schedules.delete(schedule_id)
    deferred = getattr(app.state, "deferred_schedules", None)
    if deferred is not None:
        deferred.discard(schedule_id)
    return existed


def cancel_owned_schedule(app: Any, session_id: str, schedule_id: str) -> bool:
    """Session-scoped cancel-both for the model tool + ``/cron`` command paths.

    Enforces ownership so a model in one session cannot cancel (or probe the existence
    of) another session's schedule. Resolving to ``False`` for BOTH the not-found and the
    wrong-owner case makes the two indistinguishable to the caller — no cross-session
    enumeration oracle. A wrong-owner attempt emits the typed ``cron_delete_not_owner``
    reason to the trace/log (no-silent-refusal holds server-side). The HTTP delete route
    deliberately uses :func:`cancel_schedule` instead — it serves direct user action on
    arbitrary sessions behind its own destructive-action guard.

    Args:
        app: The live CLIO app carrying ``state.schedules`` (and optional
            ``state.deferred_schedules``).
        session_id: The calling session — must own the schedule to cancel it.
        schedule_id: The id to cancel.

    Returns:
        ``True`` only when an owned schedule was removed; ``False`` when no such id
        exists for this session (missing OR owned by another session).
    """

    sch = app.state.schedules.get(schedule_id)
    if sch is None:
        return False
    if sch.session_id != session_id:
        logger.warning(
            "cron delete refused: cross-session ownership "
            "reason=cron_delete_not_owner schedule_id=%s caller_session=%s owner_session=%s",
            schedule_id,
            session_id,
            sch.session_id,
        )
        return False
    return cancel_schedule(app, schedule_id)


# --------------------------------------------------------------------------- #
# /cron command parsing (owner-module logic; the catalog route stays thin).    #
# Mirrors run_loop_command / run_goal_command exactly: same body-parse          #
# convention, same typed-error style, same result-message shape (#1081 P4.3     #
# gap — the command dispatch was never wired, so POSTing /commands/cron         #
# returned "unhandled command").                                                #
# --------------------------------------------------------------------------- #
_LIST_TOKENS = frozenset({"list", "ls", "show"})
_DELETE_TOKENS = frozenset({"delete", "cancel", "rm", "remove"})


def _has_create_args(args: Mapping[str, Any]) -> bool:
    """True when the structured ``args`` carry a create trigger (cron/run_at/delay_s)."""

    return bool(
        str(args.get("cron") or "").strip()
        or str(args.get("run_at") or "").strip()
        or int(args.get("delay_s") or 0) > 0
    )


def _format_schedule_line(s: Schedule) -> str:
    """One readable row for the ``/cron list`` body (mirrors cron_list's fields)."""

    trigger = f"cron {s.cron}" if s.cron else f"run_at {s.run_at}"
    kind = "recurring" if s.recurring else "one-shot"
    return (
        f"  {s.id}  {trigger}  next_fire_at={s.next_fire_at or '-'}  "
        f"tz={s.timezone}  {kind}  prompt={s.question!r}"
    )


def run_cron_command(app: Any, sid: str, request_body: Mapping[str, Any]) -> str:
    """Execute the ``/cron`` (alias ``/schedule``) user command; return the message body.

    Surfaces the create/list/delete triad the command advertises, reusing the SAME
    :meth:`ScheduleStore.create` / :func:`cancel_schedule` logic the model tools and HTTP
    route use (no duplicated scheduler logic — clamps, local-tz, one-shot, and cancel-both
    are all enforced there). Parse + dispatch + message live here so the catalog route
    stays a thin one-liner (no-accretion — that file is a ratcheted god-file).

    Body-parse convention matches :func:`parse_loop_command` / :func:`parse_goal_command`:
    text from ``input``/``text``/``prompt`` plus an optional ``args`` mapping.
    """

    text = str(
        request_body.get("input") or request_body.get("text") or request_body.get("prompt") or ""
    ).strip()
    raw_args = request_body.get("args")
    args: Mapping[str, Any] = raw_args if isinstance(raw_args, Mapping) else {}

    first, _, rest = text.partition(" ")
    verb = first.strip().lower()
    rest = rest.strip()

    # DELETE / cancel / rm <id> — id from the next token or args.schedule_id (cancel-both).
    if verb in _DELETE_TOKENS:
        schedule_id = (rest.split()[0] if rest else "") or str(
            args.get("schedule_id") or ""
        ).strip()
        if not schedule_id:
            return "usage: /cron delete <schedule_id> — the id from /cron list or cron_create"
        removed = cancel_owned_schedule(app, sid, schedule_id)
        return (
            f"schedule {schedule_id} cancelled (recurring tick + any pending retry cleared)"
            if removed
            else f"no schedule {schedule_id} to cancel (already gone or never armed)"
        )

    # LIST — explicit verb, or a bare /cron with no create intent (empty text + no args).
    if verb in _LIST_TOKENS or (not text and not _has_create_args(args)):
        rows = app.state.schedules.list(session_id=sid)
        if not rows:
            return (
                "no schedules armed for this session — "
                "/cron <5-field-cron> <prompt> to add one (e.g. /cron 0 9 * * * daily digest)"
            )
        return "scheduled turns for this session:\n" + "\n".join(
            _format_schedule_line(s) for s in rows
        )

    # CREATE — structured args win; else parse the text form `<5-field-cron> <prompt>`.
    cron = str(args.get("cron") or "").strip()
    run_at = str(args.get("run_at") or "").strip()
    delay_s = int(args.get("delay_s") or 0)
    recurring = bool(args.get("recurring", True))
    timezone_name = str(args.get("timezone") or "").strip()
    prompt = str(args.get("prompt") or "").strip()

    if not (cron or run_at or delay_s > 0):
        tokens = text.split()
        if len(tokens) >= 5:
            cron = " ".join(tokens[:5])
            prompt = " ".join(tokens[5:]).strip()

    if not (cron or run_at or delay_s > 0):
        return (
            "usage: /cron <5-field-cron> <prompt> — e.g. /cron 0 9 * * * post the daily "
            "standup. Or pass args.run_at / args.delay_s for a one-shot, /cron list to "
            "review, /cron delete <id> to cancel."
        )
    if not prompt:
        return "usage: /cron <5-field-cron> <prompt> — a prompt is required for the scheduled turn"

    try:
        sch = app.state.schedules.create(
            session_id=sid,
            question=prompt,
            cron=cron,
            run_at=run_at,
            delay_s=delay_s,
            recurring=recurring,
            timezone_name=timezone_name,
        )
    except CronError as exc:
        return f"/cron rejected: {exc} (reason={exc.reason})"
    kind = "recurring" if sch.recurring else "one-shot"
    trigger = f"cron {sch.cron}" if sch.cron else f"run_at {sch.run_at}"
    return (
        f"schedule {sch.id} armed — {kind} {trigger} ({sch.timezone}); "
        f"next fire {sch.next_fire_at}. Use /cron list to review or "
        f"/cron delete {sch.id} to cancel."
    )


def build_cron_create_tool() -> Any:
    """Build the ``cron_create`` dspy.Tool (auto-attached; result-only schedule_id)."""

    import dspy  # noqa: PLC0415

    def cron_create(
        cron: str = "",
        prompt: str = "",
        recurring: bool = True,
        run_at: str = "",
        delay_s: int = 0,
        timezone: str = "",
    ) -> dict:
        """Schedule a future turn for THIS session (recurring cron or one-shot).

        Give exactly ONE trigger:
        - ``cron``: a 5-field expression ("min hour day-of-month month day-of-week",
          e.g. "0 9 * * *" = 9am daily, "*/15 * * * *" = every 15 min, "0 9 * * 1-5" =
          weekdays 9am). Evaluated in the session's LOCAL timezone.
        - ``run_at``: an ISO-8601 instant to fire ONCE ("2026-08-01T09:00").
        - ``delay_s``: fire ONCE after this many seconds.

        ``prompt`` is the question the scheduled turn will run. ``recurring=False`` (or any
        ``run_at``/``delay_s``) makes it a one-shot that deletes itself after firing.
        ``timezone`` overrides the session default (an IANA name like "America/Chicago").

        Returns a dict with the server-generated ``schedule_id`` (use it with cron_delete),
        the resolved ``cron``/``timezone``, ``recurring``, and the ``next_fire_at`` UTC
        instant. Call cron_list first if unsure whether you already armed this — do not
        double-arm. A bad cron or an anti-runaway clamp trip raises a typed error to repair."""

        app, sid = _active()
        sch = app.state.schedules.create(
            session_id=sid,
            question=prompt,
            cron=cron,
            run_at=run_at,
            delay_s=int(delay_s or 0),
            recurring=bool(recurring),
            timezone_name=timezone,
        )
        return {
            "schedule_id": sch.id,
            "cron": sch.cron,
            "run_at": sch.run_at,
            "recurring": sch.recurring,
            "timezone": sch.timezone,
            "next_fire_at": sch.next_fire_at,
        }

    return dspy.Tool(
        func=cron_create,
        name="cron_create",
        desc=cron_create.__doc__,
        args={
            "cron": {
                "type": "string",
                "description": "5-field cron in the session's local tz (or omit for a one-shot).",
            },
            "prompt": {"type": "string", "description": "The question the scheduled turn runs."},
            "recurring": {
                "type": "boolean",
                "description": "False = one-shot, auto-deletes after firing once.",
            },
            "run_at": {
                "type": "string",
                "description": "ISO-8601 instant to fire ONCE (mutually exclusive with cron/delay_s).",
            },
            "delay_s": {
                "type": "integer",
                "description": "Fire ONCE after this many seconds (mutually exclusive with cron/run_at).",
            },
            "timezone": {
                "type": "string",
                "description": "IANA tz override (e.g. 'America/Chicago'); defaults to the session/system local zone.",
            },
        },
    )


def build_cron_list_tool() -> Any:
    """Build the ``cron_list`` read-back dspy.Tool (prevents double-arming)."""

    import dspy  # noqa: PLC0415

    def cron_list() -> list:
        """List THIS session's scheduled turns (the read-back before you arm a new one).

        Returns ``[{id, cron, prompt, recurring, next_fire_at, timezone}]``. Check this before
        calling cron_create so you do not double-arm the same schedule; use an id with
        cron_delete to cancel one."""

        app, sid = _active()
        rows = app.state.schedules.list(session_id=sid)
        return [
            {
                "id": s.id,
                "cron": s.cron,
                "prompt": s.question,
                "recurring": s.recurring,
                "next_fire_at": s.next_fire_at,
                "timezone": s.timezone,
            }
            for s in rows
        ]

    return dspy.Tool(func=cron_list, name="cron_list", desc=cron_list.__doc__, args={})


def build_cron_delete_tool() -> Any:
    """Build the ``cron_delete`` dspy.Tool (cancel-both: store row + daemon deferred)."""

    import dspy  # noqa: PLC0415

    def cron_delete(schedule_id: str) -> bool:
        """Cancel a scheduled turn by its ``schedule_id`` (from cron_create/cron_list).

        Returns True if a schedule was removed, False if no such id existed FOR THIS
        session (a schedule owned by another session is indistinguishable from one that
        never existed — you can only cancel your own). This is cancel-both: it stops the
        recurring tick AND clears any pending busy-retry, so nothing fires afterward."""

        app, sid = _active()
        return cancel_owned_schedule(app, sid, str(schedule_id or "").strip())

    return dspy.Tool(
        func=cron_delete,
        name="cron_delete",
        desc=cron_delete.__doc__,
        args={
            "schedule_id": {
                "type": "string",
                "description": "The schedule id returned by cron_create / cron_list.",
            }
        },
    )


def build_cron_tools() -> list[Any]:
    """The auto-attached cron triad (order-stable for a byte-stable react tool prefix)."""

    return [build_cron_create_tool(), build_cron_list_tool(), build_cron_delete_tool()]
