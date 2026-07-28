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

from typing import Any

from clio_agent.gact import context as _ctx
from clio_agent.gact.scheduler import CronError


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
            "cron": {"type": "string", "description": "5-field cron in the session's local tz (or omit for a one-shot)."},
            "prompt": {"type": "string", "description": "The question the scheduled turn runs."},
            "recurring": {"type": "boolean", "description": "False = one-shot, auto-deletes after firing once."},
            "run_at": {"type": "string", "description": "ISO-8601 instant to fire ONCE (mutually exclusive with cron/delay_s)."},
            "delay_s": {"type": "integer", "description": "Fire ONCE after this many seconds (mutually exclusive with cron/run_at)."},
            "timezone": {"type": "string", "description": "IANA tz override (e.g. 'America/Chicago'); defaults to the session/system local zone."},
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

        Returns True if a schedule was removed, False if no such id existed. This is
        cancel-both: it stops the recurring tick AND clears any pending busy-retry, so
        nothing fires afterward."""

        app, _sid = _active()
        return cancel_schedule(app, str(schedule_id or "").strip())

    return dspy.Tool(
        func=cron_delete,
        name="cron_delete",
        desc=cron_delete.__doc__,
        args={"schedule_id": {"type": "string", "description": "The schedule id returned by cron_create / cron_list."}},
    )


def build_cron_tools() -> list[Any]:
    """The auto-attached cron triad (order-stable for a byte-stable react tool prefix)."""

    return [build_cron_create_tool(), build_cron_list_tool(), build_cron_delete_tool()]
