"""Approval enables real file operations; refusal/revocation retain the boundary."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client

from clio_agent import conf
from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.minting import _contained
from clio_agent.gact.folder_access import requested_folders
from clio_agent.gact.permission_gate import _make_permission_gate
from clio_agent.gact.runtime.globals import _gact_app_context, _tool_session_context
from clio_agent.runtime.sandbox_roots import clear_write_root_grants, granted_write_roots
from clio_agent.runtime.workspace_folders import folder_locations
from clio_agent.tools.execution import tool_workspace_context
from clio_agent.tools.file_policy import FileAccessPolicy, FilePolicyError
from clio_agent.tools.servers.fs_server import fs_server


@pytest.fixture(autouse=True)
def clean_grants() -> Any:
    clear_write_root_grants()
    yield
    clear_write_root_grants()


def setup_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, TestClient, Path, Path, str, str]:
    """Create only private workspace and external-folder data."""
    root, external = tmp_path / "Workspace", tmp_path / "Outside folder"
    root.mkdir()
    external.mkdir()
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(root))
    monkeypatch.setitem(conf.store().data["tools"]["file_policy"], "allowed_roots", [str(root)])
    monkeypatch.setattr("clio_agent.gact.permission_timeout.PERMISSION_REQUEST_TIMEOUT_S", 8)
    app = build_app(sessions_path=tmp_path / "sessions.json")
    client = TestClient(app)
    wid = client.post(
        "/v1/workspaces", json={"name": "Access review", "root_path": str(root)}
    ).json()["id"]
    sid = client.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]
    return app, client, root, external, wid, sid


def pending(app: Any, future: Any) -> dict[str, Any]:
    """Wait for the actual gate ledger, surfacing worker failures immediately."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if future.done():
            future.result()
        rows = [row for row in app.state.permissions.values() if row["status"] == "pending"]
        if rows:
            return rows[0]
        threading.Event().wait(0.01)
    raise AssertionError("No folder approval was published")


@pytest.mark.parametrize("action", ["allow_workspace", "deny"])
def test_real_read_waits_for_human_then_runs_or_returns_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    app, client, root, external, wid, sid = setup_workspace(tmp_path, monkeypatch)
    source = external / "notes.txt"
    source.write_text("private research", encoding="utf-8")
    arguments = {
        "filepath": str(source),
        "access_reason": "Read the outside notes selected for this research.",
    }

    def operation() -> Any:
        with _gact_app_context(app), _tool_session_context(sid):
            decision = _make_permission_gate(app)("fs_read_file", arguments)
            if decision != "allow":
                return decision

            async def read() -> Any:
                async with Client(fs_server) as fs:
                    return (await fs.call_tool("read_file", arguments)).structured_content

            return asyncio.run(read())

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(operation)
        row = pending(app, future)
        assert not future.done() and granted_write_roots(str(root)) == ()
        assert row["access"]["folders"] == [str(external.resolve())]
        assert row["access"]["paths"] == [str(source)]
        assert row["access"]["reason"] == arguments["access_reason"]
        assert row["actions"] == ["deny", "allow_workspace"]
        assert (
            client.post(f"/v1/permissions/{row['id']}", json={"action": "allow"}).status_code == 422
        )
        assert row["status"] == "pending"
        assert (
            client.post(f"/v1/permissions/{row['id']}", json={"action": action}).status_code == 204
        )
        result = future.result(timeout=5)
    if action == "deny":
        assert result == "deny" and "user declined" in result.deny_message
        assert granted_write_roots(str(root)) == ()
    else:
        assert result["content"] == "private research"
        assert app.state.workspaces.get(wid).config["granted_write_roots"] == [
            str(external.resolve())
        ]
        assert row["access_results"][0]["granted"]
        assert not app.state.permission_policies  # no blanket tool allow is manufactured


