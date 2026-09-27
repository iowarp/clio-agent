"""The ``download_media`` native tool: an external media file becomes a workspace file.

A2UI viewers never auto-load an external URL (release 0.9.4.19), so an agent
that wants to SHOW web media saves it into the workspace and references that
path, which the producer exports as an artifact. These tests drive the real
tool callable in-memory: a mock transport replaces the network, a stub
permission gate replaces the interactive prompt, and the workspace is a pytest
temp dir bound exactly the way a live turn binds it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from clio_agent.gact import context as gact_context
from clio_agent.gact import media_download_tool as tool_module
from clio_agent.gact.agents.declared_native_tools import (
    DECLARABLE_NATIVE_TOOLS,
    resolve_declared_native_tools,
)
from clio_agent.gact.media_download_tool import MEDIA_DOWNLOAD_TOOL, build_download_media_tool
from clio_agent.gact.types import AgentDef
from clio_agent.tools.execution import tool_workspace_context

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class _Gate:
    def __init__(self, decision: str = "allow") -> None:
        self.decision = decision
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, name: str, args: dict[str, Any]) -> str:
        self.calls.append((name, dict(args)))
        return self.decision


@pytest.fixture
def gate() -> _Gate:
    return _Gate()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: _Gate) -> Iterator[Path]:
    root = tmp_path / "ws"
    root.mkdir()
    app = SimpleNamespace(state=SimpleNamespace(pending_permission_gate=gate))
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: "sess_test")
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(root))
    with tool_workspace_context(root):
        yield root


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes = PNG,
    content_type: str = "image/png",
    status: int = 200,
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, content=body, headers={"content-type": content_type})

    monkeypatch.setattr(tool_module, "_TRANSPORT_FACTORY", lambda: httpx.MockTransport(handler))
    monkeypatch.setattr(tool_module, "_RESOLVE", lambda _host, _port: ["93.184.215.14"])
    return seen


def _download(**kwargs: Any) -> dict[str, Any]:
    return build_download_media_tool()(**kwargs)


def test_downloads_into_the_workspace_and_reports_the_path(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, gate: _Gate
) -> None:
    _serve(monkeypatch)

    result = _download(url="https://upload.example.org/wiki/Cat.png")

    assert result["ok"] is True, result
    assert result["path"] == "downloads/Cat.png"
    assert (workspace / "downloads" / "Cat.png").read_bytes() == PNG
    assert result["media_type"] == "image/png"
    assert result["size_bytes"] == len(PNG)
    assert result["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert result["source_url"] == "https://upload.example.org/wiki/Cat.png"
    # The result tells the model how the file reaches a viewer.
    assert "artifact" in result["hint"]
    assert gate.calls == [
        (MEDIA_DOWNLOAD_TOOL, {"url": "https://upload.example.org/wiki/Cat.png", "path": ""})
    ]


def test_an_explicit_workspace_path_is_honoured(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch)
    result = _download(url="https://example.org/x", path="figures/cat.png")
    assert result["ok"] is True, result
    assert result["path"] == "figures/cat.png"
    assert (workspace / "figures" / "cat.png").read_bytes() == PNG


def test_a_derived_name_gets_the_media_extension_and_never_overwrites(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch)
    first = _download(url="https://example.org/render?id=7")
    second = _download(url="https://example.org/render?id=7")
    assert first["path"] == "downloads/render.png"
    assert second["path"] == "downloads/render-1.png"


def test_an_existing_explicit_destination_is_refused(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _serve(monkeypatch)
    (workspace / "cat.png").write_bytes(b"keep me")
    result = _download(url="https://example.org/cat.png", path="cat.png")
    assert result["ok"] is False
    assert result["reason"] == "media_download_destination_exists"
    assert (workspace / "cat.png").read_bytes() == b"keep me"
    assert seen == []


def test_a_destination_outside_the_workspace_is_refused(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _serve(monkeypatch)
    result = _download(url="https://example.org/cat.png", path="../escape.png")
    assert result["ok"] is False
    assert result["reason"] == "media_download_outside_workspace"
    assert not (workspace.parent / "escape.png").exists()
    assert seen == []


def test_the_file_policy_allowed_roots_are_enforced(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from clio_agent.tools.file_policy import FileAccessPolicy

    seen = _serve(monkeypatch)
    # from_env always grants the active workspace (file_policy's
    # _with_active_workspace), so prove the tool consults the policy by
    # handing it one whose allowed roots exclude the workspace.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(
        FileAccessPolicy,
        "from_env",
        classmethod(lambda cls: cls(allowed_roots=(elsewhere.resolve(),))),
    )
    result = _download(url="https://example.org/cat.png", path="cat.png")
    assert result["ok"] is False
    assert result["reason"] == "media_download_file_policy"
    assert result["policy_code"] == "outside_allowed_roots"
    assert not (workspace / "cat.png").exists()
    assert seen == []


def test_the_permission_gate_can_deny(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, gate: _Gate
) -> None:
    seen = _serve(monkeypatch)
    gate.decision = "deny"
    result = _download(url="https://example.org/cat.png")
    assert result["ok"] is False
    assert result["reason"] == "media_download_permission_denied"
    assert seen == []
    assert not (workspace / "downloads").exists()


def test_a_fetch_refusal_leaves_no_partial_file(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, body=b"<html>nope</html>", content_type="text/html")
    result = _download(url="https://example.org/page")
    assert result["ok"] is False
    assert result["reason"] == "media_download_content_type_not_allowed"
    downloads = workspace / "downloads"
    assert not downloads.exists() or not any(downloads.iterdir())


def test_the_size_limit_is_the_smaller_of_config_and_file_policy(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, body=PNG + b"\x00" * 200)
    monkeypatch.setenv("CLIO_MEDIA_DOWNLOAD_MAX_BYTES", "10MiB")
    monkeypatch.setenv("CLIO_MAX_FILE_SIZE_BYTES", "100")
    result = _download(url="https://example.org/big.png")
    assert result["ok"] is False
    assert result["reason"] == "media_download_too_large"


def test_no_workspace_is_a_typed_refusal(monkeypatch: pytest.MonkeyPatch, gate: _Gate) -> None:
    app = SimpleNamespace(state=SimpleNamespace(pending_permission_gate=gate))
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    with tool_workspace_context(None):
        result = _download(url="https://example.org/cat.png")
    assert result["reason"] == "media_download_workspace_unavailable"


def test_outside_a_gact_session_there_is_no_gate_so_it_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _serve(monkeypatch)
    monkeypatch.setattr(gact_context, "active_app", lambda: None)
    with tool_workspace_context(tmp_path):
        result = _download(url="https://example.org/cat.png")
    assert result["reason"] == "media_download_session_unavailable"
    assert seen == []


def test_the_tool_is_declarable_and_presents_as_a_workspace_file() -> None:
    assert MEDIA_DOWNLOAD_TOOL in DECLARABLE_NATIVE_TOOLS
    agent = AgentDef(id="leaf", title="Leaf", tools=[MEDIA_DOWNLOAD_TOOL])
    requested, native, gateway = resolve_declared_native_tools(agent, {})
    assert requested == [MEDIA_DOWNLOAD_TOOL]
    assert list(native) == [MEDIA_DOWNLOAD_TOOL]
    assert gateway == []
    tool = native[MEDIA_DOWNLOAD_TOOL]
    assert set(tool.args) == {"url", "path"}
    assert "workspace" in (tool.desc or "")


def test_root_agents_that_produce_surfaces_get_the_tool() -> None:
    from clio_agent.gact.agents.auto_tools import build_auto_react_tools

    root = AgentDef(id="main", title="Main", tools=[])
    with_producers = [t.name for t in build_auto_react_tools(root, a2ui_producers=True)]
    without = [t.name for t in build_auto_react_tools(root, a2ui_producers=False)]
    assert MEDIA_DOWNLOAD_TOOL in with_producers
    assert MEDIA_DOWNLOAD_TOOL not in without
    declared = AgentDef(id="main", title="Main", tools=[MEDIA_DOWNLOAD_TOOL])
    names = [t.name for t in build_auto_react_tools(declared, a2ui_producers=True)]
    assert MEDIA_DOWNLOAD_TOOL not in names  # the declared-native resolver attaches it


def test_blueprint_validation_accepts_declared_native_tools() -> None:
    from clio_agent.gact.agent_blueprints import _validate_agent_tool_references

    row = AgentDef(id="leaf", title="Leaf", tools=[MEDIA_DOWNLOAD_TOOL, "view_image"])
    [validated] = _validate_agent_tool_references([row], mcp_descriptors=[])
    assert validated.validation_errors == []
