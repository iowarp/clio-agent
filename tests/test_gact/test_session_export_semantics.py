"""Session export must preserve evidence and keep file scopes distinct."""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message as ASGIMessage

from clio_agent.errors import ClioError
from clio_agent.gact.artifacts.minting import mint_artifact
from clio_agent.gact.artifacts.records import ArtifactKind, Custody, IdentityEvidence, Mechanism
from clio_agent.gact.artifacts.registry import ArtifactRegistry
from clio_agent.gact.auth import BearerAuthMiddleware
from clio_agent.gact.messages import MessageStore
from clio_agent.gact.routes.session_export import (
    _ExportFileResponse,
    register_session_export_routes,
)
from clio_agent.gact.semantic_events import SemanticEvent
from clio_agent.gact.semantic_trace_file import FileSemanticTraceBackend
from clio_agent.gact.session_export import build_transcript
from clio_agent.gact.session_export_archive import build_archive
from clio_agent.gact.session_export_downloads import ExportDownloads
from clio_agent.gact.sessions import SessionStore
from clio_agent.gact.types import Message, Part
from clio_agent.gact.workspaces import WorkspaceStore
from clio_agent.tools.servers.shell_spill_store import spill_directory


@pytest.fixture
def export_app(tmp_path: Path) -> tuple[FastAPI, str, str]:
    app = FastAPI()
    app.state.sessions = SessionStore(path=tmp_path / "state" / "sessions.json")
    app.state.workspaces = WorkspaceStore(path=tmp_path / "state" / "workspaces.json")
    root = tmp_path / "source"
    root.mkdir()
    workspace = app.state.workspaces.create(name="source", root_path=str(root))
    session = app.state.sessions.create(workspace_id=workspace.id, title="review")
    app.state.messages = {}
    app.state.context_files = {}
    app.state.semantic_trace_backend = FileSemanticTraceBackend(tmp_path / "state" / "traces")
    app.state.artifact_registry = ArtifactRegistry()
    register_session_export_routes(app)
    return app, session.id, workspace.id


def emit(app: FastAPI, sid: str, kind: str, payload: dict[str, Any]) -> None:
    app.state.semantic_trace_backend.emit(
        SemanticEvent(event_type=kind, session_id=sid, trace_id="trace_test", payload=payload)
    )


def mint(app: FastAPI, sid: str, wid: str, name: str, content: bytes) -> str:
    root = Path(app.state.workspaces.get(wid).root_path)
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    version = mint_artifact(
        app,
        sid,
        name=name,
        kind=ArtifactKind.OTHER,
        path=str(path),
        workspace_id=wid,
        mechanism=Mechanism.TOOL_SCHEMA,
        custody=Custody.WORKSPACE_REFERENCED,
        evidence=IdentityEvidence.hashed_at_use(
            sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content)
        ),
        producer={"session_id": sid},
    )
    assert version is not None
    return version.artifact_id


def test_full_tool_values_and_loaded_skill_survive_projection_and_restart(
    export_app: tuple[FastAPI, str, str],
) -> None:
    app, sid, _ = export_app
    full = "procedure\n" * 10000
    app.state.messages[sid] = [
        Message(
            id="m1",
            session_id=sid,
            role="assistant",
            created_at="2026-10-05T00:00:00Z",
            updated_at="2026-10-05T00:00:00Z",
            parts=[
                Part(
                    id="p1",
                    type="tool_call",
                    call_id="call1",
                    name="load_skill",
                    input={"skill_id": "guide"},
                )
            ],
        )
    ]
    emit(
        app,
        sid,
        "tool.call.started",
        {"call_id": "call1", "tool": "load_skill", "args": {"skill_id": "guide", "long": full}},
    )
    emit(app, sid, "skill.loaded", {"skill_id": "guide", "path": "old/skill.md"})
    emit(
        app, sid, "tool.call.completed", {"call_id": "call1", "tool": "load_skill", "result": full}
    )
    app.state.semantic_trace_backend.flush()
    app.state.semantic_trace_backend = FileSemanticTraceBackend(
        app.state.semantic_trace_backend.path
    )
    blob = build_transcript(app, sid)
    assert blob["tool_records"][0]["input"]["long"] == full
    assert blob["tool_records"][0]["output"] == full
    assert blob["loaded_skills"][0]["content"] == full
    assert blob["loaded_skills"][0]["content_source"] == "recorded_tool_response"
    assert not blob["recording"]["issues"]


