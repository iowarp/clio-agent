"""Bytes served inline on CLIO's origin never run as an active document."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.proposals import Proposal, promote_proposal
from clio_agent.gact.inline_safety import INLINE_SANDBOX_CSP, is_active_type
from tests.test_gact.test_document_artifacts import _workspace_session

_SCRIPT = "<script>fetch('/v1/mcp/servers', {method: 'POST'})</script>"


@pytest.mark.parametrize(
    "name, body",
    [
        ("index.html", f"<!doctype html><title>x</title>{_SCRIPT}"),
        ("logo.svg", f'<svg xmlns="http://www.w3.org/2000/svg">{_SCRIPT}</svg>'),
        ("data.xml", f'<x:html xmlns:x="http://www.w3.org/1999/xhtml">{_SCRIPT}</x:html>'),
    ],
)
def test_active_workspace_files_are_sandboxed(tmp_path: Path, name: str, body: str) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / name).write_text(body)
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        wid = client.post("/v1/workspaces", json={"name": "p", "root_path": str(root)}).json()["id"]
        response = client.get(f"/v1/workspaces/{wid}/files/read", params={"path": name})
    assert response.status_code == 200
    assert response.text == body  # the bytes are unchanged for the UI's viewers
    assert response.headers["content-security-policy"] == INLINE_SANDBOX_CSP
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "name, body",
    [
        ("notes.txt", b"plain"),
        ("plot.png", b"\x89PNG\r\n\x1a\n"),
        # Detected as text/plain, which nosniff keeps inert.
        ("page.xhtml", b'<html xmlns="http://www.w3.org/1999/xhtml"><script>x()</script></html>'),
    ],
)
def test_passive_workspace_files_get_nosniff_only(tmp_path: Path, name: str, body: bytes) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / name).write_bytes(body)
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        wid = client.post("/v1/workspaces", json={"name": "p", "root_path": str(root)}).json()["id"]
        response = client.get(f"/v1/workspaces/{wid}/files/read", params={"path": name})
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in response.headers


def test_an_html_artifact_is_sandboxed(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        app = cast(FastAPI, client.app)
        workspace, session = _workspace_session(client, root)
        result = promote_proposal(
            app,
            session,
            Proposal(name="report.html", content=f"<html>{_SCRIPT}</html>", purpose="deliverable"),
            workspace_id=workspace,
            turn_id="t1",
            agent_id="main",
        )
        assert result.accepted and result.version is not None
        response = client.get(f"/v1/artifacts/{result.version.artifact_id}/bytes")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert response.headers["content-security-policy"] == INLINE_SANDBOX_CSP
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "media_type, active",
    [
        ("text/html; charset=utf-8", True),
        ("IMAGE/SVG+XML", True),
        ("application/rss+xml", True),
        ("text/xml", True),
        ("text/plain", False),
        ("application/json", False),
        ("image/png", False),
        (None, False),
    ],
)
def test_active_types(media_type: str | None, active: bool) -> None:
    assert is_active_type(media_type) is active
