"""Tests for the desktop-facing lifecycle seams (gact/desktop_lifecycle.py).

Split out of ``gact/app.py`` (file-size ratchet, #775/#774): ``serve_foreground``
(``run_server``'s non-reload uvicorn branch) and ``release_runtime_after_drain``
(the lifespan desktop-release step) moved to their own owner module. These
tests pin the two behaviors app.py used to cover inline.
"""

from __future__ import annotations

import pytest
import uvicorn
from fastapi import FastAPI

from clio_agent.gact import desktop_lifecycle


def test_serve_foreground_stamps_uvicorn_server_on_app_state(monkeypatch) -> None:
    """``serve_foreground`` must stamp ``app.state.uvicorn_server`` BEFORE it
    blocks in ``server.run()`` -- the authenticated desktop lifecycle route
    reaches it there to set ``should_exit`` for a cross-platform graceful stop."""

    stamped_at_run: list[object] = []

    def _fake_run(self: uvicorn.Server) -> None:
        # Assert the stamp already happened by the time run() is entered, not
        # merely after serve_foreground returns.
        stamped_at_run.append(app.state.uvicorn_server)

    monkeypatch.setattr(uvicorn.Server, "run", _fake_run)

    app = FastAPI()
    desktop_lifecycle.serve_foreground(app, host="127.0.0.1", port=8123)

    server = app.state.uvicorn_server
    assert isinstance(server, uvicorn.Server)
    assert stamped_at_run == [server]
    assert server.config.host == "127.0.0.1"
    assert server.config.port == 8123


@pytest.mark.asyncio
async def test_release_runtime_after_drain_reports_not_desktop_when_flag_unset() -> None:
    """No release, and an honest ``"not_desktop"`` report, when this process was
    never a desktop-driven boot (``app.state.desktop_shutdown_requested`` unset)."""

    app = FastAPI()

    result = await desktop_lifecycle.release_runtime_after_drain(app)

    assert result == "not_desktop"