def test_descendants_and_missing_historical_records_are_explicit(
    export_app: tuple[FastAPI, str, str],
) -> None:
    app, sid, wid = export_app
    child = app.state.sessions.create(workspace_id=wid, title="delegate", parent_session_id=sid)
    emit(app, child.id, "skill.loaded", {"skill_id": "historical", "path": "missing"})
    app.state.messages[child.id] = [
        Message(
            id="child_m",
            session_id=child.id,
            role="assistant",
            created_at="2026-10-05T00:00:00Z",
            updated_at="2026-10-05T00:00:00Z",
            parts=[Part(id="old_call", type="tool_call", name="fs_read", input={"path": "old"})],
        )
    ]
    blob = build_transcript(app, sid)
    assert blob["children"][0]["messages"][0]["id"] == "child_m"
    assert {issue["reason"] for issue in blob["children"][0]["recording"]["issues"]} == {
        "historical_tool_record_only",
        "skill_content_not_recorded",
    }


def test_parallel_legacy_skill_loads_keep_their_own_procedures(
    export_app: tuple[FastAPI, str, str],
) -> None:
    app, sid, _ = export_app
    for cid, name in (("a", "exports"), ("b", "evidence")):
        emit(
            app,
            sid,
            "tool.call.started",
            {"tool": "load_skill", "call_id": cid, "args": {"kwargs": {"skill_id": name}}},
        )
    for name in ("exports", "evidence"):
        emit(app, sid, "skill.loaded", {"skill_id": name})
    for cid, name in (("b", "evidence"), ("a", "exports")):
        emit(
            app,
            sid,
            "tool.call.completed",
            {"tool": "load_skill", "call_id": cid, "result": f"# Skill: {name}"},
        )
    skills = build_transcript(app, sid)["loaded_skills"]
    assert [(skill["skill_id"], skill["content"], skill["call_id"]) for skill in skills] == [
        ("exports", "# Skill: exports", "a"),
        ("evidence", "# Skill: evidence", "b"),
    ]


def test_ambiguous_legacy_skill_load_remains_an_explicit_gap(
    export_app: tuple[FastAPI, str, str],
) -> None:
    app, sid, _ = export_app
    for cid in ("a", "b"):
        emit(
            app,
            sid,
            "tool.call.started",
            {"tool": "load_skill", "call_id": cid, "args": {"skill_id": "same"}},
        )
    emit(app, sid, "skill.loaded", {"skill_id": "same"})
    for cid in ("a", "b"):
        emit(app, sid, "tool.call.completed", {"tool": "load_skill", "call_id": cid, "result": cid})
    blob = build_transcript(app, sid)
    assert "content" not in blob["loaded_skills"][0]
    assert blob["recording"]["issues"][0]["reason"] == "skill_content_not_recorded"


def test_effects_excludes_other_sessions_and_untouched_versions(
    export_app: tuple[FastAPI, str, str],
) -> None:
    app, sid, wid = export_app
    own = mint(app, sid, wid, "report.txt", b"ours")
    sibling = app.state.sessions.create(workspace_id=wid, title="other")
    other = mint(app, sibling.id, wid, "other.txt", b"other")
    emit(app, sid, "artifact.used", {"artifact_id": own})
    path = build_archive(app, build_transcript(app, sid), "effects")
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert [item["artifact_id"] for item in manifest["artifacts"]] == [own]
            assert all(other not in name for name in archive.namelist())
            assert archive.read(f"effects/{own}/report.txt") == b"ours"
            for row in manifest["files"]:
                assert (
                    hashlib.sha256(archive.read(row["archive_path"])).hexdigest() == row["sha256"]
                )
    finally:
        path.unlink()


