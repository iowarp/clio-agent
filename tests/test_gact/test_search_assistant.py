"""Real search sources, read boundaries, and isolated search-session activation."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from clio_agent import conf
from clio_agent.gact import context
from clio_agent.gact.agents.tool_instrumentation import instrument_tools
from clio_agent.gact.agents.tool_recording import recording_tool
from clio_agent.gact.app import build_app
from clio_agent.gact.routes.workspace_file_read import readable_workspace_file
from clio_agent.gact.runtime.session_recall_search import (
    _recall_search_excerpt,
    _recall_search_response,
)
from clio_agent.gact.search_assistant import (
    _active,
    build_clio_search_read_tool,
    build_clio_search_tool,
    read_search_source,
    search_connected_service,
)
from clio_agent.gact.side_sessions import READ_ONLY_SIDE_PROFILE, is_read_only_side_session
from clio_agent.gact.types import Message, Part
from tests._plugin_fixtures import custom_marketplace, tools_module

pytestmark = pytest.mark.usefixtures("host_agent_executor")

#: A stand-in for the marketplace's CLIO Search agent: one plugin claiming the
#: two search tools.
SEARCH_AGENT_SOURCE = """
from clio_agent.sdk import Agent


class ClioSearch(Agent):
    id = "clio-search"
    version = "1.0.0"
    name = "CLIO Search"
    role = "a read-only search assistant over the user's connected sources"
    plugins = Agent.plugins + ["clio-search"]
