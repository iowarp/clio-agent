"""What crosses from a running Clio Coder turn to the CLIO conversation while it works.

Two channels: Clio Coder's permission asks go through CLIO's own permission gate (so the person,
the conversation's approval mode and its policies decide, exactly as for CLIO's own tools), and
the turn's tool activity and notices stream into the delegating call's live progress.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from typing import Any

from clio_agent.gact import context
from clio_agent.runtime.clio_coder.acp import PermissionHandler, reject_permissions

#: The gate has no deadline (a question waits for the person, permission_timeout), so Clio
#: Coder is asked to wait as long as its ``--permission-timeout`` allows (2**31 - 1 ms, about
#: 24 days): the gate's verdict, or the run's own timeout, always comes first.
CLIO_CODER_PERMISSION_TIMEOUT_S = 2_147_483.0
_INPUT_LIMIT = 600


def _input_text(raw: Any) -> str:
    if raw is None:
        return ""
    text = raw if isinstance(raw, str) else json.dumps(raw, default=str, sort_keys=True)
    return text if len(text) <= _INPUT_LIMIT else text[:_INPUT_LIMIT] + "..."


def describe_ask(params: Mapping[str, Any]) -> dict[str, Any]:
    """The gate arguments for one ACP ``session/request_permission``: what Clio Coder wants."""
    call = params.get("toolCall") if isinstance(params.get("toolCall"), Mapping) else {}
    locations = [
        str(loc.get("path"))
        for loc in call.get("locations") or []
        if isinstance(loc, Mapping) and loc.get("path")
    ]
    return {
        "request": str(call.get("title") or "a tool call"),
        "kind": str(call.get("kind") or "other"),
        "input": _input_text(call.get("rawInput")),
        "locations": locations[:20],
    }


def gate_permission_relay(app: Any, tool_name: str, run_id: str) -> PermissionHandler:
    """Answer each Clio Coder ask with CLIO's gate verdict: allow once, else refuse once.

    The gate is the same one CLIO's own calls go through, so a plan-mode conversation refuses,
    an ask-mode one asks the person, and a bypassed one allows: the delegated run never gets
    more than the conversation that started it.
    """
    from clio_agent.gact.permission_gate import _invoke_permission_gate  # noqa: PLC0415

    gate = getattr(app.state, "pending_permission_gate", None) or app.state.make_permission_gate()

    async def relay(params: dict[str, Any]) -> str | None:
        ask = describe_ask(params)
        where = f" ({', '.join(ask['locations'][:3])})" if ask["locations"] else ""
        summary = f"Clio Coder (run {run_id}) asks to {ask['request']}{where}"
        if ask["input"]:
            summary += f": {ask['input']}"
        # Path-scoped policies match one ``path`` per call, so each location is gated on its own
        # and the ask is allowed only when every location is.
        targets = ask["locations"] or [None]
        decision = "allow"
        for target in targets:
            args = {"run": run_id, **ask, **({"path": target} if target else {})}
            decision = await asyncio.to_thread(
                _invoke_permission_gate,
                gate,
                tool_name,
                args,
                {"kind": "native", "summary": summary[:1000]},
            )
            if decision != "allow":
                break
        if decision == "allow":
            for option in params.get("options", []):
                if option.get("kind") == "allow_once":
                    return str(option["optionId"])
        return await reject_permissions(params)

    return relay


def _progress_line(event: Mapping[str, Any]) -> str | None:
    kind = event.get("kind")
    if kind == "session":
        verb = "resumed" if event.get("resumed") else "started"
        return f"Clio Coder session {event.get('sessionId')} {verb}"
    if kind == "tool" and event.get("status") in ("completed", "failed", "blocked"):
        return f"Clio Coder {event.get('status')}: {event.get('name')}"
    if kind == "notice" and event.get("text"):
        return f"Clio Coder notice: {event.get('text')}"
    return None


def progress_sink(tool_name: str, args: Mapping[str, Any]) -> Callable[[dict[str, Any]], None]:
    """Stream the turn's session, finished tool calls and notices as the call's live progress.

    Answer text is not streamed; it arrives whole with the hand-back.
    """
    from clio_agent.gact.tool_observer import _OBSERVER_CALL_IDS  # noqa: PLC0415
    from clio_agent.tools.execution import notify_global_tool_observer  # noqa: PLC0415

    call_id = _OBSERVER_CALL_IDS.get()
    session_id = context.active_session_id() if call_id else ""
    handle = {"call_id": call_id, "session_id": session_id}
    count = 0
    last = ""

    def sink(event: dict[str, Any]) -> None:
        nonlocal count, last
        line = _progress_line(event)
        if not call_id or not session_id or line is None or line == last:
            return
        count, last = count + 1, line
        notify_global_tool_observer(
            tool_name,
            args,
            "progress",
            None,
            {"observer_handle": handle, "progress": count, "total": None, "message": line},
        )

    return sink