def test_additional_folder_browses_previews_and_revokes_without_deleting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client, root, external, wid, _sid = setup_workspace(tmp_path, monkeypatch)
    source = external / "notes.txt"
    source.write_text("source material", encoding="utf-8")
    client.post(f"/v1/workspaces/{wid}/grants", json={"kind": "fs_root", "pattern": str(external)})
    prefix = next(iter(folder_locations(root)))
    listing = client.get(f"/v1/workspaces/{wid}/files", params={"directory": ""}).json()
    assert any(
        row["path"] == prefix and "Additional folder" in row["display_path"]
        for row in listing["entries"]
    )
    children = client.get(f"/v1/workspaces/{wid}/files", params={"directory": prefix}).json()
    assert children["entries"][0]["path"] == prefix + "/notes.txt"
    assert (
        client.get(f"/v1/workspaces/{wid}/files/read", params={"path": prefix + "/notes.txt"}).text
        == "source material"
    )
    assert (
        client.get(f"/v1/workspaces/{wid}/files/read", params={"path": str(source)}).text
        == "source material"
    )
    with tool_workspace_context(root):
        assert FileAccessPolicy.from_env().validate_read(prefix + "/notes.txt") == source
        assert _contained(source, root)
    assert (
        client.get(
            f"/v1/workspaces/{wid}/files/read", params={"path": prefix + "/../notes.txt"}
        ).status_code
        == 403
    )
    client.delete(
        f"/v1/workspaces/{wid}/grants", params={"kind": "fs_root", "pattern": str(external)}
    )
    assert (
        client.get(f"/v1/workspaces/{wid}/files", params={"directory": prefix}).status_code == 403
    )
    assert (
        client.get(
            f"/v1/workspaces/{wid}/files/read", params={"path": prefix + "/notes.txt"}
        ).status_code
        == 403
    )
    assert source.read_text() == "source material"
    assert not _contained(source, root)
    with tool_workspace_context(root), pytest.raises(FilePolicyError):
        FileAccessPolicy.from_env().validate_read(str(source))


def test_normalized_permission_response_rechecks_policy_and_only_user_can_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    from clio_agent.gact.permission_gate import resolve_permission

    app, client, root, external, wid, sid = setup_workspace(tmp_path, monkeypatch)
    client.__enter__()
    request.addfinalizer(lambda: client.__exit__(None, None, None))
    source = external / "notes.txt"
    source.write_text("private", encoding="utf-8")

    def ask() -> str:
        with _gact_app_context(app), _tool_session_context(sid):
            return _make_permission_gate(app)("fs_read_file", {"filepath": str(source)})

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ask)
        row = pending(app, future)
        wire = client.get(f"/v1/sessions/{sid}/interactions").json()["interactions"][0]
        assert wire["payload"]["access"]["folders"] == [str(external)]
        assert wire["payload"]["execution"]["cwd"] == str(root)
        assert wire["payload"]["execution"]["computer"]
        respond = f"/v1/sessions/{sid}/interactions/permission:{row['id']}/respond"
        assert client.post(respond, json={"action": "allow"}).status_code == 422
        with pytest.raises(PermissionError, match="Only the user"):
            resolve_permission(app, row["id"], "allow_workspace", grantor="reviewer")
        app.state.permission_policies = [
            {
                "kind": "fs_root",
                "scope": "workspace",
                "scope_id": wid,
                "action": "deny",
                "path_pattern": str(external),
            }
        ]
        assert client.post(respond, json={"action": "allow_workspace"}).status_code == 403
        assert row["status"] == "pending" and not granted_write_roots(str(root))
        assert client.post(respond, json={"action": "deny"}).status_code == 200
        assert future.result(timeout=5) == "deny"
    assert not granted_write_roots(str(root))


def test_shell_declares_both_move_paths_and_empty_defaults_are_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, source, destination = (
        tmp_path / "workspace",
        tmp_path / "source",
        tmp_path / "destination",
    )
    for folder in (root, source, destination):
        folder.mkdir()
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(root))
    monkeypatch.setitem(conf.store().data["tools"]["file_policy"], "allowed_roots", [str(root)])
    with tool_workspace_context(root):
        assert requested_folders("shell_bash", {"access_paths": None}) == []
        assert requested_folders(
            "shell_bash",
            {
                "cwd": str(source),
                "access_paths": [str(source / "a.txt"), str(destination / "a.txt")],
            },
        ) == [str(source), str(destination)]


