"""Local blueprint development environments must not enter installed revisions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from clio_agent.gact.app import build_app
from clio_agent.gact.blueprint_install_files import copy_blueprint_tree, tree_checksum


def test_runtime_caches_are_neither_copied_nor_hashed(tmp_path: Path) -> None:
    source = tmp_path / "pack"
    source.mkdir()
    (source / "AGENT.md").write_text("source", encoding="utf-8")
    expected = tree_checksum(source)
    for directory in ("impl/.venv", "node_modules", ".git", "impl/__pycache__"):
        runtime = source / directory
        runtime.mkdir(parents=True)
        (runtime / "dependency.bin").write_bytes(b"machine-specific dependency")
    (source / "compiled.pyc").write_bytes(b"bytecode")
    assert tree_checksum(source) == expected
    destination = tmp_path / "installed"
    copy_blueprint_tree(source, destination)
    assert tree_checksum(destination) == expected
    assert not (destination / "impl/.venv").exists()
    assert sorted(path.name for path in destination.rglob("*") if path.is_file()) == ["AGENT.md"]
    (source / "AGENT.md").write_text("real source edit", encoding="utf-8")
    assert tree_checksum(source) != expected


@pytest.mark.asyncio
async def test_capability_projection_does_not_run_discovery_on_the_request_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import clio_agent.gact.routes.system as system

    app = build_app(agent=None, sessions_path=tmp_path / "sessions.json")

    def project(*args: Any, **kwargs: Any) -> dict[str, Any]:
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return {"a2ui_capabilities": {"installed": True}}

    monkeypatch.setattr(system, "capabilities_to_v3", project)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.get("/v1/capabilities", headers={"X-GACT-Version": "0.3"})
    assert response.status_code == 200
    assert response.json()["a2ui_capabilities"]["installed"]
