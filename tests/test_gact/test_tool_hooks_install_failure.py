"""Slice 2 (#772) — tool-runtime-hooks install failure must be LOUD.

The permission gate + tool observer are installed via
``_install_tool_runtime_hooks``. If that install raises, the failure was
previously swallowed by a bare ``except Exception: pass`` — leaving the
server running WITHOUT a permission gate and no trace of why. That is the
highest-severity silent fallback in the epic: tools would execute
ungated and unobserved while /v1/health still reported ``ready``.

These tests pin the loud behaviour: a structured
``reason=tool_runtime_hooks_install_failed`` error log, the
``app.state.tool_hooks_installed`` flag flipped to ``False`` with the
error captured in ``app.state.tool_hooks_install_error``, and the flag
surfaced on GET /v1/health so an operator can see the degraded gate.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import app as gact_app
from clio_agent.gact.app import build_app


class _FakeAgent:
    """Minimal agent stub so build_app takes the eager-agent branch."""

    def forward(self, question: str, session_id: str) -> Any:  # pragma: no cover - unused
        raise NotImplementedError


def test_install_failure_flips_flag_logs_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raising installer must set the flag False, capture the error, and log the reason."""

    def _boom(app: Any) -> None:
        raise RuntimeError("gate wiring exploded")

    monkeypatch.setattr(gact_app, "_install_tool_runtime_hooks", _boom)

    with caplog.at_level(logging.ERROR, logger="clio_agent.gact.app"):
        app = build_app(sessions_path=tmp_path / "sessions.json", agent=_FakeAgent())

    assert app.state.tool_hooks_installed is False
    assert "gate wiring exploded" in app.state.tool_hooks_install_error

    matches = [r for r in caplog.records if "tool_runtime_hooks_install_failed" in r.getMessage()]
    assert matches, "install failure must log reason=tool_runtime_hooks_install_failed"
    assert matches[0].levelno == logging.ERROR


def test_health_surfaces_tool_hooks_installed_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/v1/health must report tool_hooks_installed=False after a failed install."""

    def _boom(app: Any) -> None:
        raise RuntimeError("gate wiring exploded")

    monkeypatch.setattr(gact_app, "_install_tool_runtime_hooks", _boom)

    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=_FakeAgent()))
    body = client.get("/v1/health").json()
    assert body["tool_hooks_installed"] is False


def test_health_reports_tool_hooks_installed_true_when_gate_present(tmp_path: Path) -> None:
    """A healthy install surfaces tool_hooks_installed=True on /v1/health."""

    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=_FakeAgent()))
    body = client.get("/v1/health").json()
    assert body["tool_hooks_installed"] is True


def test_deferred_boot_reports_pending_not_failed(tmp_path: Path) -> None:
    """The deferred-agent boot window must read as pending (None), never as failed.

    Production ``main()`` calls ``build_app(agent=None)`` and installs the
    hooks later, in ``_construct_agent_async``. During that window the flag
    must be ``None`` ("not yet determined"), NOT ``False`` — ``False`` is
    reserved exclusively for an install *failure* (the highest-severity
    fallback), and conflating the two makes every normal daemon startup
    look like an ungated tool surface on /v1/health.
    """

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=None)
    assert app.state.tool_hooks_installed is None

    # TestClient without a `with` block does not run the lifespan, so the
    # deferred agent construction never fires — exactly the startup window.
    body = TestClient(app).get("/v1/health").json()
    assert body["tool_hooks_installed"] is None
