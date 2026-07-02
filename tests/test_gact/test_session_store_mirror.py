"""Workspace mirror failures must log a structured reason (#772).

The mirror is best-effort by design, but a swallowed write error makes
workspace/session drift undetectable — every degraded path has to say WHY
it degraded (the ``reason=`` house style from ``gact/streaming.py``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import session_store


class _RaisingMessageStore:
    """MessageStore stand-in whose construction always fails."""

    def __init__(self, root: Path) -> None:
        raise OSError("disk full")


def _app_with_session(session_id: str) -> Any:
    """Minimal app shape: one session owned by one workspace."""
    sess = SimpleNamespace(workspace_id="ws1")
    return SimpleNamespace(
        state=SimpleNamespace(
            sessions={session_id: sess},
            workspaces={"ws1": object()},
            messages={session_id: [SimpleNamespace(role="user", content="hi")]},
        )
    )


@pytest.fixture()
def _mirror_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Route the mirror at tmp_path and make its MessageStore explode."""
    monkeypatch.setattr(session_store, "resolve_workspace_storage_root", lambda ws: tmp_path)
    monkeypatch.setattr(session_store, "MessageStore", _RaisingMessageStore)
    return _app_with_session("sess-mirror")


def test_mirror_messages_write_failure_logs_reason(
    _mirror_env: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed message-ledger mirror write warns instead of vanishing."""
    with caplog.at_level(logging.WARNING, logger="clio_agent.gact.session_store"):
        session_store._mirror_workspace_messages(_mirror_env, "sess-mirror")
    assert "reason=workspace_mirror_messages_write_failed" in caplog.text
    assert "sess-mirror" in caplog.text


def test_mirror_remove_failure_logs_reason(
    _mirror_env: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed mirror removal warns instead of vanishing."""
    with caplog.at_level(logging.WARNING, logger="clio_agent.gact.session_store"):
        session_store._remove_workspace_session_mirror(_mirror_env, "sess-mirror")
    assert "reason=workspace_mirror_remove_failed" in caplog.text
    assert "sess-mirror" in caplog.text
