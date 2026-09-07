"""Durable user-level MCP configuration route contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.routes.mcp import _configured_web_remote_url, _probe_web_search_remote
from clio_agent.tools.mcp_config import load_mcp_servers


def _web_spec(remote_url: str) -> dict[str, Any]:
    return {
        "name": "CLIO Web Search",
        "transport": "stdio",
        "command": "uvx",
        "args": [
            "--from",
            "clio-kit==2.10.5",
            "clio-kit",
            "mcp-server",
            "web",
            "--remote-url",
            remote_url,
        ],
    }


def test_web_remote_readiness_uses_only_the_declared_endpoint(monkeypatch) -> None:
    requested: list[str] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "status": "ready",
                "checks": {"docling": "ready", "searxng": "ready", "grobid": "ready"},
            }

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["timeout"] == 15.0

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def get(self, url: str) -> _Response:
            requested.append(url)
            return _Response()

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    spec = _web_spec("http://search.example:8089")

    assert _configured_web_remote_url(spec) == "http://search.example:8089"
    asyncio.run(_probe_web_search_remote("http://search.example:8089"))
    assert requested == ["http://search.example:8089/readyz"]


def test_user_mcp_configuration_persists_across_restart_and_preserves_siblings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    config_path = user_dir / "mcp.yaml"
    config_path.write_text(
        "custom_setting: keep-me\n"
        "mcp_servers:\n"
        "  sibling:\n"
        "    transport: stdio\n"
        "    command: sibling-command\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLIO_USER_DIR", str(user_dir))

    async def ready_probe(spec: Any) -> tuple[list[str], str | None]:
        del spec
        return ["web_search", "web_fetch"], None

    monkeypatch.setattr(
        "clio_agent.gact.routes.mcp._probe_user_mcp_server",
        ready_probe,
    )

    first_app = build_app(sessions_path=tmp_path / "first.json")
    with TestClient(first_app) as client:
        saved = client.put(
            "/v1/mcp/configuration/web",
            json=_web_spec("http://search.internal:8089"),
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["status"] == "ready"

    second_app = build_app(sessions_path=tmp_path / "second.json")
    with TestClient(second_app) as client:
        restored = client.get("/v1/mcp/configuration/web")
        assert restored.status_code == 200, restored.text
        assert restored.json()["configured"] is True
        assert restored.json()["tools"] == ["web_search", "web_fetch"]

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["custom_setting"] == "keep-me"
    assert document["mcp_servers"]["sibling"]["command"] == "sibling-command"
    assert document["mcp_servers"]["web"]["args"][-1] == "http://search.internal:8089"


def test_edit_replaces_named_server_without_duplicate_runtime_or_yaml_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    user_dir = tmp_path / "user"
    monkeypatch.setenv("CLIO_USER_DIR", str(user_dir))

    async def ready_probe(spec: Any) -> tuple[list[str], str | None]:
        del spec
        return ["web_search"], None

    monkeypatch.setattr("clio_agent.gact.routes.mcp._probe_user_mcp_server", ready_probe)
    app = build_app(sessions_path=tmp_path / "sessions.json")
    app.state.external_mcp_servers = {
        "mcp_ext_old": {
            "name": "CLIO Web Search",
            "status": "ready",
            "transport": "stdio",
            "tools": ["web_search"],
            "spec": {},
        }
    }
    with TestClient(app) as client:
        first = client.put(
            "/v1/mcp/configuration/web",
            json=_web_spec("http://first.internal:8089"),
        )
        second = client.put(
            "/v1/mcp/configuration/web",
            json=_web_spec("http://second.internal:8089"),
        )
        assert first.status_code == second.status_code == 200

    document = yaml.safe_load((user_dir / "mcp.yaml").read_text(encoding="utf-8"))
    assert list(document["mcp_servers"]).count("web") == 1
    assert document["mcp_servers"]["web"]["args"][-1] == "http://second.internal:8089"
    assert app.state.external_mcp_servers == {}


def test_unreachable_configuration_is_saved_and_remains_retryable_after_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    user_dir = tmp_path / "user"
    monkeypatch.setenv("CLIO_USER_DIR", str(user_dir))

    async def unavailable_probe(spec: Any) -> tuple[list[str], str | None]:
        del spec
        return [], "ConnectionError: service offline"

    monkeypatch.setattr(
        "clio_agent.gact.routes.mcp._probe_user_mcp_server",
        unavailable_probe,
    )
    app = build_app(sessions_path=tmp_path / "first.json")
    with TestClient(app) as client:
        saved = client.put(
            "/v1/mcp/configuration/web",
            json=_web_spec("http://offline.internal:8089"),
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["configured"] is True
        assert saved.json()["status"] == "degraded"
        assert saved.json()["retryable"] is True

    restarted = build_app(sessions_path=tmp_path / "second.json")
    with TestClient(restarted) as client:
        status = client.get("/v1/mcp/configuration/web")
        assert status.status_code == 200, status.text
        assert status.json()["status"] == "degraded"
        assert "service offline" in status.json()["error"]


def test_remove_user_override_restores_pack_local_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    user_dir = tmp_path / "user"
    monkeypatch.setenv("CLIO_USER_DIR", str(user_dir))

    async def ready_probe(spec: Any) -> tuple[list[str], str | None]:
        del spec
        return ["web_search"], None

    monkeypatch.setattr("clio_agent.gact.routes.mcp._probe_user_mcp_server", ready_probe)
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        client.put(
            "/v1/mcp/configuration/web",
            json=_web_spec("http://remote.internal:8089"),
        )
        removed = client.delete("/v1/mcp/configuration/web")
        assert removed.status_code == 200, removed.text
        assert removed.json()["status"] == "local_fallback"
        assert removed.json()["configured"] is False

    specs = load_mcp_servers(
        home=tmp_path,
        cwd=tmp_path / "workspace",
        env={"CLIO_USER_DIR": str(user_dir)},
        pack_servers={"base": {"web": "clio-kit mcp-server web"}},
    )
    assert specs["web"].source == "pack:base"
    assert specs["web"].command == "clio-kit"
    assert specs["web"].args == ("mcp-server", "web")


def test_missing_named_configuration_reports_local_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CLIO_USER_DIR", str(tmp_path / "user"))
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        response = client.get("/v1/mcp/configuration/web")

    assert response.status_code == 200
    assert response.json() == {
        "name": "web",
        "configured": False,
        "scope": "user",
        "status": "local_fallback",
        "tools": [],
        "tools_count": 0,
        "retryable": False,
    }
