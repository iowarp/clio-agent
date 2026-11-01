"""The Claude Code CLI runs in the session's workspace, never the server's folder.

The CLI tells the model its working folder and whether it is a git repository, even
with CLIO's own system prompt and no settings sources, so that folder must be the
workspace CLIO's prompt names.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.providers import claude_code_cwd
from clio_agent.providers import claude_code_engine as engine_module


def _bind(monkeypatch: pytest.MonkeyPatch, root: Path | None) -> None:
    app = SimpleNamespace()
    monkeypatch.setattr("clio_agent.gact.context.active_app", lambda: app)
    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_workspace_catalog_cwd",
        lambda bound, *, session_id: root if bound is app and session_id == "sess" else None,
    )


def test_a_session_runs_in_its_workspace_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _bind(monkeypatch, tmp_path)
    assert claude_code_cwd.session_cwd("sess") == str(tmp_path)


def test_without_a_workspace_it_runs_in_an_empty_clio_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _bind(monkeypatch, None)
    events: list[str] = []
    monkeypatch.setattr(claude_code_cwd.trace, "event", lambda tag, fmt, *a: events.append(fmt % a))
    folder = Path(claude_code_cwd.session_cwd("other"))
    assert folder.is_dir() and not any(folder.iterdir())
    assert str(folder) != os.getcwd()
    assert events and events[0].startswith(claude_code_cwd.NO_WORKSPACE_REASON)


def test_the_engine_queries_from_the_session_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[str] = []

    async def _query(self: Any, request: Any, system: str, send: Any, cwd: str) -> Any:
        seen.append(cwd)
        if False:
            yield None

    monkeypatch.setattr(engine_module, "session_cwd", lambda session_id: str(tmp_path))
    monkeypatch.setattr(engine_module.AsyncClaudeCodeEngine, "_query", _query)
    monkeypatch.setattr(engine_module, "render_system", lambda system, tools: "")
    monkeypatch.setattr(engine_module._CONVERSATIONS, "plan", lambda key, request, system: None)

    async def _drain() -> None:
        engine = engine_module.AsyncClaudeCodeEngine("claude-haiku-5-5")
        async for _ in engine.stream(SimpleNamespace(system=None, tools=())):
            pass

    import asyncio

    asyncio.run(_drain())
    assert seen == [str(tmp_path)]
