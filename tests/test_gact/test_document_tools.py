"""The default agent can prepare documents without bypassing workspace policy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact import document_tools
from clio_agent.gact.agents.declared_native_tools import resolve_declared_native_tools
from clio_agent.gact.catalog import _builtin_main_agent
from clio_agent.gact.skills import SkillCatalog, read_skill_body
from clio_agent.tools.file_policy import FileAccessPolicy


def test_default_document_tools_are_available_without_vision() -> None:
    """Text-capable models can create and inspect documents; pixels remain gated."""
    agent = _builtin_main_agent()
    requested, available, gateway = resolve_declared_native_tools(agent, {})
    assert {"prepare_document", "prepare_document_runtime", "prepare_execution_runtime"} <= set(
        available
    )
    assert "view_image" not in requested
    assert not {"prepare_document", "prepare_document_runtime"} & set(gateway)


def test_default_office_skills_resolve_independently_of_user_directories(tmp_path: Path) -> None:
    """A bare-session skill declaration resolves the packaged workflows."""
    agent = _builtin_main_agent()
    catalog = SkillCatalog(home=tmp_path / "home", cwd=tmp_path / "workspace")
    resolutions = catalog.resolve_declared(agent.skills)
    for resolution in resolutions.values():
        assert resolution.status == "resolved"
        assert resolution.skill is not None
        assert resolution.skill.scope == "builtin"
        assert read_skill_body(resolution.skill)


def test_document_tool_refuses_outside_workspace_before_preparing_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "private.docx"
    outside.write_text("private")
    monkeypatch.setattr(document_tools, "_workspace", lambda: root)
    monkeypatch.setattr(
        FileAccessPolicy, "from_env", classmethod(lambda cls: FileAccessPolicy((tmp_path,)))
    )
    with pytest.raises(ValueError, match="inside the active workspace"):
        document_tools.build_prepare_document_tool().func(path=str(outside))


@pytest.mark.parametrize("args", [{"action": "execute"}, {"dpi": 999}])
def test_document_tool_rejects_invalid_operations_before_side_effects(args: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        document_tools.build_prepare_document_tool().func(path="missing.pdf", **args)


def test_document_tool_passes_selections_as_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "budget with spaces.xlsx"
    source.write_bytes(b"document")
    monkeypatch.setattr(document_tools, "_workspace", lambda: tmp_path)
    monkeypatch.setattr(
        FileAccessPolicy, "from_env", classmethod(lambda cls: FileAccessPolicy((tmp_path,)))
    )
    monkeypatch.setattr(
        document_tools,
        "prepare_document_runtime",
        lambda root: {"output_directory": str(root / "out")},
    )
    seen: list[str] = []

    def run(runtime: dict[str, Any], args: list[str], *, cwd: Path) -> dict[str, Any]:
        seen.extend(args)
        assert cwd == tmp_path
        return {"status": "failed", "error": "converter missing", "manifest": "out/manifest.json"}

    monkeypatch.setattr(document_tools, "run_document_helper", run)
    result = document_tools.build_prepare_document_tool().func(
        path=source.name,
        sheet="Budget Inputs",
        cell_range="A1:D5",
    )
    assert seen[:2] == ["inspect", str(source)]
    assert seen[seen.index("--sheet") + 1] == "Budget Inputs"
    assert result["status"] == "failed"
