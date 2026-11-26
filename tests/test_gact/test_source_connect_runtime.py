"""Source attachment approval and source/account status use the real storage service."""

from __future__ import annotations

import asyncio
import sys
import traceback
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp_tasks.client_models import ClientGetTaskResult
from jsonschema import ValidationError
from mcp.types import CallToolResult

from clio_agent.gact.app import build_app
from clio_agent.gact.storage import connect_tool, setup_tool
from clio_agent.gact.storage.models import SourceRecord
from clio_agent.gact.storage.service import StorageService
from clio_agent.gact.task_access import TaskCaller


def _connect(
    app: Any,
    provider: str,
    root: str,
    label: str,
    *,
    configuration: dict[str, Any] | None = None,
    mode: str = "read_only",
) -> dict[str, Any]:
    """Call the declared production tool through its real built-in MCP boundary."""
    tool = app.state.builtin_boundary.bind(connect_tool.build_connected_data_connect_tool())
    return asyncio.run(
        tool.acall(
            provider=provider, root=root, label=label, mode=mode, configuration=configuration
        )
    )


def _collect(
    app: Any, accepted: dict[str, Any], *, session_id: str | None = None, kind: str = "Indexing"
) -> tuple[dict[str, Any], ClientGetTaskResult]:
    """Collect actual native execution using shared waits and confirmed stored output."""
    assert accepted["accepted"] is True and accepted["kind"] == kind
    caller = TaskCaller(
        session_id or connect_tool.context.active_session_id(), app.state.human_principal
    )

    async def collect() -> tuple[dict[str, Any], ClientGetTaskResult]:
        report = await app.state.task_operations.wait(caller, accepted["handle"])
        assert report["unfinished"] == [] and len(report["outcomes"]) == 1
        snapshot = await app.state.task_history.io.run(
            app.state.task_history.task_snapshot, accepted["handle"]
        )
        assert snapshot is not None and snapshot.terminal_at is not None
        assert snapshot.result is not None
        result = await app.state.task_history.io.run(
            app.state.task_history.read_content, snapshot.result
        )
        assert isinstance(result, dict)
        reply = ClientGetTaskResult.model_validate(result)
        assert snapshot.backend is not None and reply.task_id == snapshot.backend.task_id
        return report["outcomes"][0], reply

    loop = app.state.builtin_boundary._loop
    assert loop is not None
    waiter = asyncio.run_coroutine_threadsafe(collect(), loop)
    try:
        return waiter.result(timeout=10)
    except TimeoutError:
        waiter.cancel()  # The application still owns and settles accepted indexing.
        raise


def _body(reply: ClientGetTaskResult) -> dict[str, Any]:
    """Read the protocol's actual structured tool output, preserving failed envelopes."""
    assert reply.result is not None, reply
    output = CallToolResult.model_validate(reply.result)
    assert output.structured_content is not None, output
    return output.structured_content