@pytest.mark.parametrize("mode", ["transcript", "effects", "full"])
def test_complete_tool_outputs_are_portable_and_session_scoped(
    export_app: tuple[FastAPI, str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: Any
) -> None:
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "home"))
    app, sid, wid = export_app
    root = app.state.workspaces.get(wid).root_path
    folder = spill_directory(root, session_id=sid)
    folder.mkdir(parents=True)
    output = folder / "call.stdout.txt"
    output.write_text("complete output\n" * 500, encoding="utf-8")
    sibling = spill_directory(root, session_id="another-session")
    sibling.mkdir()
    (sibling / "private.txt").write_text("another session", encoding="utf-8")
    app.state.messages[sid] = [
        Message(
            id="m",
            session_id=sid,
            role="assistant",
            created_at="2026-10-05T00:00:00Z",
            updated_at="2026-10-05T00:00:00Z",
            parts=[Part(id="result", type="tool_result", call_id="call")],
        )
    ]
    emit(app, sid, "tool.call.started", {"call_id": "call", "tool": "shell", "args": {}})
    emit(
        app,
        sid,
        "tool.call.completed",
        {
            "call_id": "call",
            "tool": "shell",
            "result": {
                "stdout": "short excerpt",
                "stdout_spill": {"status": "spilled", "path": str(output)},
            },
        },
    )
    path = build_archive(app, build_transcript(app, sid), mode)
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert len(manifest["tool_output_files"]) == 1
            entry = manifest["tool_output_files"][0]
            assert archive.read(entry["archive_path"]) == output.read_bytes()
            assert "complete output" in archive.read("index.html").decode()
            assert (
                "Complete saved output" in archive.read("index.html").decode()
                if mode == "transcript"
                else "Open complete saved output" in archive.read("index.html").decode()
            )
            assert not any("another-session" in name for name in archive.namelist())
    finally:
        path.unlink()


def test_recorded_spill_paths_never_authorize_reads(
    export_app: tuple[FastAPI, str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "home"))
    app, sid, _ = export_app
    private = tmp_path / "private.txt"
    private.write_text("DO NOT EXPORT", encoding="utf-8")
    emit(
        app,
        sid,
        "tool.call.completed",
        {
            "call_id": "bad",
            "tool": "shell",
            "result": {"stdout_spill": {"status": "spilled", "path": str(private)}},
        },
    )
    path = build_archive(app, build_transcript(app, sid), "transcript")
    try:
        with zipfile.ZipFile(path) as archive:
            assert not json.loads(archive.read("manifest.json"))["tool_output_files"]
            assert "DO NOT EXPORT" not in archive.read("index.html").decode()
    finally:
        path.unlink()


def test_transcript_has_no_effect_payloads_and_full_includes_workspace(
    export_app: tuple[FastAPI, str, str],
) -> None:
    app, sid, wid = export_app
    mint(app, sid, wid, "report.txt", b"ours")
    root = Path(app.state.workspaces.get(wid).root_path)
    (root / "original.csv").write_text("id,value\n1,2\n", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "private.txt").write_text("dependency", encoding="utf-8")
    for mode in ("transcript", "full"):
        path = build_archive(app, build_transcript(app, sid), mode)
        try:
            with zipfile.ZipFile(path) as archive:
                assert (f"workspace/{wid}/0/original.csv" in archive.namelist()) is (mode == "full")
                assert not any(".venv" in name for name in archive.namelist())
                if mode == "transcript":
                    assert not any(name.startswith("effects/") for name in archive.namelist())
        finally:
            path.unlink()