"""


def _search_marketplace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return custom_marketplace(
        tmp_path,
        monkeypatch,
        plugins={
            "clio-search": {
                "files": {
                    "ai.iowarp.clio/tools/search.py": tools_module(
                        "clio_search", "clio_search_read"
                    )
                }
            }
        },
        agents={"clio_search.py": SEARCH_AGENT_SOURCE},
    )


def test_literal_search_reads_the_corpus_off_the_request_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.routes import session_recall as memory

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=None)
    workspace, _ = _source(app, tmp_path / "data")
    original = memory._recall_search_response
    reader_threads: list[int] = []

    def observed_search(*args: Any, **kwargs: Any) -> Any:
        reader_threads.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(memory, "_recall_search_response", observed_search)

    @app.get("/__test/thread")
    async def loop_thread() -> dict[str, int]:
        return {"thread": threading.get_ident()}

    with TestClient(app) as client:
        loop = client.get("/__test/thread").json()["thread"]
        result = client.get(
            "/v1/session-recall/search",
            params={
                "query": "share a plant",
                "workspace_id": workspace.id,
                "include_cross_session": True,
            },
        )
        assert result.status_code == 200, result.text
        assert result.json()["hits"]
        assert reader_threads and all(thread != loop for thread in reader_threads)


def _message(session_id: str, text: str, *, message_id: str = "msg_match") -> Message:
    return Message(
        id=message_id,
        session_id=session_id,
        role="assistant",
        created_at="2026-10-13T12:00:00Z",
        updated_at="2026-10-13T12:00:00Z",
        parts=[Part(id="part_match", type="text", text=text)],
    )


def _source(app: FastAPI, root: Path, name: str = "Evidence") -> Any:
    root.mkdir(exist_ok=True)
    workspace = app.state.workspaces.create(name=name, root_path=str(root))
    session = app.state.sessions.create(workspace_id=workspace.id, title=f"{name} decision")
    app.state.messages[session.id] = [
        _message(session.id, "I checked the protocol. Share a plant with the reviewer.")
    ]
    return workspace, session


def test_rendered_phrase_beats_common_words_and_excerpt_reaches_later_passage(
    tmp_path: Path,
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=None)
    workspace, session = _source(app, tmp_path / "data")
    query = "Share a plant with the reviewer"
    text = "A heading and unrelated context. " * 100 + "Share a **plant** with the reviewer."
    app.state.messages[session.id] = [
        _message(session.id, text),
        _message(session.id, "a reviewer mentioned a protocol", message_id="msg_other"),
    ]
    response = _recall_search_response(
        app, query=query, workspace_id=workspace.id, include_cross_session=True
    )
    hit = response.hits[0]
    assert hit.message_id == "msg_match"
    assert hit.metadata["exact_phrase"] is True
    assert "Share a **plant** with the reviewer" in hit.text
    assert len(hit.text) <= 486
    assert "plant" in _recall_search_excerpt(text, ["a", "plant", "reviewer"], max_chars=200)


@pytest.mark.asyncio
async def test_search_crosses_registered_workspaces_and_returns_navigable_sources(
    tmp_path: Path,
) -> None:
    app = await asyncio.to_thread(build_app, sessions_path=tmp_path / "sessions.json", agent=None)
    first, _ = _source(app, tmp_path / "first", "First")
    second, session = _source(app, tmp_path / "second", "Second")
    (tmp_path / "second" / "notes.txt").write_text(
        "Intro\nShare a plant with the reviewer.\n", encoding="utf-8"
    )
    result = await search_connected_service(app, query="share a plant", content=True)
    passage = next(
        hit
        for hit in result["hits"]
        if hit["kind"] == "message" and hit["session_id"] == session.id
    )
    assert passage["href"].startswith(
        f"session://{second.id}/{session.id}#message-msg_match?part=part_match&find="
    )
    file = next(
        hit for hit in result["hits"] if hit["kind"] == "file" and hit["path"] == "notes.txt"
    )
    assert file["line"] == 2
    assert file["href"].startswith(f"workspace-file://{second.id}/notes.txt?line=2&find=")
    scoped = await search_connected_service(app, query="share a plant", workspace_id=first.id)
    assert all(hit["workspace_id"] == first.id for hit in scoped["hits"])
    read = await read_search_source(
        app, workspace_id=second.id, path="notes.txt", start_line=2, max_lines=1
    )
    assert read["text"] == "Share a plant with the reviewer."
    assert read["truncated"] is False
    with pytest.raises(ValueError, match="unavailable"):
        await read_search_source(app, workspace_id=first.id, session_id=session.id)


@pytest.mark.asyncio
async def test_policy_denial_and_partial_content_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = await asyncio.to_thread(build_app, sessions_path=tmp_path / "sessions.json", agent=None)
    workspace, _ = _source(app, tmp_path / "data")
    config = tmp_path / "data" / ".clio" / "config.yaml"
    config.parent.mkdir(exist_ok=True)
    config.write_text("private_key: secret-token\n", encoding="utf-8")
    monkeypatch.setenv("CLIO_MAX_FILE_SIZE_BYTES", str(1024 * 1024))
    (tmp_path / "data" / "denied.txt").write_text(
        "secret-token\n" + "A" * (2 * 1024 * 1024), encoding="utf-8"
    )
    (tmp_path / "data" / "large.txt").write_text("A" * (512 * 1024 + 20), encoding="utf-8")
    (tmp_path / "data" / "image.bin").write_bytes(b"binary\x00payload")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(HTTPException) as denied:
        readable_workspace_file(app, workspace.id, "../outside.txt")
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as credentials:
        readable_workspace_file(app, workspace.id, ".clio/config.yaml")
    assert credentials.value.status_code == 403
    result = await search_connected_service(
        app, query="secret-token", workspace_id=workspace.id, kind="files", content=True
    )
    assert not result["hits"]
    assert any(item.get("unreadable_or_policy_denied_files", 0) for item in result["limitations"])
    assert any(item.get("prefix_only_files", 0) for item in result["limitations"])
    assert any(
        item.get("binary_or_unsupported_encoding_files", 0) for item in result["limitations"]
    )
    binary = await search_connected_service(
        app, query="image.bin", workspace_id=workspace.id, kind="files"
    )
    assert binary["hits"][0]["href"] == f"workspace-file://{workspace.id}/image.bin"
    read = await read_search_source(app, workspace_id=workspace.id, path="large.txt")
    assert read["truncated"] is True


def test_search_tools_require_server_owned_read_only_profile(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=None)
    fake = app.state.sessions.create(
        workspace_id="ws_default", metadata={"search_session": {"ephemeral": True}}
    )
    app_token = context.set_app(app)
    session_token = context.set_session_id(fake.id)
    try:
        with pytest.raises(PermissionError, match="read-only"):
            _active()
        protected = app.state.sessions.create(
            workspace_id="ws_default",
            metadata={"search_session": {"ephemeral": True}},
            approval_profile=READ_ONLY_SIDE_PROFILE,
        )
        protected_token = context.set_session_id(protected.id)
        try:
            assert _active()[1].id == protected.id
        finally:
            context.reset(protected_token)
    finally:
        context.reset(session_token)
        context.reset(app_token)


def test_search_session_runs_the_search_agent_and_inherits_model_without_copying_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _search_marketplace(tmp_path, monkeypatch)
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=None)
    parent = app.state.sessions.create(
        workspace_id="ws_default",
        title="Original",
        model={"provider_id": "codex", "model_id": "gpt-6-luna"},
    )
    app.state.messages[parent.id] = [_message(parent.id, "This stays in the parent")]
    with TestClient(app) as client:
        response = client.post(
            "/v1/search/sessions",
            json={"workspace_id": "ws_default", "parent_session_id": parent.id},
        )
        assert response.status_code == 200, response.text
        opened = app.state.sessions.get(response.json()["id"])
        assert opened is not None and is_read_only_side_session(opened)
        assert opened.model == parent.model
        assert opened.parent_session_id == ""
        assert opened.metadata["search_session"]["ephemeral"]
        assert opened.metadata["agent_ref"] == "clio/clio-search"
        assert opened.agent["id"] == "clio-search"
        pinned = client.get(f"/v1/sessions/{opened.id}/agent").json()["resolution"]
        assert "clio-search" in [row["id"] for row in pinned["plugins"]]
        assert {"clio_search", "clio_search_read"} <= set(pinned["tool_names"])
        assert not app.state.messages.get(opened.id)
        assert app.state.messages[parent.id][0].parts[0].text == "This stays in the parent"
        assert client.delete(f"/v1/sessions/{opened.id}").status_code in {200, 204}
        assert app.state.sessions.get(parent.id) is not None
        monkeypatch.setenv("CLIO_FLEET_SEARCH", '{"provider_id":"codex","model_id":"gpt-5.6-luna"}')
        conf.reload()
        second = client.post(
            "/v1/search/sessions",
            json={"workspace_id": "ws_default", "parent_session_id": parent.id},
        )
        selected = app.state.sessions.get(second.json()["id"])
        assert selected is not None and selected.model == {
            "provider_id": "codex",
            "model_id": "gpt-5.6-luna",
        }
        bad = client.post("/v1/search/sessions", json={"workspace_id": "ws_missing"})
        assert bad.status_code == 404
        workspace, _ = _source(app, tmp_path / "passages")
        (tmp_path / "passages" / "notes.txt").write_text(
            "Intro\nA matching source line.\n", encoding="utf-8"
        )
        passage = client.get(
            f"/v1/workspaces/{workspace.id}/files/passage", params={"path": "notes.txt", "line": 2}
        )
        assert passage.status_code == 200, passage.text
        assert passage.json()["start_line"] == 1
        assert "A matching source line." in passage.json()["text"]
        denied = client.get(
            f"/v1/workspaces/{workspace.id}/files/passage", params={"path": "../outside.txt"}
        )
        assert denied.status_code == 403


def test_search_session_without_the_search_agent_is_a_typed_422(tmp_path: Path) -> None:
    """The hermetic default marketplace ships no ``clio-search`` agent."""

    with TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=None)) as client:
        before = {row["id"] for row in client.get("/v1/sessions").json()["sessions"]}
        response = client.post("/v1/search/sessions", json={"workspace_id": "ws_default"})
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert error["error"] == "agent_unavailable"
        assert error["details"]["agent_ref"] == "clio/clio-search"
        assert error["details"]["reason"] == "agent_not_installed"
        after = {row["id"] for row in client.get("/v1/sessions").json()["sessions"]}
        assert after == before


@pytest.mark.asyncio
async def test_native_tools_read_real_sources_and_global_search_omits_ephemeral_chats(
    tmp_path: Path,
) -> None:
    app = await asyncio.to_thread(build_app, sessions_path=tmp_path / "sessions.json", agent=None)
    workspace, session = _source(app, tmp_path / "data")
    (tmp_path / "data" / "notes.txt").write_text("Evidence from the file", encoding="utf-8")
    side = app.state.sessions.create(
        workspace_id=workspace.id,
        metadata={"search_session": {"ephemeral": True}, "side_session": {"ephemeral": True}},
        approval_profile=READ_ONLY_SIDE_PROFILE,
    )
    app.state.messages[side.id] = [_message(side.id, "Share a plant with the reviewer.")]
    app.state.sessions.create(workspace_id=workspace.id, metadata={"side_session": "legacy marker"})
    hits = _recall_search_response(app, query="share a plant", include_cross_session=True)
    assert side.id not in {hit.session_id for hit in hits.hits}
    app_token, session_token = context.set_app(app), context.set_session_id(side.id)
    try:
        search_tool, read_tool = instrument_tools(
            [
                recording_tool(build_clio_search_tool()),
                recording_tool(build_clio_search_read_tool()),
            ]
        )

        async def read_sources() -> tuple[Any, Any]:
            found = await search_tool.acall(
                query="Evidence", kind="sessions", workspace_id=workspace.id
            )
            read = await read_tool.acall(workspace_id=workspace.id, path="notes.txt")
            return found, read

        # Run the production lifespan so its actual server loop is registered;
        # the guard remains strict without a lifespan, even on worker loops.
        with TestClient(app):
            found, read = await asyncio.to_thread(lambda: asyncio.run(read_sources()))
        assert found["hits"][0]["session_id"] == session.id
        assert read["text"] == "Evidence from the file"
    finally:
        context.reset(session_token)
        context.reset(app_token)
