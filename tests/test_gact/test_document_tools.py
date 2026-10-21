"""The default agent can prepare documents without bypassing workspace policy."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact import context, document_tools
from clio_agent.gact.agents.native_tool_registry import build_native_tools
from clio_agent.gact.agents.resolution import resolution_agent_def, session_resolution
from clio_agent.gact.app import build_app
from clio_agent.plugins.service import PluginService
from clio_agent.tools.file_policy import FileAccessPolicy
from tests._plugin_fixtures import FIXTURE, custom_marketplace


def test_default_document_tools_are_available_without_vision() -> None:
    """Text-capable models can create and inspect documents; pixels remain gated."""
    resolution = PluginService().resolve("clio/standard", pin=False)
    agent = resolution_agent_def(resolution)
    claimed = [row["name"] for row in resolution.tools if row["kind"] == "native"]
    assert {
        "prepare_document",
        "prepare_document_runtime",
        "prepare_execution_runtime",
        "view_image",
    } <= set(claimed)
    available, gateway, withheld = build_native_tools(agent, claimed, {}, supports_vision=False)
    assert {"prepare_document", "prepare_document_runtime", "prepare_execution_runtime"} <= set(
        available
    )
    assert "view_image" not in available
    assert withheld["view_image"] == "model_lacks_image_input"
    assert not {"prepare_document", "prepare_document_runtime"} & set(gateway)


def test_execution_and_document_discovery_describe_their_actual_checks() -> None:
    """Every agent receives the same accurate discovery contract from the tool builder."""
    execution = document_tools.build_prepare_execution_runtime_tool()
    documents = document_tools.build_prepare_document_runtime_tool()
    assert "without importing every package" in execution.desc
    assert "Converter and font probes are omitted" in execution.desc
    assert "not required before every shell call" in execution.desc
    assert "inspect current converters and fonts" in documents.desc
    assert "probes are omitted" not in documents.desc


def test_execution_discovery_reports_missing_imports_and_keeps_commands_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prepared toolchain is not advertised as ready for an unavailable import."""
    inventory = {
        "status": "ready",
        "python": "managed-python",
        "python_argv": ["uv", "run", "--no-project", "--python", "managed-python", "python"],
        "packages": {"numpy": {"version": "locked", "import": "numpy"}},
        "font_files": [f"C:/Windows/Fonts/font-{index}.ttf" for index in range(150)],
        "font_families": ["Arial"],
        "shell_environment": {"PATH": "prepared tool paths;" * 500},
    }
    monkeypatch.setattr(document_tools, "_workspace", lambda: tmp_path)

    def prepare(root: Path, *, discovery_only: bool = False) -> dict[str, Any]:
        assert root == tmp_path
        assert discovery_only
        return inventory

    monkeypatch.setattr(document_tools, "prepare_document_runtime", prepare)

    def probe(python: Path, modules: list[str]) -> dict[str, dict[str, str]]:
        assert python == Path("managed-python")
        assert modules == ["numpy", "cv2"]
        return {"numpy": {"status": "ready"}, "cv2": {"status": "missing"}}

    monkeypatch.setattr(document_tools, "inspect_python_imports", probe)
    tool = document_tools.build_prepare_execution_runtime_tool()
    result = tool.func(required_imports=["numpy", "cv2"])
    assert result["status"] == "missing_dependencies"
    assert result["required_imports"]["cv2"]["status"] == "missing"
    assert result["python_argv"] == inventory["python_argv"]
    assert "font_files" not in result
    assert inventory["font_files"]
    assert "shell_environment" not in result
    assert inventory["shell_environment"]
    assert "--with" in result["dependency_guidance"]


@pytest.mark.usefixtures("host_agent_executor")
def test_document_runtime_reports_the_sessions_plugin_skill_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Document discovery names the folders of the skills the session's plugins ship."""
    plugin_files = {
        "ai.iowarp.clio/tools/builtin.py": (
            FIXTURE / "plugins/document-runtime/ai.iowarp.clio/tools/builtin.py"
        ).read_text(encoding="utf-8"),
        "skills/work-with-pdfs/SKILL.md": (
            "---\nname: work-with-pdfs\ndescription: Inspect PDFs.\n---\nRun the helper.\n"
        ),
        "skills/work-with-pdfs/scripts/prepare_pdf.py": "print('ok')\n",
    }
    custom_marketplace(
        tmp_path,
        monkeypatch,
        plugins={
            "document-runtime": {"ext": {"requires": ["shell"]}, "files": plugin_files},
        },
    )
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(document_tools, "_workspace", lambda: root)
    monkeypatch.setattr(
        document_tools,
        "prepare_document_runtime",
        lambda workspace, **_: {"status": "ready", "python": "managed-python"},
    )
    tool = document_tools.build_prepare_document_runtime_tool()
    # No active session: there are no plugin skills to report.
    assert tool.func()["skills"] == {}
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        app = cast(FastAPI, client.app)
        workspace = client.post("/v1/workspaces", json={"name": "docs", "root_path": str(root)})
        assert workspace.status_code == 201, workspace.text
        created = client.post("/v1/sessions", json={"workspace_id": workspace.json()["id"]})
        assert created.status_code == 200, created.text
        session_id = created.json()["id"]
        resolution = session_resolution(app, session_id)
        (row,) = [row for row in resolution.skills if row["id"] == "work-with-pdfs"]
        assert row["plugin"] == "document-runtime"
        app_token = context.set_app(app)
        session_token = context.set_session_id(session_id)
        try:
            runtime = tool.func()
        finally:
            context.reset(session_token)
            context.reset(app_token)
    folder = Path(runtime["skills"]["work-with-pdfs"])
    assert folder == Path(row["path"]).parent
    assert (folder / "SKILL.md").is_file()
    assert (folder / "scripts" / "prepare_pdf.py").is_file()
    assert set(runtime["skills"]) == {skill["id"] for skill in resolution.skills}
    # Execution discovery is not the document inventory and names no skills.
    execution = document_tools.build_prepare_execution_runtime_tool()
    assert "skills" not in execution.func()


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
