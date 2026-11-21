"""``/v1/sessions/{sid}/scripts/foreground``: the session's running foreground scripts,
and the person's "send to background" for them.

A synchronous ``run_code`` holds the agent's turn while its script runs. ``GET`` lists the
ones running now (label, elapsed seconds, tool calls so far), and ``POST …/background``
moves them, still running, to background tasks: the agent's call returns with each
task's handle and the agent goes on (:mod:`clio_agent.gact.code_mode.foreground`).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from clio_agent.gact.code_mode import foreground


def register_foreground_script_routes(app: FastAPI) -> None:
    """List and background a session's running foreground scripts."""

    def _known(sid: str) -> None:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(status_code=404, detail=f"session not found: {sid}")

    @app.get("/v1/sessions/{sid}/scripts/foreground")
    async def foreground_scripts(sid: str) -> dict[str, Any]:
        _known(sid)
        return {"scripts": foreground.running_scripts(sid)}

    @app.post("/v1/sessions/{sid}/scripts/foreground/background")
    async def background_foreground_scripts(sid: str) -> dict[str, Any]:
        """Move the session's running foreground scripts to background tasks."""
        _known(sid)
        return {"moved": foreground.request_background(sid)}
