"""Agent-created monitoring loops with explicit optional limits."""

from __future__ import annotations

from typing import Any

from clio_agent.gact import context as _ctx
from clio_agent.gact.autonomous_loop import (
    _LOOP_MUTEX,
    LOOP_STICKY_STOP_REASONS,
    LoopError,
    _get_loop,
    _put_loop,
    start_loop,
)


def build_set_loop_tool() -> Any:
    """Expose loop configuration without allowing an agent to erase a user's stop or cap."""
    from clio_agent.gact.agents.tool_instrumentation import native_tool

    def set_loop(
        prompt: str,
        interval_s: int = 300,
        stop_text: str = "",
        max_iters: int = 0,
        max_wallclock_s: float = 0,
        max_tokens: int = 0,
        max_usd: float = 0,
    ) -> dict[str, Any]:
        """Repeat a monitoring instruction at an interval; zero limits are unlimited.

        Use for checking an HPC job, CI or another changing external state. An optional
        stop_text ends monitoring when your final answer contains that exact whole line,
        e.g. DONE. loop_wakeup may adjust the next wait or stop voluntarily. Existing
        active loops and user stops/caps require the user to replace or restart them.
        Goals independently drive a complex objective; this loop does not pace goals.
        """
        app, sid = _ctx.active_app(), _ctx.active_session_id()
        with _LOOP_MUTEX:
            prior = _get_loop(app, sid)
            if prior.get("active") or prior.get("stop_reason") in LOOP_STICKY_STOP_REASONS:
                raise LoopError(
                    "Ask the user to replace or restart the loop", reason="loop_protected"
                )
            result = start_loop(
                app,
                sid,
                prompt=prompt,
                interval_s=interval_s,
                stop_text=stop_text,
                max_iters=max_iters,
                max_wallclock_s=max_wallclock_s,
                max_tokens=max_tokens,
                max_usd=max_usd,
            )
            # Revising the initial pending wake is configuration, not a completed iteration.
            if turn_id := _ctx.active_turn_id():
                loop = _get_loop(app, sid)
                loop["last_reschedule_turn_id"] = turn_id
                _put_loop(app, sid, loop)
            return result

    return native_tool(
        set_loop,
        name="set_loop",
        presentation="loop_started",
        domain="autonomy",
        desc=set_loop.__doc__,
        title="Start loop",
        args={
            "prompt": {"type": "string"},
            "interval_s": {"type": "integer"},
            "stop_text": {"type": "string"},
            "max_iters": {"type": "integer"},
            "max_wallclock_s": {"type": "number"},
            "max_tokens": {"type": "integer"},
            "max_usd": {"type": "number"},
        },
    )
