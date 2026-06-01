"""POST /v1/sessions/{sid}/attachments — upload bytes into the workspace.

Bytes arrive base64-in-JSON (the CLIO Desktop transport bridge only
forwards UTF-8 string bodies, so multipart cannot survive the shipped
desktop / an SSH tunnel). The upload is written under
``{workspace_root}/.clio/attachments/{sid}/`` and registered as a
context file, so the existing agent read path consumes it next turn.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


class _RecordingAgent:
    """Captures every question forwarded to it (mirror test_context_injection)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def forward(self, question: str, session_id: str = "default"):
        self.calls.append((question, session_id))
        return type(
            "Pred",
            (),
            {"answer": "ok", "selected_expert": "", "routing_rationale": ""},
        )()


@pytest.fixture()
def setup(tmp_path: Path):
    agent = _RecordingAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    # Pin ws_default's root to tmp_path so uploads land under the policy boundary.
    app.state.workspaces.update("ws_default", root_path=str(tmp_path))
    return app, TestClient(app), agent, tmp_path


def _upload(c: TestClient, sid: str, *, filename: str, content: bytes, mode: str = "read"):
    return c.post(
        f"/v1/sessions/{sid}/attachments",
        json={
            "file": base64.b64encode(content).decode("ascii"),
            "filename": filename,
            "mode": mode,
        },
    )


def test_upload_writes_into_workspace_and_registers(setup) -> None:
    app, c, _agent, tmp_path = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

    resp = _upload(c, sid, filename="notes.md", content=b"hello body")

    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["mode"] == "read"
    assert row["path"].replace("\\", "/").endswith(f".clio/attachments/{sid}/notes.md")
    # Bytes really landed on disk inside the workspace.
    on_disk = tmp_path / ".clio" / "attachments" / sid / "notes.md"
    assert on_disk.is_file()
    assert on_disk.read_bytes() == b"hello body"
    # And it's in the context-file ledger.
    listed = c.get(f"/v1/sessions/{sid}/context/files").json()["files"]
    assert any(f["path"] == row["path"] for f in listed)


def test_upload_consumed_by_agent_next_turn(setup) -> None:
    from .conftest import complete_turn

    app, c, agent, _tmp = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

    _upload(c, sid, filename="insight.txt", content=b"important upload insight")
    complete_turn(c, sid, "summarise")

    seen, _ = agent.calls[-1]
    # The end-to-end proof: uploaded bytes reach the agent's prompt via the
    # existing context-file read path, with no agent-layer change.
    assert "important upload insight" in seen
    assert "summarise" in seen


def test_upload_path_traversal_is_confined_to_attachments_dir(setup) -> None:
    app, c, _agent, tmp_path = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
    base = tmp_path / ".clio" / "attachments" / sid

    # Posix- and Windows-style traversal attempts.
    for bad in ("../../evil.txt", "..\\..\\evil2.txt"):
        resp = _upload(c, sid, filename=bad, content=b"pwned")
        assert resp.status_code == 200, resp.text
        landed = resp.json()["path"]
        # Reduced to a bare basename inside the attachments dir.
        assert landed.replace("\\", "/").startswith(f".clio/attachments/{sid}/")
        assert "evil" in Path(landed).name

    # NOTHING was written outside the attachments dir.
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path.parent / "evil.txt").exists()
    assert not (tmp_path / "evil2.txt").exists()
    assert (base / "evil.txt").is_file()
    assert (base / "evil2.txt").is_file()


def test_upload_rejects_dotdot_only_and_empty_names(setup) -> None:
    app, c, _agent, _tmp = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
    for bad in ("..", "", "   "):
        resp = _upload(c, sid, filename=bad, content=b"x")
        assert resp.status_code == 422, f"{bad!r} -> {resp.status_code}"


def test_upload_collision_does_not_overwrite(setup) -> None:
    app, c, _agent, tmp_path = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

    first = _upload(c, sid, filename="notes.md", content=b"one").json()
    second = _upload(c, sid, filename="notes.md", content=b"two").json()

    assert first["path"] != second["path"]
    base = tmp_path / ".clio" / "attachments" / sid
    assert (base / "notes.md").read_bytes() == b"one"
    assert (base / "notes (2).md").read_bytes() == b"two"
    listed = c.get(f"/v1/sessions/{sid}/context/files").json()["files"]
    assert len(listed) == 2


def test_upload_oversize_rejected(setup) -> None:
    app, c, _agent, _tmp = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

    resp = _upload(c, sid, filename="big.bin", content=b"\x00" * (25 * 1024 * 1024 + 1))

    assert resp.status_code == 413
    assert resp.json()["error"]["error"] == "payload_too_large"


def test_upload_emits_context_file_added(setup) -> None:
    app, c, _agent, _tmp = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

    _upload(c, sid, filename="notes.md", content=b"hello")

    history = app.state.bus._history.get(sid, [])
    added = [e for e in history if e.type == "context.file.added"]
    assert len(added) == 1
    assert added[0].payload["file"]["mode"] == "read"


def test_upload_bad_base64_rejected(setup) -> None:
    app, c, _agent, _tmp = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

    resp = c.post(
        f"/v1/sessions/{sid}/attachments",
        json={"file": "!!!not base64!!!", "filename": "x.txt"},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["error"] == "bad_request"


def test_capabilities_advertise_attachments_upload(setup) -> None:
    app, c, _agent, _tmp = setup
    caps = c.get("/v1/capabilities").json()["capabilities"]
    assert caps["attachments_upload"] is True
