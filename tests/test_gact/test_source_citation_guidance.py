"""Every agent's composed prompt advertises real connected-service citation behavior.

The citation and rendering guidance is the ``core.rendering`` fragment of the
``core`` plugin every agent carries; the workspace context only names the
session's workspace root.
"""

from pathlib import Path

import pytest
from fastapi import FastAPI

from clio_agent.gact.agents.composition import _runtime_active_workspace_context
from clio_agent.plugins.service import PluginService


def _composed_prompt() -> str:
    return " ".join(PluginService().resolve("clio/standard", pin=False).prompt_text.split())


def test_file_citation_guidance_is_shared_and_workspace_scoped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep citations tied to the agent's workspace without widening file access."""
    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_workspace_catalog_cwd",
        lambda app, *, session_id: tmp_path,
    )
    workspace = _runtime_active_workspace_context(FastAPI(), session_id="session")
    assert f"Active workspace root: {tmp_path}" in workspace

    text = _composed_prompt()
    assert "workspace-file:/absolute/workspace/README.md" in text
    assert "connected service" in text
    assert "actual returned artifact: or resource: URI" in text
    assert "outside this workspace is not automatically viewable" in text
    assert "pending tasks are not scientific evidence" in text
    assert "natural language" in text
    assert "session://WORKSPACE_ID/SESSION_ID" in text
    assert "matching ordered entries" in text
    assert "inline LaTeX uses $$x^2$$" in text
    assert r"\bar{x} = \frac{1}{n}" in text


def test_rendering_syntax_remains_available_without_a_resolved_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Avoid withholding general response capabilities when no path is known yet."""
    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_workspace_catalog_cwd",
        lambda app, *, session_id: None,
    )
    workspace = _runtime_active_workspace_context(FastAPI(), session_id="session")
    assert "Active workspace root:" not in workspace

    text = _composed_prompt()
    assert "Response presentation:" in text
    assert "inline LaTeX uses $$x^2$$" in text