def test_real_write_and_move_continue_after_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise actual file MCP writes and a newly confined native shell child."""
    from clio_agent.tools.servers.shell_server import shell_server

    app, client, root, external, wid, sid = setup_workspace(tmp_path, monkeypatch)
    destination = tmp_path / "Move destination"
    destination.mkdir()
    source, target = external / "notes.txt", destination / "moved.txt"
    source.write_text("before", encoding="utf-8")
    # Keep ordinary action confirmation separate from root grants: the folder
    # request must still ask even when ordinary tool actions use auto-edits/bypass.
    app.state.sessions.update(sid, approval_mode="bypass")

    def run(name: str, args: dict[str, Any], server: Any, method: str) -> Any:
        with _gact_app_context(app), _tool_session_context(sid):
            assert _make_permission_gate(app)(name, args) == "allow"

            async def invoke() -> Any:
                async with Client(server) as tools:
                    return (await tools.call_tool(method, args)).structured_content

            return asyncio.run(invoke())

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            run,
            "fs_write_file",
            {"filepath": str(source), "content": "reviewed"},
            fs_server,
            "write_file",
        )
        row = pending(app, future)
        assert source.read_text() == "before"
        assert (
            client.post(
                f"/v1/permissions/{row['id']}", json={"action": "allow_workspace"}
            ).status_code
            == 204
        )
        written = future.result(timeout=10)
        assert written["ok"] and written["sha256"] == hashlib.sha256(b"reviewed").hexdigest()
        assert source.read_text() == "reviewed"
        if sys.platform == "win32":
            command = f"Move-Item -LiteralPath '{str(source).replace(chr(39), chr(39) * 2)}' -Destination '{str(target).replace(chr(39), chr(39) * 2)}'"
        else:
            import shlex

            command = f"mv -- {shlex.quote(str(source))} {shlex.quote(str(target))}"
        future = pool.submit(
            run,
            "shell_bash",
            {
                "command": command,
                "cwd": str(root),
                "access_paths": [str(source), str(target)],
                "access_reason": "Move the reviewed file to the selected destination.",
                "timeout_s": 10,
            },
            shell_server,
            "bash",
        )
        row = pending(app, future)
        assert row["access"]["folders"] == [str(destination)]
        assert row["access"]["command"] == command
        assert source.exists() and not target.exists()
        assert (
            client.post(
                f"/v1/permissions/{row['id']}", json={"action": "allow_workspace"}
            ).status_code
            == 204
        )
        result = future.result(timeout=15)
        assert result["exit_code"] == 0, result
    assert not source.exists() and target.read_text() == "reviewed"
    assert app.state.workspaces.get(wid).config["granted_write_roots"] == [
        str(external),
        str(destination),
    ]


def test_granted_consumed_input_gets_hash_and_version(tmp_path: Path) -> None:
    from clio_agent.gact.artifacts.transform_edges import detect_used_edges
    from clio_agent.runtime.sandbox_roots import register_write_root_grant
    from tests.test_gact.test_artifacts_s5 import _make_app

    root, external = tmp_path / "workspace", tmp_path / "source"
    root.mkdir()
    external.mkdir()
    source = external / "notes.txt"
    source.write_text("consumed", encoding="utf-8")
    app, session, _ = _make_app(root)
    register_write_root_grant(str(root), str(external))
    scan = detect_used_edges(
        app,
        session.id,
        args={"filepath": str(source)},
        workspace_id="ws1",
        turn_id="",
        trace_id="",
        tool_name="fs_read_file",
        allow_external_inputs=True,
    )
    assert len(scan.edges) == 1
    edge = scan.edges[0]
    assert edge.sha256 == hashlib.sha256(b"consumed").hexdigest()
    assert edge.artifact_id and edge.version == 1
    second = tmp_path / "another source"
    second.mkdir()
    other = second / "notes.txt"
    other.write_text("different file", encoding="utf-8")
    register_write_root_grant(str(root), str(second))
    other_scan = detect_used_edges(
        app,
        session.id,
        args={"filepath": str(other)},
        workspace_id="ws1",
        turn_id="",
        trace_id="",
        tool_name="fs_read_file",
        allow_external_inputs=True,
    )
    assert other_scan.edges[0].artifact_id != edge.artifact_id
    assert other_scan.edges[0].version == 1
    failed = detect_used_edges(
        app,
        session.id,
        args={"filepath": str(source)},
        workspace_id="ws1",
        turn_id="",
        trace_id="",
        tool_name="fs_read_file",
        allow_external_inputs=False,
    )
    assert not failed.edges


def test_long_windows_artifact_paths_remain_visible_and_evictable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real deep paths exercise ingestion, enumeration, stats and verified eviction."""
    from clio_agent.gact.artifacts.cas import CASStore, ingest_identity
    from clio_agent.platform_paths import win_extended_path

    root = tmp_path / ("deep-source-" * 5) / ("nested-folder-" * 5)
    monkeypatch.setenv("CLIO_AGENT_STATE_DIR", str(root / "private-state"))
    os.makedirs(win_extended_path(root), exist_ok=True)
    source = root / "measurements.txt"
    with open(win_extended_path(source), "wb") as handle:
        handle.write(b"long path evidence")
    identity = ingest_identity(source, workspace_root=root)
    store = CASStore(root)
    sha = hashlib.sha256(b"long path evidence").hexdigest()
    assert len(str(store.blob_path(sha))) > 260
    assert identity.evidence.sha256 == sha and store.has_blob(sha)
    entries = list(store.iter_blobs())
    assert len(entries) == 1 and entries[0].sha256 == sha
    assert entries[0].size_bytes == len(b"long path evidence")
    assert store.evict(sha) == len(b"long path evidence")
    assert not store.has_blob(sha) and not list(store.iter_blobs())
