"""SPOTTER clearance enforcement at the mutating-tool permission boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.gact.spotter_watcher import wait_for_spotter_clearance

if TYPE_CHECKING:
    from fastapi import FastAPI


def enforce_spotter_clearance(
    app: "FastAPI",
    sid: str,
    session: Any,
    tool_name: str,
    args: Mapping[str, Any],
    subject: str,
    record_resolution: Callable[..., Any],
) -> str:
    """Wait for standing surveillance or return a fail-closed denial message."""

    if session is None or getattr(session, "approval_mode", "") != "spotter-ai":
        return ""
    if wait_for_spotter_clearance(app, sid):
        return ""
    record_resolution(
        app,
        session_id=sid,
        tool_name=tool_name,
        args=args,
        status="auto_denied",
        action="deny",
        summary=f"{subject} {tool_name!r} blocked while SPOTTER surveillance was pending",
        reason="spotter_clearance_timeout",
    )
    return (
        "SPOTTER did not finish reviewing the preceding workload evidence within "
        "the safety deadline, so this tool call was not run."
    )