@pytest.fixture
def bound(tmp_path: Path) -> Iterator[tuple[StorageService, Any, Path]]:
    service = StorageService(tmp_path / "data", tmp_path / "private" / "credentials.json")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = build_app(sessions_path=tmp_path / "sessions.json")
    app.state.connected_storage = service
    configured = app.state.workspaces.update("ws_default", root_path=str(workspace))
    assert configured is not None
    configured.config["granted_write_roots"] = [str(tmp_path / "second")]
    sid = app.state.sessions.create(workspace_id="ws_default", title="owner").id
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    (upstream / "measurements.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    with TestClient(app):
        app.state.pending_permission_gate = lambda *args: "allow"
        from tests.test_gact.task_records import active_turn_fixture, record_assignment_fixture

        record_assignment_fixture(app, sid)
        with active_turn_fixture(app, sid):
            yield service, app, upstream


def test_permission_denial_leaves_no_source(bound: tuple[StorageService, Any, Path]) -> None:
    service, app, upstream = bound
    app.state.pending_permission_gate = lambda *args: "deny"
    with pytest.raises(PermissionError, match="not approved"):
        _connect(app, "local", str(upstream), "Measurements")
    assert service.sources("ws_default") == []


@pytest.mark.parametrize("mode,decision", [("ask", "deny"), ("ask", "allow"), ("bypass", "allow")])
def test_connection_uses_live_permission_gate(
    tmp_path: Path, mode: str, decision: str, request: pytest.FixtureRequest
) -> None:
    """Prompted and bypass connections share the actual audited permission gate."""
    import threading
    import time

    from clio_agent.gact.app import _gact_app_context, _tool_session_context
    from tests.test_gact.task_records import active_turn_fixture, record_assignment_fixture
    from tests.test_gact.test_a2ui_v3 import HEADERS, _session_client

    client, sid, _ = _session_client(tmp_path)
    client.__enter__()
    request.addfinalizer(lambda: client.__exit__(None, None, None))
    app = client.app
    assert isinstance(app, FastAPI)
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    (upstream / "readme.txt").write_text("input", encoding="utf-8")
    response = client.patch(f"/v1/sessions/{sid}", headers=HEADERS, json={"approval_mode": mode})
    assert response.status_code == 200
    record_assignment_fixture(app, sid)
    outcomes: list[object] = []

    def connect() -> None:
        with _gact_app_context(app), _tool_session_context(sid), active_turn_fixture(app, sid):
            token = connect_tool.context.set_session_id(sid)
            try:
                outcomes.append(_connect(app, "local", str(upstream), "Input"))
            except Exception as exc:
                outcomes.append(exc)
            finally:
                connect_tool.context.reset(token)

    worker = threading.Thread(target=connect, daemon=True)
    worker.start()
    try:
        if mode == "ask":
            deadline = time.monotonic() + 5
            published = None
            while time.monotonic() < deadline:
                published = next(
                    (
                        event
                        for event in app.state.bus.session_events_since(sid)
                        if event.type == "permission.requested"
                        and event.payload["tool_call"]["tool_name"] == "connected_data_connect"
                    ),
                    None,
                )
                if published is not None:
                    break
                time.sleep(0.01)
            assert published is not None
            pending = app.state.permissions[published.payload["id"]]
            assert pending["status"] == "pending"
            assert pending["tool_call"]["tool_name"] == "connected_data_connect"
            assert pending["summary"] == "Connect Input"
            replied = client.post(
                f"/v1/permissions/{pending['id']}", headers=HEADERS, json={"action": decision}
            )
            assert replied.status_code == 204
        worker.join(timeout=5)
        assert not worker.is_alive(), "".join(
            traceback.format_stack(sys._current_frames().get(worker.ident or -1))
        )
        session = app.state.sessions.get(sid)
        assert session is not None
        assert len(outcomes) == 1, outcomes
        if decision == "deny":
            records = app.state.connected_storage.sources(session.workspace_id)
            assert isinstance(outcomes[0], PermissionError) and records == []
        else:
            assert isinstance(outcomes[0], dict), outcomes
            terminal, reply = _collect(app, outcomes[0], session_id=sid)
            result = _body(reply)
            assert terminal["status"] == "completed" and result["state"] == "completed"
            records = app.state.connected_storage.sources(session.workspace_id)
            assert len(records) == 1 and result["source_id"] == records[0].source.id
        if mode == "bypass":
            row = next(iter(app.state.permissions.values()))
            assert row["status"] == "auto_approved" and row["reason"] == "approval_mode_bypass"
    finally:
        if worker.is_alive():
            for pending in tuple(app.state.permissions.values()):
                if (
                    pending["status"] == "pending"
                    and pending["tool_call"]["tool_name"] == "connected_data_connect"
                ):
                    client.post(
                        f"/v1/permissions/{pending['id']}",
                        headers=HEADERS,
                        json={"action": "deny"},
                    )
            worker.join(timeout=5)
            assert not worker.is_alive(), "".join(
                traceback.format_stack(sys._current_frames().get(worker.ident or -1))
            )


def test_connection_is_real_and_reused_without_a_second_approval(
    bound: tuple[StorageService, Any, Path],
) -> None:
    service, app, upstream = bound
    requests: list[tuple[Any, ...]] = []

    def allow(*args: Any) -> str:
        requests.append(args)
        return "allow"

    app.state.pending_permission_gate = allow
    first = _connect(app, "local", str(upstream), "Measurements", configuration={})
    presentation = setup_tool.setup_presentation({}, first, None)
    assert presentation["summary"] == "Indexing requested"
    assert presentation["blocks"][0]["result_kind"] == "snapshot"
    terminal, reply = _collect(app, first)
    indexed = _body(reply)
    assert terminal["status"] == "completed" and indexed["state"] == "completed"
    assert indexed["manifest_id"] and indexed["entries"] == 1
    assert indexed["open_reference"] == {
        "source_id": indexed["source_id"],
        "revision": indexed["manifest_id"],
        "linked": True,
    }
    second = _connect(app, "local", str(upstream), "Measurements", configuration={})
    terminal, reply = _collect(app, second)
    linked = _body(reply)
    assert terminal["status"] == "completed"
    assert linked["connection"]["source_id"] == indexed["source_id"]
    assert linked["connection"]["state"] == "linked"
    assert setup_tool.setup_presentation({}, linked, None)["summary"] == "Source linked"
    assert len(requests) == 1 and requests[0][0] == "connected_data_connect"
    opened = setup_tool.connected_data_open(indexed["source_id"])
    assert [row["path"] for row in opened["entries"]] == ["measurements.csv"]
    assert len(service.sources("ws_default")) == 1


def test_failed_refresh_does_not_present_previous_manifest_as_its_result(
    bound: tuple[StorageService, Any, Path],
) -> None:
    """An unavailable real source keeps the old index without claiming a new output."""
    service, app, upstream = bound
    accepted = _connect(app, "local", str(upstream), "Retained measurements")
    terminal, reply = _collect(app, accepted)
    assert terminal["status"] == "completed"
    original = _body(reply)
    source_id, previous = original["source_id"], original["manifest_id"]
    upstream.rename(upstream.with_name(upstream.name + "-unavailable"))
    refresh = app.state.builtin_boundary.bind(connect_tool.build_connected_data_index_tool())
    receipt = asyncio.run(refresh.acall(source_id=source_id))
    terminal, reply = _collect(app, receipt)
    failed = _body(reply)
    assert terminal["status"] == "failed" and failed["state"] == "failed"
    assert failed["manifest_id"] is None and failed["open_reference"] is None
    assert failed["entries"] == 0 and failed["hashes"] == {}
    assert service.get("ws_default", source_id).linked_manifest_id == previous


@pytest.mark.parametrize("change", ["mode", "configuration", "principal", "disconnected", "clio"])
def test_reused_source_requires_new_approval_when_its_scope_changes(
    bound: tuple[StorageService, Any, Path], change: str
) -> None:
    """A retained source grants only its original owner, host and exact access settings."""
    service, app, upstream = bound
    decisions: list[tuple[Any, ...]] = []

    def gate(*args: Any) -> str:
        decisions.append(args)
        return "allow" if len(decisions) == 1 else "deny"

    app.state.pending_permission_gate = gate
    accepted = _connect(app, "local", str(upstream), "Measurements")
    terminal, reply = _collect(app, accepted)
    assert terminal["status"] == "completed"
    source_id = _body(reply)["source_id"]
    record = service.get("ws_default", source_id)
    arguments: dict[str, Any] = {}
    if change == "mode":
        arguments["mode"] = "write_enabled"
    elif change == "configuration":
        arguments["configuration"] = {"github_ref": "another-revision"}
    else:
        if change == "principal":
            record.principal = "another-human"
        elif change == "disconnected":
            record.connected = False
        else:
            record.source = record.source.model_copy(
                update={"owner": record.source.owner.model_copy(update={"clio_id": "other-clio"})}
            )
        service.store.put("source", source_id, record)
    prior_tasks = app.state.task_history.task_handles(
        kind="Indexing", session_id=connect_tool.context.active_session_id()
    )
    with pytest.raises(PermissionError, match="not approved"):
        _connect(app, "local", str(upstream), "Measurements", **arguments)
    assert len(decisions) == 2
    assert (
        app.state.task_history.task_handles(
            kind="Indexing", session_id=connect_tool.context.active_session_id()
        )
        == prior_tasks
    )
    assert len(service.store.list("source", SourceRecord)) == 1


@pytest.mark.parametrize("change", ["disconnected", "configuration"])
def test_revoked_reused_source_cannot_be_recreated_by_an_accepted_worker(
    bound: tuple[StorageService, Any, Path], change: str
) -> None:
    """Revocation between submission and worker entry leaves the existing source untouched."""
    service, app, upstream = bound
    accepted = _connect(app, "local", str(upstream), "Measurements")
    terminal, reply = _collect(app, accepted)
    assert terminal["status"] == "completed"
    source_id = _body(reply)["source_id"]
    record = service.get("ws_default", source_id)
    manifest = record.linked_manifest_id
    if change == "disconnected":
        record.connected = False
    else:
        record.configuration = record.configuration.model_copy(update={"github_ref": "changed"})
    service.store.put("source", source_id, record)
    with pytest.raises(PermissionError, match="changed before execution"):
        connect_tool.prepare_connect(
            app,
            {"provider": "local", "root": str(upstream), "label": "Measurements"},
            human_action=True,
            session_id=connect_tool.context.active_session_id(),
            approved_source_id=source_id,
        )
    retained = service.store.list("source", SourceRecord)
    assert len(retained) == 1 and retained[0].source.id == source_id
    assert retained[0].linked_manifest_id == manifest


def test_status_keeps_workspace_folders_when_remote_sources_are_empty(
    bound: tuple[StorageService, Any, Path],
) -> None:
    _, _, _ = bound
    result = setup_tool.connected_data_status()
    assert result["sources"] == []
    assert [folder["role"] for folder in result["workspace_folders"]] == ["primary", "additional"]
    assert not any(
        block["type"] == "link"
        for block in setup_tool.setup_presentation({}, result, None)["blocks"]
    )


def test_oauth_connection_waits_for_private_sign_in(
    bound: tuple[StorageService, Any, Path],
) -> None:
    service, app, _ = bound
    accepted = _connect(app, "google_drive", "drive-folder", "Dataset")
    terminal, reply = _collect(app, accepted)
    result = _body(reply)
    assert terminal["status"] == "failed" and result["connection"]["state"] == "sign_in_required"
    assert result["error"]["code"] == "source_sign_in_required"
    assert setup_tool.setup_presentation({}, result, None)["summary"] == "Sign-in required"
    assert service.sources("ws_default")[0].linked_manifest_id is None
    status = setup_tool.connected_data_status()
    assert (
        next(row for row in status["accounts"] if row["provider"] == "google_drive")[
            "login_action"
        ]["name"]
        == "data_source/login/google_drive"
    )


def test_source_configuration_cannot_smuggle_a_credential(
    bound: tuple[StorageService, Any, Path],
) -> None:
    service, app, _ = bound
    permission_calls: list[tuple[Any, ...]] = []

    def allow(*args: Any) -> str:
        permission_calls.append(args)
        return "allow"

    app.state.pending_permission_gate = allow
    with pytest.raises(ValidationError, match="token"):
        _connect(
            app, "github", "iowarp/clio-agent", "Repo", configuration={"token": "must-not-accept"}
        )
    assert permission_calls == []
    assert not app.state.task_history.task_handles(
        kind="Indexing", session_id=connect_tool.context.active_session_id()
    )
    assert service.sources("ws_default") == []