def test_changed_artifact_fails_and_temp_archive_is_removed(
    export_app: tuple[FastAPI, str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app, sid, wid = export_app
    mint(app, sid, wid, "report.txt", b"original")
    (Path(app.state.workspaces.get(wid).root_path) / "report.txt").write_bytes(b"changed")
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    with pytest.raises(ValueError, match="recorded artifact bytes changed"):
        build_archive(app, build_transcript(app, sid), "effects")
    assert not list(tmp_path.glob("clio-session-export-*.zip"))


def test_routes_download_zip_and_reject_invalid_modes(export_app: tuple[FastAPI, str, str]) -> None:
    app, sid, _ = export_app
    with TestClient(app) as client:
        assert (
            client.get(f"/v1/sessions/{sid}/export").json()["export_schema"]
            == "clio.session-export.v2"
        )
        response = client.get(f"/v1/sessions/{sid}/export?mode=transcript")
        assert response.status_code == 200
        assert response.content.startswith(b"<!doctype html>")
        assert "text/html" in response.headers["content-type"]
        assert "transcript.html" in response.headers["content-disposition"]
        bundled = client.get(f"/v1/sessions/{sid}/export?mode=effects")
        assert bundled.content.startswith(b"PK")
        assert "effects.zip" in bundled.headers["content-disposition"]
        assert client.get(f"/v1/sessions/{sid}/export?mode=invalid").status_code == 422
        assert client.get("/v1/sessions/missing/export").status_code == 404
        response = client.post(
            f"/v1/sessions/{sid}/export",
            json={
                "visual_review": {
                    "javascript": "console.log('review')",
                    "stylesheet": "",
                    "snapshot": {},
                }
            },
        )
        assert response.status_code == 200


def test_visual_bootstrap_preserves_text_without_executable_interpolation(
    export_app: tuple[FastAPI, str, str],
) -> None:
    app, sid, _ = export_app
    app.state.messages[sid] = [
        Message(
            id="m",
            session_id=sid,
            role="user",
            created_at="2026-10-05T00:00:00Z",
            updated_at="2026-10-05T00:00:00Z",
            parts=[Part(id="p", type="text", text="</script><script>fetch('bad')</script>")],
        )
    ]
    emit(app, sid, "test.record", {"value": "full raw trace"})
    path = build_archive(
        app,
        build_transcript(app, sid),
        "transcript",
        {"javascript": "console.log('review')", "stylesheet": "", "snapshot": {}},
    )
    try:
        with zipfile.ZipFile(path) as archive:
            html = archive.read("index.html").decode()
            assert "</script><script>fetch" not in html
            assert 'id="archive-document"' in html
            assert "&lt;script&gt;" in html
            assert "<script src=" not in html
            assert '<link rel="stylesheet"' not in html
            assert 'id="evidence"' in html
            assert "evidence.html" not in archive.namelist()
            bootstrap = re.search(r"<script>(window.CLIO_EXPORT_DATA=.*?;)</script>", html).group(1)
            encoded = json.loads(
                bootstrap.removeprefix("window.CLIO_EXPORT_DATA=").removesuffix(";")
            )
            review = json.loads(gzip.decompress(base64.b64decode(encoded)))
            assert review["transcript"]["messages"][0]["parts"][0]["text"] == (
                "</script><script>fetch('bad')</script>"
            )
            assert "<" not in bootstrap
            assert review["transcript"]["semantic_events"][0]["payload"] == {
                "value": "full raw trace"
            }
            assert json.loads(archive.read("transcript.json"))["semantic_events"][0]["payload"] == {
                "value": "full raw trace"
            }
            manifest = json.loads(archive.read("manifest.json"))
            for entry in manifest["files"]:
                assert (
                    hashlib.sha256(archive.read(entry["archive_path"])).hexdigest()
                    == entry["sha256"]
                )
            import html as html_module

            assert "connect-src 'none'" in html_module.unescape(html)
            assert "script-src 'sha256-" in html_module.unescape(html)
    finally:
        path.unlink()


def test_prepared_download_has_only_one_file_capability(
    export_app: tuple[FastAPI, str, str],
) -> None:
    app, sid, _ = export_app
    app.add_middleware(BearerAuthMiddleware, token="test-export-bearer", state=app.state)
    body = {"visual_review": {"javascript": "review()", "stylesheet": "", "snapshot": {}}}
    with TestClient(app) as client:
        assert client.post(f"/v1/sessions/{sid}/export-download", json=body).status_code == 401
        prepared = client.post(
            f"/v1/sessions/{sid}/export-download",
            json=body,
            headers={"Authorization": "Bearer test-export-bearer"},
        )
        assert prepared.status_code == 200
        ticket_path = prepared.json()["download_path"]
        assert "test-export-bearer" not in ticket_path
        assert client.post(ticket_path).status_code == 401
        response = client.get(ticket_path)
        assert response.status_code == 200
        assert prepared.json()["filename"].endswith(".transcript.html")
        assert response.content.startswith(b"<!doctype html>")
        assert response.headers["cache-control"] == "no-store"
        assert client.get(ticket_path).status_code == 401
        assert not app.state.session_export_downloads._items


def test_download_expiry_and_capacity_remove_their_archives(tmp_path: Path) -> None:
    pool = ExportDownloads(max_pending=1)
    first = tmp_path / "first.zip"
    first.write_bytes(b"zip")
    path = pool.add(first, "first.zip")
    second = tmp_path / "second.zip"
    second.write_bytes(b"zip")
    with pytest.raises(ValueError, match="Too many pending"):
        pool.add(second, "second.zip")
    assert not second.exists()
    assert pool.admits("GET", path)
    assert not pool.admits("POST", path)
    assert not pool.admits("GET", path + "/other")
    pool._expire(path.rsplit("/", 1)[1])
    assert not first.exists()
    assert not pool.admits("GET", path)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_interrupted_download_removes_its_temporary_archive(
    tmp_path: Path, failure: type[BaseException]
) -> None:
    path = tmp_path / "large-export.zip"
    path.write_bytes(b"zip" * 100_000)

    async def receive() -> ASGIMessage:
        return {"type": "http.request"}

    async def send(message: ASGIMessage) -> None:
        if message["type"] == "http.response.body":
            raise failure("browser download closed")

    with pytest.raises(failure, match="browser download closed"):
        await _ExportFileResponse(path)(
            {"type": "http", "method": "GET", "headers": []}, receive, send
        )
    assert not path.exists()


def test_full_never_follows_a_workspace_junction(
    export_app: tuple[FastAPI, str, str], tmp_path: Path
) -> None:
    import os
    import subprocess

    app, sid, wid = export_app
    root = Path(app.state.workspaces.get(wid).root_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    link = root / "linked"
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    path = build_archive(app, build_transcript(app, sid), "full")
    try:
        with zipfile.ZipFile(path) as archive:
            assert not any("secret.txt" in name for name in archive.namelist())
            assert any(
                row["reason"] == "link_not_followed"
                for row in json.loads(archive.read("manifest.json"))["omissions"]
            )
    finally:
        path.unlink()
        link.rmdir() if os.name == "nt" else link.unlink()


def test_tool_output_store_never_follows_a_junction(
    export_app: tuple[FastAPI, str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import subprocess

    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "home"))
    app, sid, wid = export_app
    folder = spill_directory(app.state.workspaces.get(wid).root_path, session_id=sid)
    folder.parent.mkdir(parents=True)
    outside = tmp_path / "private-output"
    outside.mkdir()
    (outside / "secret.txt").write_text("private", encoding="utf-8")
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(folder), str(outside)],
            check=True,
            capture_output=True,
        )
    else:
        folder.symlink_to(outside, target_is_directory=True)
    path = build_archive(app, build_transcript(app, sid), "transcript")
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert not manifest["tool_output_files"]
            assert any(
                item["reason"] == "tool_output_link_not_followed" for item in manifest["omissions"]
            )
            assert not any("secret.txt" in name for name in archive.namelist())
    finally:
        path.unlink()
        folder.rmdir() if os.name == "nt" else folder.unlink()


def test_loaded_skill_body_is_durable_and_live_activity_stays_small() -> None:
    event = SemanticEvent(
        event_type="skill.loaded",
        session_id="s",
        trace_id="t",
        payload={"skill_id": "guide", "content": "body" * 10000, "content_sha256": "hash"},
    )
    assert event.to_dict("full")["payload"]["content"] == "body" * 10000
    assert "content" not in event.to_dict("sse")["payload"]
    assert event.to_dict("sse")["payload"]["content_sha256"] == "hash"


def test_damaged_cold_projection_exports_real_file_ledger_and_reports_source(
    export_app: tuple[FastAPI, str, str], tmp_path: Path
) -> None:
    app, sid, _ = export_app

    class DamagedProjection:
        def get(self, key: str, default: Any = None) -> Any:
            raise ClioError("damaged ARC projection")

    app.state.messages = DamagedProjection()
    app.state.message_store = MessageStore(tmp_path / "messages")
    message = Message(
        id="saved_message",
        session_id=sid,
        role="assistant",
        created_at="2026-10-05T00:00:00Z",
        updated_at="2026-10-05T00:00:00Z",
        parts=[Part(id="body", type="text", text="original saved answer")],
    )
    app.state.message_store.append(sid, message)
    blob = build_transcript(app, sid)
    assert blob["messages"][0]["parts"][0]["text"] == "original saved answer"
    assert blob["recording"]["issues"][0]["reason"] == "message_projection_unavailable"
    assert blob["recording"]["issues"][0]["message_source"] == "durable_file"
    app.state.transcript_file = False
    with pytest.raises(ValueError, match="recorded transcript unavailable"):
        build_transcript(app, sid)


def test_only_missing_tool_fields_are_filled_from_historical_messages(
    export_app: tuple[FastAPI, str, str],
) -> None:
    app, sid, _ = export_app
    emit(
        app,
        sid,
        "tool.call.started",
        {"call_id": "call", "tool": "read", "args": {"path": "exact"}},
    )
    app.state.messages[sid] = [
        Message(
            id="m",
            session_id=sid,
            role="assistant",
            created_at="2026-10-05T00:00:00Z",
            updated_at="2026-10-05T00:00:00Z",
            parts=[
                Part(
                    id="result",
                    type="tool_result",
                    call_id="call",
                    name="read",
                    text='{"original": "response"}',
                )
            ],
        )
    ]
    blob = build_transcript(app, sid)
    assert blob["tool_records"][0]["input"] == {"path": "exact"}
    assert blob["tool_records"][0]["output"] == '{"original": "response"}'
    assert blob["tool_records"][0]["source"] == "mixed_history"
    assert blob["recording"]["issues"][0]["call_id"] == "call"


def test_review_summary_uses_real_validator_and_correlates_the_saved_message(
    tmp_path: Path,
) -> None:
    from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
    from clio_agent.gact.app import build_app
    from clio_agent.gact.protocol.constants import A2UI_V091
    from clio_agent.gact.session_export_review import review_summary

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="saved view")
    part = Part(
        id="view",
        type="a2ui",
        a2ui_protocol_version=A2UI_V091,
        a2ui_messages=[
            {
                "version": "v0.9.1",
                "createSurface": {"surfaceId": "saved", "catalogId": workspace_catalog_id()},
            },
            {
                "version": "v0.9.1",
                "updateComponents": {
                    "surfaceId": "saved",
                    "components": [{"id": "root", "component": "Text", "text": "Saved evidence"}],
                },
            },
        ],
        metadata={"recorded_at": "2026-10-05T00:00:00Z"},
    )
    result = review_summary(
        app,
        {
            "session": session.to_wire(),
            "messages": [{"id": "original_message", "parts": [part.model_dump(mode="json")]}],
            "children": [],
        },
    )
    assert result["surfaces"], result
    assert result["surfaces"][0]["id"] == "saved"
    assert result["surfaces"][0]["message_id"] == "original_message"
    assert result["degradations"] == []
    invalid = part.model_dump(mode="json")
    invalid["a2ui_messages"][1]["updateComponents"]["components"][0]["component"] = "NotInstalled"
    result = review_summary(
        app,
        {
            "session": session.to_wire(),
            "messages": [{"id": "original_message", "parts": [invalid]}],
            "children": [],
        },
    )
    assert result["degradations"]
