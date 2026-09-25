"""Focused custody, upload, authorization, and message-reference tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.messaging import _dspy_files_from_parts, _dspy_images_from_parts
from clio_agent.gact.parts import Part
from clio_agent.gact.protocol.v3.message import part_to_v3_block
from clio_agent.gact.resource_custody import (
    ResourceLimitError,
    ResourceMaterialization,
    ResourceStore,
    _safe_name,
    windows_safe_filename,
)
from clio_agent.gact.resource_enrichment import (
    PROCESSING_QUERY_TOOL,
    describe_resource_parts,
)
from clio_agent.gact.resource_processing import (
    ResourceConverterFactory,
    ResourceProcessingRecord,
    ResourceProcessingStore,
    _document_service_payload,
    resource_processing_task_id,
)
from clio_agent.gact.resource_tools import (
    inspect_workspace_resource,
    list_workspace_resources,
    read_workspace_resource_structure,
    read_workspace_resource_text,
    search_workspace_resource,
    wait_for_workspace_resource_processing,
)
from tests.test_gact.test_post_messages import FakeClioAgent

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def test_document_service_markdown_becomes_a_named_clio_derivative(tmp_path: Path) -> None:
    """Adapt the real clio-web-search result shape without inventing content."""

    payload = _document_service_payload(
        {
            "id": "docling_job",
            "status": "complete",
            "result": {
                "markdown": "# HStream\n",
                "document": {
                    "structure": {"texts": [{"text": "HStream"}]},
                    "capabilities": ["markdown", "document_structure"],
                },
            },
        }
    )
    store = ResourceStore(root=tmp_path / "resources", max_resource_bytes=1024)
    record, _replay = store.create_or_resume(
        workspace_id="ws_1",
        name="paper.pdf",
        declared_size=len(b"%PDF-test"),
        claimed_mime="application/pdf",
    )
    record = store.append(record.id, offset=0, data=b"%PDF-test")
    processing = ResourceProcessingStore(store)

    completed = processing.save_result(
        record,
        processing.state(record),
        payload["result"],
    )
    manifest = processing.manifest(record)

    assert completed.derivatives_available is True
    assert manifest is not None
    assert manifest["entries"][0]["name"] == "paper.md"
    derivative_path, _entry = processing.derivative_path(record, "markdown")
    assert derivative_path.read_text(encoding="utf-8") == "# HStream\n"


def test_document_service_marks_progress_as_stage_based() -> None:
    """Do not present fixed Docling milestones as measured completion."""

    payload = _document_service_payload(
        {
            "id": "docling_job",
            "status": "running",
            "progress": 40,
            "stage": "docling",
            "message": "Docling is still processing (15s elapsed)",
        }
    )

    assert payload["progress"] == 40
    assert payload["progress_kind"] == "stage"
    assert payload["stage"] == "docling"


def test_resource_processing_exposes_bounded_converter_activity(tmp_path: Path) -> None:
    """Carry real converter events without exposing an unbounded log response."""

    class _EventfulDocumentProcessor(_CompleteDocumentProcessor):
        async def submit(self, record: object, content_path: Path) -> dict[str, Any]:
            del record, content_path
            return {"id": "eventful_job", "status": "processing"}

        async def status(self, job_id: str) -> dict[str, Any]:
            assert job_id == "eventful_job"
            return {
                "id": job_id,
                "status": "processing",
                "progress": 40,
                "progress_kind": "stage",
                "stage": "docling",
                "events": [
                    {
                        "sequence": sequence,
                        "created_at": 1_788_300_000 + sequence,
                        "level": "warning" if sequence == 104 else "info",
                        "progress": 40,
                        "stage": "docling",
                        "message": f"converter event {sequence}" + ("x" * 2_000),
                    }
                    for sequence in range(1, 105)
                ],
            }

    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=FakeClioAgent(answer="unused"),
    )
    app.state.resource_converter_factory = ResourceConverterFactory([_EventfulDocumentProcessor()])
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(
            client,
            workspace_id,
            name="activity.md",
            content=b"# Activity\n",
            media_type="text/markdown",
        )
        payload = client.get(
            f"/v1/workspaces/{workspace_id}/resources/{resource['id']}/derivatives"
        ).json()

    events = payload["processor"]["events"]
    assert len(events) == 100
    assert events[0]["sequence"] == 5
    assert events[-1]["sequence"] == 104
    assert events[-1]["level"] == "warning"
    assert events[-1]["progress_kind"] == "stage"
    assert events[-1]["created_at"].endswith("+00:00")
    assert len(events[-1]["message"]) == 1_000


class _CompleteDocumentProcessor:
    id = "test-docling"
    priority = 20
    endpoint = "http://processor.test"
    configured = True

    def supports(self, record: Any) -> bool:
        return record.detected_mime == "text/markdown"

    async def submit(self, record: object, content_path: Path) -> dict[str, Any]:
        assert content_path.read_text(encoding="utf-8") == "# Structured\n"
        return {
            "id": "doc_job_1",
            "status": "complete",
            "result": {
                "markdown": "# Structured\n",
                "html": "<h1>Structured</h1>",
                "document": {
                    "structure": {
                        "texts": [{"text": "Structured", "prov": [{"page_no": 1}]}],
                        "tables": [{"data": [["value"]]}],
                    },
                    "capabilities": ["document_structure", "named_derivatives"],
                },
                "derivatives": {
                    "schema": "clio.resource-derivatives.v1",
                    "entries": [
                        {
                            "id": "markdown",
                            "name": "structured.md",
                            "kind": "markdown",
                            "media_type": "text/markdown",
                            "content": "# Structured\n",
                        },
                        {
                            "id": "html",
                            "name": "structured.html",
                            "kind": "html",
                            "media_type": "text/html",
                            "content": "<h1>Structured</h1>",
                        },
                        {
                            "id": "table-1",
                            "name": "structured.table-1.json",
                            "kind": "table",
                            "media_type": "application/json",
                            "collection": "tables",
                            "index": 0,
                        },
                    ],
                },
            },
        }

    async def status(self, job_id: str) -> dict[str, Any]:
        raise AssertionError(f"completed job must not be polled: {job_id}")


def _workspace(client: TestClient, root: Path, name: str = "resources") -> str:
    root.mkdir(parents=True, exist_ok=True)
    response = client.post(
        "/v1/workspaces",
        json={"name": name, "root_path": str(root)},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _upload(
    client: TestClient,
    workspace_id: str,
    *,
    name: str,
    content: bytes,
    media_type: str,
) -> dict[str, object]:
    created = client.post(
        f"/v1/workspaces/{workspace_id}/resources",
        json={"name": name, "size": len(content), "media_type": media_type},
    )
    assert created.status_code == 201, created.text
    resource = created.json()
    if content:
        appended = client.patch(
            resource["upload_url"],
            headers={"Upload-Offset": "0", "Content-Type": "application/offset+octet-stream"},
            content=content,
        )
        assert appended.status_code == 204, appended.text
    response = client.get(f"/v1/workspaces/{workspace_id}/resources/{resource['id']}")
    assert response.status_code == 200, response.text
    return response.json()


def test_resumable_upload_computes_server_identity_and_survives_restart(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    app = build_app(sessions_path=sessions_path, agent=FakeClioAgent(answer="unused"))
    content = b"first line\nsecond line\n"
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        created = client.post(
            f"/v1/workspaces/{workspace_id}/resources",
            json={
                "name": "../notes.md",
                "size": len(content),
                "media_type": "application/octet-stream",
                "client_upload_id": "browser-upload-1",
            },
        ).json()
        first = client.patch(
            created["upload_url"],
            headers={"Upload-Offset": "0"},
            content=content[:7],
        )
        assert first.status_code == 204
        assert first.headers["upload-offset"] == "7"

    restarted = build_app(sessions_path=sessions_path, agent=FakeClioAgent(answer="unused"))
    with TestClient(restarted) as client:
        resumed = client.post(
            f"/v1/workspaces/{workspace_id}/resources",
            json={
                "name": "notes.md",
                "size": len(content),
                "media_type": "application/octet-stream",
                "client_upload_id": "browser-upload-1",
            },
        )
        assert resumed.status_code == 201, resumed.text
        assert resumed.json()["id"] == created["id"]
        assert resumed.json()["received_size"] == 7
        assert resumed.json()["idempotent_replay"] is True
        head = client.head(created["upload_url"])
        assert head.headers["upload-offset"] == "7"
        completed = client.patch(
            created["upload_url"],
            headers={"Upload-Offset": "7"},
            content=content[7:],
        )
        assert completed.status_code == 204
        record = client.get(f"/v1/workspaces/{workspace_id}/resources/{created['id']}").json()
        assert record["name"] == "notes.md"
        assert record["state"] == "ready"
        assert record["detected_mime"] == "text/markdown"
        assert record["detection_source"] == "utf8_and_extension"
        assert record["mime_mismatch"] is True
        assert record["sha256"] == hashlib.sha256(content).hexdigest()
        workspace_copy = Path(str(record["workspace_path"]))
        assert (
            workspace_copy
            == (
                tmp_path / "workspace" / ".clio" / "inputs" / str(record["id"]) / "notes.md"
            ).resolve()
        )
        assert workspace_copy.read_bytes() == content
        stored = restarted.state.resource_store.get(workspace_id, str(record["id"]))
        assert stored is not None
        custody = restarted.state.resource_store.content_path(stored)
        assert workspace_copy != custody
        source_entry = next(
            entry
            for entry in client.get(f"/v1/workspaces/{workspace_id}/files").json()["entries"]
            if entry.get("resource_id") == record["id"]
        )
        assert source_entry["path"] == f".clio/inputs/{record['id']}/notes.md"
        assert source_entry["display_path"] == f"Sources/{record['id']}/notes.md"
        assert source_entry["type"] == "file"
        assert source_entry["internal"] is False
        assert source_entry["source"] == "managed_input"
        assert source_entry["resource_id"] == record["id"]
        assert source_entry["size"] == len(content)
        assert source_entry["media_type"] == "text/markdown"

        # The .clio walk now descends into everything EXCEPT .clio/inputs (owner
        # ruling: show all dot files/folders, but the managed-input subtree is
        # already surfaced above as a friendly Sources/<id>/<name> entry, so it
        # must not also show up a second time under its raw .clio/inputs path).
        all_entries = client.get(f"/v1/workspaces/{workspace_id}/files").json()["entries"]
        matching_paths = [
            entry["path"] for entry in all_entries if entry["path"] == source_entry["path"]
        ]
        assert matching_paths == [source_entry["path"]]
        assert not any(entry["path"] == ".clio/inputs" for entry in all_entries)

        workspace_copy.write_bytes(b"mutable workspace edit\n")
        assert custody.read_bytes() == content
        assert client.get(created["upload_url"]).content == content


def test_upload_identity_rejects_metadata_rebinding(tmp_path: Path) -> None:
    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=FakeClioAgent(answer="unused"),
    )
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        first = client.post(
            f"/v1/workspaces/{workspace_id}/resources",
            json={
                "name": "first.md",
                "size": 5,
                "media_type": "text/markdown",
                "client_upload_id": "stable-upload",
            },
        )
        assert first.status_code == 201, first.text
        conflict = client.post(
            f"/v1/workspaces/{workspace_id}/resources",
            json={
                "name": "different.md",
                "size": 5,
                "media_type": "text/markdown",
                "client_upload_id": "stable-upload",
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["error"] == "resource_upload_identity_conflict"
        assert conflict.json()["error"]["details"]["current"]["id"] == first.json()["id"]


def test_ready_idempotent_replay_starts_newly_available_converter(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    content = b"# Structured\n"
    body = {
        "name": "structured.md",
        "size": len(content),
        "media_type": "text/markdown",
        "client_upload_id": "stable-browser-upload",
    }
    initial = build_app(sessions_path=sessions_path, agent=FakeClioAgent(answer="unused"))
    with TestClient(initial) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        created = client.post(f"/v1/workspaces/{workspace_id}/resources", json=body).json()
        completed = client.patch(
            created["upload_url"], headers={"Upload-Offset": "0"}, content=content
        )
        assert completed.status_code == 204, completed.text
        before = client.get(f"/v1/workspaces/{workspace_id}/resources/{created['id']}").json()
        assert before["processing"]["state"] == "not_started"

    restarted = build_app(sessions_path=sessions_path, agent=FakeClioAgent(answer="unused"))
    restarted.state.resource_converter_factory = ResourceConverterFactory(
        [_CompleteDocumentProcessor()]
    )
    with TestClient(restarted) as client:
        replay = client.post(f"/v1/workspaces/{workspace_id}/resources", json=body)
        assert replay.status_code == 201, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["id"] == created["id"]
        processed = client.get(f"/v1/workspaces/{workspace_id}/resources/{created['id']}").json()
        assert processed["processing"]["state"] == "complete"


def test_upload_offset_limit_preview_search_and_workspace_isolation(tmp_path: Path) -> None:
    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=FakeClioAgent(answer="unused"),
    )
    with TestClient(app) as client:
        first_workspace = _workspace(client, tmp_path / "first", "first")
        second_workspace = _workspace(client, tmp_path / "second", "second")
        created = client.post(
            f"/v1/workspaces/{first_workspace}/resources",
            json={"name": "sample.txt", "size": 5, "media_type": "text/plain"},
        ).json()
        stale = client.patch(
            created["upload_url"], headers={"Upload-Offset": "1"}, content=b"hello"
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["error"] == "upload_conflict"
        assert (
            client.patch(
                created["upload_url"], headers={"Upload-Offset": "0"}, content=b"hello"
            ).status_code
            == 204
        )

        preview = client.get(f"/v1/workspaces/{first_workspace}/resources/{created['id']}/preview")
        assert preview.status_code == 200
        assert preview.content == b"hello"
        search = client.get(
            f"/v1/workspaces/{first_workspace}/resources/{created['id']}/search",
            params={"q": "ELL"},
        ).json()
        assert search["matches"] == [{"line": 1, "text": "hello"}]
        assert (
            client.get(f"/v1/workspaces/{second_workspace}/resources/{created['id']}").status_code
            == 404
        )


def test_binary_resource_has_honest_metadata_only_preview(tmp_path: Path) -> None:
    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=FakeClioAgent(answer="unused"),
    )
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        record = _upload(
            client,
            workspace_id,
            name="array.h5",
            content=b"\x89HDF\r\n\x1a\nopaque scientific bytes",
            media_type="application/x-hdf5",
        )
        preview = client.get(f"/v1/workspaces/{workspace_id}/resources/{record['id']}/preview")
        assert preview.status_code == 415
        assert preview.json()["error"]["error"] == "preview_unavailable"


def test_resource_limit_is_enforced_before_storage(tmp_path: Path) -> None:
    store = ResourceStore(root=tmp_path / "resources", max_resource_bytes=4)
    with pytest.raises(ResourceLimitError, match="deployment limit"):
        store.create_or_resume(workspace_id="ws_1", name="large.txt", declared_size=5)
    assert store.list("ws_1") == []


def test_workspace_deletion_cascades_resource_bytes_and_index(tmp_path: Path) -> None:
    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=FakeClioAgent(answer="unused"),
    )
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        record = _upload(
            client,
            workspace_id,
            name="remove.md",
            content=b"remove me",
            media_type="text/markdown",
        )
        resource_root = app.state.resource_store.root / workspace_id / str(record["id"])
        workspace_copy = Path(str(record["workspace_path"]))
        assert resource_root.exists()
        assert workspace_copy.exists()
        assert client.delete(f"/v1/workspaces/{workspace_id}").status_code == 204
        assert not resource_root.exists()
        assert not workspace_copy.exists()
        assert app.state.resource_store.list(workspace_id) == []


def test_message_resource_ref_is_workspace_scoped_ready_and_server_normalized(
    tmp_path: Path,
) -> None:
    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=FakeClioAgent(answer="received"),
    )
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        other_workspace = _workspace(client, tmp_path / "other", "other")
        sid = client.post(
            "/v1/sessions",
            json={"title": "resource ref", "workspace_id": workspace_id},
        ).json()["id"]
        ready = _upload(
            client,
            workspace_id,
            name="truth.md",
            content=b"authoritative",
            media_type="text/markdown",
        )
        foreign = _upload(
            client,
            other_workspace,
            name="foreign.md",
            content=b"foreign",
            media_type="text/markdown",
        )

        rejected = client.post(
            f"/v1/sessions/{sid}/messages",
            json={
                "parts": [
                    {
                        "type": "resource_ref",
                        "resource_id": foreign["id"],
                        "resource_revision": "1",
                    }
                ]
            },
        )
        assert rejected.status_code == 404
        accepted = client.post(
            f"/v1/sessions/{sid}/messages",
            json={
                "client_message_id": "msg_resource_ref",
                "parts": [
                    {"type": "text", "text": "inspect this"},
                    {
                        "type": "resource_ref",
                        "resource_id": ready["id"],
                        "resource_revision": "1",
                        "name": "spoofed.txt",
                        "media_type": "application/octet-stream",
                    },
                ],
            },
        )
        assert accepted.status_code == 200, accepted.text
        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        user_message = next(row for row in messages if row["id"] == "msg_resource_ref")
        reference = next(part for part in user_message["parts"] if part["type"] == "resource_ref")
        assert reference["name"] == "truth.md"
        assert reference["media_type"] == "text/markdown"
        assert reference["metadata"]["workspace_id"] == workspace_id
        assert reference["metadata"]["resource_sha256"] == ready["sha256"]
        assert reference["metadata"]["delivery"]["representation"] == "bounded_tools"
        deliveries = client.get(f"/v1/workspaces/{workspace_id}/resource-deliveries").json()[
            "records"
        ]
        assert len(deliveries) == 1
        assert deliveries[0]["message_id"] == "msg_resource_ref"
        assert deliveries[0]["resource_id"] == ready["id"]
        assert deliveries[0]["representation"] == "bounded_tools"


def test_text_only_selected_model_rejects_image_resource_before_turn(
    tmp_path: Path,
) -> None:
    """An image never degrades to metadata when the selected model cannot see pixels."""

    agent = FakeClioAgent(answer="must not run")
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=agent)
    app.state.provider_catalog = {
        "providers": [
            {
                "id": "chatgpt",
                "health": "ready",
                "models": [
                    {
                        "model_id": "gpt-5.3-cg-spark",
                        "availability": "available",
                        "modalities": ["text"],
                        "evidence": {
                            "live": True,
                            "generated_at": "2026-09-01T12:00:00+00:00",
                        },
                    }
                ],
            }
        ]
    }
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(
            client,
            workspace_id,
            name="pixel.png",
            content=png,
            media_type="image/png",
        )
        sid = client.post(
            "/v1/sessions",
            json={"title": "text-only image", "workspace_id": workspace_id},
        ).json()["id"]

        response = client.post(
            f"/v1/sessions/{sid}/messages",
            json={
                "client_message_id": "msg_text_only_image",
                "model": {"provider_id": "chatgpt", "model_id": "gpt-5.3-cg-spark"},
                "parts": [
                    {"type": "text", "text": "What is in this image?"},
                    {
                        "type": "resource_ref",
                        "resource_id": resource["id"],
                        "resource_revision": str(resource["revision"]),
                    },
                ],
            },
        )

        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert error["error"] == "unsupported_resource_modality"
        assert error["details"] == {
            "workspace_id": workspace_id,
            "resource_id": resource["id"],
            "media_type": "image/png",
            "provider": "chatgpt",
            "model": "gpt-5.3-cg-spark",
            "representation": "metadata_only",
            "evidence_source": "live_handshake",
            "recovery_actions": [
                "choose_image_capable_model",
                "remove_resource",
                "retry",
            ],
        }
        assert client.get(f"/v1/sessions/{sid}/messages").json()["messages"] == []
        assert (
            client.get(f"/v1/workspaces/{workspace_id}/resource-deliveries").json()["records"] == []
        )
    assert agent.calls == []


def test_structured_processor_preserves_named_derivatives_and_bounded_nodes(
    tmp_path: Path,
) -> None:
    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=FakeClioAgent(answer="unused"),
    )
    app.state.resource_converter_factory = ResourceConverterFactory([_CompleteDocumentProcessor()])
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(
            client,
            workspace_id,
            name="structured.md",
            content=b"# Structured\n",
            media_type="text/markdown",
        )
        base = f"/v1/workspaces/{workspace_id}/resources/{resource['id']}"
        processed = client.post(f"{base}/reprocess")
        assert processed.status_code == 200, processed.text
        assert processed.json()["state"] == "complete"
        assert processed.json()["derivatives_available"] is True

        derivatives = client.get(f"{base}/derivatives").json()
        assert [row["id"] for row in derivatives["derivatives"]] == [
            "markdown",
            "html",
            "table-1",
        ]
        assert "content_path" not in derivatives["derivatives"][0]
        assert derivatives["derivatives"][0]["content_url"].endswith(
            "/derivatives/markdown/content"
        )
        assert client.get(f"{base}/structure").json()["collections"] == {
            "texts": 1,
            "tables": 1,
        }
        node = client.get(f"{base}/structure/tables/0")
        assert node.json()["node"] == {"data": [["value"]]}
        html = client.get(f"{base}/derivatives/html/content")
        assert html.text == "<h1>Structured</h1>"
        assert html.headers["content-security-policy"].startswith("sandbox;")

        listed = list_workspace_resources(app, workspace_id)
        assert listed["resources"][0]["id"] == resource["id"]
        inspected = inspect_workspace_resource(app, workspace_id, str(resource["id"]))
        assert inspected["processing"]["state"] == "complete"
        assert [row["id"] for row in inspected["derivatives"]] == [
            "markdown",
            "html",
            "table-1",
        ]
        searched = search_workspace_resource(
            app,
            workspace_id,
            str(resource["id"]),
            "structured",
            "markdown",
        )
        assert searched["matches"] == [{"line": 1, "text": "# Structured"}]
        outline = read_workspace_resource_structure(app, workspace_id, str(resource["id"]))
        assert outline["collections"] == {"texts": 1, "tables": 1}
        structured_node = read_workspace_resource_structure(
            app, workspace_id, str(resource["id"]), "tables", 0
        )
        assert structured_node["node"] == {"data": [["value"]]}


def test_cancelled_refresh_keeps_completed_derivatives_available(tmp_path: Path) -> None:
    """Cancelling a refresh must not invalidate an earlier successful conversion."""

    class _RefreshableDocumentProcessor(_CompleteDocumentProcessor):
        async def reprocess(self, record: object, content_path: Path) -> dict[str, Any]:
            del record, content_path
            return {"id": "refresh_job", "status": "processing"}

        async def status(self, job_id: str) -> dict[str, Any]:
            assert job_id == "refresh_job"
            return {"id": job_id, "status": "processing", "progress": 25}

        async def cancel(self, job_id: str) -> dict[str, Any]:
            assert job_id == "refresh_job"
            return {"id": job_id, "status": "cancelled"}

    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=FakeClioAgent(answer="unused"),
    )
    app.state.resource_converter_factory = ResourceConverterFactory(
        [_RefreshableDocumentProcessor()]
    )
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(
            client,
            workspace_id,
            name="structured.md",
            content=b"# Structured\n",
            media_type="text/markdown",
        )
        base = f"/v1/workspaces/{workspace_id}/resources/{resource['id']}"
        assert (
            client.get(f"{base}/derivatives").json()["processor"]["derivatives_available"] is True
        )

        refresh = client.post(f"{base}/reprocess")
        assert refresh.status_code == 202, refresh.text
        assert refresh.json()["state"] == "submitted"
        assert refresh.json()["derivatives_available"] is True

        cancelled = client.post(f"{base}/processing/cancel")
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["state"] == "cancelled"
        assert cancelled.json()["derivatives_available"] is True
        assert client.get(f"{base}/structure").status_code == 200
        assert client.get(f"{base}/derivatives/markdown/content").text.strip() == "# Structured"


def test_resource_list_advances_pending_converter_state(tmp_path: Path) -> None:
    class _PendingDocumentProcessor(_CompleteDocumentProcessor):
        async def submit(self, record: object, content_path: Path) -> dict[str, Any]:
            del record, content_path
            return {"id": "pending_job", "status": "processing"}

        async def status(self, job_id: str) -> dict[str, Any]:
            assert job_id == "pending_job"
            return {
                "id": job_id,
                "status": "complete",
                "result": {
                    "document": {"structure": {"texts": [{"text": "ready"}]}},
                    "derivatives": {"entries": []},
                },
            }

    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=FakeClioAgent(answer="unused"),
    )
    app.state.resource_converter_factory = ResourceConverterFactory([_PendingDocumentProcessor()])
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        created = client.post(
            f"/v1/workspaces/{workspace_id}/resources",
            json={"name": "pending.md", "size": 4, "media_type": "text/markdown"},
        ).json()
        appended = client.patch(
            created["upload_url"], headers={"Upload-Offset": "0"}, content=b"text"
        )
        assert appended.status_code == 204

        listed = client.get(f"/v1/workspaces/{workspace_id}/resources").json()["resources"]
        assert listed[0]["processing"]["state"] == "complete"


def test_a_generic_result_without_a_derivative_manifest_is_refused_not_patched_up(
    tmp_path: Path,
) -> None:
    """The generic converter contract still requires an explicit manifest."""

    store = ResourceStore(root=tmp_path / "resources", max_resource_bytes=1024)
    record, _replay = store.create_or_resume(
        workspace_id="ws_1", name="doc.md", declared_size=len(b"# x\n")
    )
    record = store.append(record.id, offset=0, data=b"# x\n")
    processing = ResourceProcessingStore(store)

    with pytest.raises(ValueError, match="derivative manifest"):
        processing.save_result(
            record,
            processing.state(record),
            {"markdown": "# x\n", "document": {"structure": {"texts": []}}},
        )


def test_malformed_completed_converter_result_never_breaks_resource_reads(tmp_path: Path) -> None:
    class _MalformedDocumentProcessor(_CompleteDocumentProcessor):
        async def submit(self, record: object, content_path: Path) -> dict[str, Any]:
            del record, content_path
            return {"id": "malformed_job", "status": "processing"}

        async def status(self, job_id: str) -> dict[str, Any]:
            assert job_id == "malformed_job"
            return {
                "id": job_id,
                "status": "complete",
                "result": {"document": {"structure": {}}},
            }

    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=FakeClioAgent(answer="unused"),
    )
    app.state.resource_converter_factory = ResourceConverterFactory([_MalformedDocumentProcessor()])
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(
            client,
            workspace_id,
            name="structured.md",
            content=b"# Structured\n",
            media_type="text/markdown",
        )

        response = client.get(f"/v1/workspaces/{workspace_id}/resources/{resource['id']}")

        assert response.status_code == 200, response.text
        assert response.json()["processing"]["state"] == "failed"
        failure = response.json()["processing"]["failure"]
        assert failure["code"] == "processor_result_invalid"
        # The reason names WHICH contract term the processor broke, so an
        # operator is not left guessing between "no structure" and "no manifest".
        assert "derivative manifest" in failure["detail"]


def test_active_resource_conversion_remains_pending_until_user_cancels(tmp_path: Path) -> None:
    cancelled_jobs: list[str] = []

    class _CancellableDocumentProcessor(_CompleteDocumentProcessor):
        async def submit(self, record: object, content_path: Path) -> dict[str, Any]:
            del record
            assert content_path.read_text(encoding="utf-8") == "# Structured\n"
            return {"id": "long_job", "status": "processing"}

        async def status(self, job_id: str) -> dict[str, Any]:
            assert job_id == "long_job"
            return {"id": job_id, "status": "processing", "progress": 17}

        async def cancel(self, job_id: str) -> dict[str, Any]:
            cancelled_jobs.append(job_id)
            return {"id": job_id, "status": "cancelled"}

    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=FakeClioAgent(answer="unused"),
    )
    app.state.resource_converter_factory = ResourceConverterFactory(
        [_CancellableDocumentProcessor()]
    )
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(
            client,
            workspace_id,
            name="structured.md",
            content=b"# Structured\n",
            media_type="text/markdown",
        )
        resource_id = str(resource["id"])
        before = client.get(f"/v1/workspaces/{workspace_id}/resources/{resource_id}").json()[
            "processing"
        ]
        assert before["state"] == "processing"
        assert before["progress"] == 17

        cancelled = client.post(
            f"/v1/workspaces/{workspace_id}/resources/{resource_id}/processing/cancel"
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["state"] == "cancelled"
        assert cancelled.json()["cancellation"]["remote_cancelled"] is True
        assert cancelled_jobs == ["long_job"]

        replay = client.post(
            f"/v1/workspaces/{workspace_id}/resources/{resource_id}/processing/cancel"
        )
        assert replay.status_code == 200
        assert replay.json()["state"] == "cancelled"
        assert cancelled_jobs == ["long_job"]


@pytest.mark.parametrize("local_state", ["complete", "not_started"])
def test_resource_reprocess_uses_converter_extension(
    tmp_path: Path,
    local_state: str,
) -> None:
    reprocessed: list[str] = []

    class _ReprocessingDocumentProcessor(_CompleteDocumentProcessor):
        async def reprocess(self, record: object, content_path: Path) -> dict[str, Any]:
            del record
            assert content_path.read_text(encoding="utf-8") == "# Structured\n"
            reprocessed.append("fresh_job")
            return {"id": "fresh_job", "status": "processing"}

        async def status(self, job_id: str) -> dict[str, Any]:
            assert job_id == "fresh_job"
            return {"id": job_id, "status": "processing", "progress": 3}

    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=FakeClioAgent(answer="unused"),
    )
    app.state.resource_converter_factory = ResourceConverterFactory(
        [_ReprocessingDocumentProcessor()]
    )
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(
            client,
            workspace_id,
            name="structured.md",
            content=b"# Structured\n",
            media_type="text/markdown",
        )
        base = f"/v1/workspaces/{workspace_id}/resources/{resource['id']}"
        assert client.get(base).json()["processing"]["state"] == "complete"
        if local_state == "not_started":
            record = app.state.resource_store.get(workspace_id, str(resource["id"]))
            assert record is not None
            app.state.resource_processing_store.save_state(
                record,
                ResourceProcessingRecord(
                    workspace_id=workspace_id,
                    resource_id=record.id,
                    resource_revision=record.revision,
                    source_sha256=record.sha256,
                ),
            )

        response = client.post(f"{base}/reprocess")

        assert response.status_code == 202, response.text
        assert response.json()["job_id"] == "fresh_job"
        assert response.json()["state"] == "submitted"
        assert reprocessed == ["fresh_job"]


def test_converter_factory_uses_magic_mime_priority_and_falls_back(tmp_path: Path) -> None:
    calls: list[str] = []

    class _Converter(_CompleteDocumentProcessor):
        def __init__(self, converter_id: str, priority: int, *, reject: bool) -> None:
            self.id = converter_id
            self.priority = priority
            self.reject = reject

        async def submit(self, record: object, content_path: Path) -> dict[str, Any]:
            del record, content_path
            calls.append(self.id)
            if self.reject:
                raise RuntimeError("unavailable")
            return {"id": "fallback_job", "status": "processing"}

    store = ResourceStore(root=tmp_path / "resources", max_resource_bytes=1024)
    record, _replay = store.create_or_resume(
        workspace_id="ws_1",
        name="notes.md",
        declared_size=len(b"# Notes\n"),
        claimed_mime="application/pdf",
    )
    record = store.append(record.id, offset=0, data=b"# Notes\n")
    assert record.detected_mime == "text/markdown"
    factory = ResourceConverterFactory(
        [
            _Converter("fallback", 20, reject=False),
            _Converter("preferred", 10, reject=True),
        ]
    )

    submission = asyncio.run(factory.submit(record, store.content_path(record)))
    assert calls == ["preferred", "fallback"]
    assert submission.converter.id == "fallback"
    assert submission.payload["id"] == "fallback_job"


def test_native_image_resource_ref_becomes_model_image_input(tmp_path: Path) -> None:
    """A native-planned image resource supplies its original pixels to DSPy."""

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    store = ResourceStore(root=tmp_path / "resources", max_resource_bytes=1024)
    record, _replay = store.create_or_resume(
        workspace_id="ws_vision",
        name="pixel.png",
        declared_size=len(png),
        claimed_mime="image/png",
    )
    record = store.append(record.id, offset=0, data=png)
    part = Part(
        type="resource_ref",
        resource_id=record.id,
        resource_revision=str(record.revision),
        name=record.name,
        media_type=record.detected_mime,
        metadata={"delivery": {"representation": "native"}},
    )
    app = SimpleNamespace(state=SimpleNamespace(resource_store=store))

    images = _dspy_images_from_parts([part], app=app, workspace_id="ws_vision")

    assert len(images) == 1
    assert images[0].url.startswith("data:image/png;base64,")


def test_non_native_image_resource_ref_never_becomes_model_image_input(tmp_path: Path) -> None:
    """Unknown/text-only capability keeps images behind bounded resource tools."""

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    store = ResourceStore(root=tmp_path / "resources", max_resource_bytes=1024)
    record, _replay = store.create_or_resume(
        workspace_id="ws_text_only",
        name="pixel.png",
        declared_size=len(png),
        claimed_mime="image/png",
    )
    record = store.append(record.id, offset=0, data=png)
    part = Part(
        type="resource_ref",
        resource_id=record.id,
        resource_revision=str(record.revision),
        name=record.name,
        media_type=record.detected_mime,
        metadata={"delivery": {"representation": "bounded_tools"}},
    )
    app = SimpleNamespace(state=SimpleNamespace(resource_store=store))

    assert _dspy_images_from_parts([part], app=app, workspace_id="ws_text_only") == []


def test_native_pdf_resource_ref_becomes_model_file_input(tmp_path: Path) -> None:
    """A native-planned PDF supplies its immutable original to DSPy."""

    pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
    store = ResourceStore(root=tmp_path / "resources", max_resource_bytes=1024)
    record, _replay = store.create_or_resume(
        workspace_id="ws_pdf",
        name="paper.pdf",
        declared_size=len(pdf),
        claimed_mime="application/pdf",
    )
    record = store.append(record.id, offset=0, data=pdf)
    part = Part(
        type="resource_ref",
        resource_id=record.id,
        resource_revision=str(record.revision),
        name=record.name,
        media_type=record.detected_mime,
        metadata={"delivery": {"representation": "native"}},
    )
    app = SimpleNamespace(state=SimpleNamespace(resource_store=store))

    files = _dspy_files_from_parts([part], app=app, workspace_id="ws_pdf")

    assert len(files) == 1
    assert files[0].filename == "paper.pdf"
    assert files[0].file_data.startswith("data:application/pdf;base64,")


def test_non_native_pdf_resource_ref_never_becomes_model_file_input(tmp_path: Path) -> None:
    pdf = b"%PDF-1.4\n%%EOF\n"
    store = ResourceStore(root=tmp_path / "resources", max_resource_bytes=1024)
    record, _replay = store.create_or_resume(
        workspace_id="ws_pdf_tools",
        name="paper.pdf",
        declared_size=len(pdf),
        claimed_mime="application/pdf",
    )
    record = store.append(record.id, offset=0, data=pdf)
    part = Part(
        type="resource_ref",
        resource_id=record.id,
        resource_revision=str(record.revision),
        name=record.name,
        media_type=record.detected_mime,
        metadata={"delivery": {"representation": "structured_document"}},
    )
    app = SimpleNamespace(state=SimpleNamespace(resource_store=store))

    assert _dspy_files_from_parts([part], app=app, workspace_id="ws_pdf_tools") == []


def test_resource_reference_projects_as_attachment_block() -> None:
    block = part_to_v3_block(
        {
            "id": "part_resource",
            "type": "resource_ref",
            "resource_id": "res_1",
            "resource_revision": "2",
            "name": "paper.pdf",
            "media_type": "application/pdf",
            "metadata": {"workspace_id": "ws_1"},
        }
    )
    assert block == {
        "id": "part_resource",
        "type": "resource",
        "resource_id": "res_1",
        "resource_revision": "2",
        "workspace_id": "ws_1",
        "name": "paper.pdf",
        "media_type": "application/pdf",
    }


def test_resource_reference_projects_native_delivery_provenance() -> None:
    block = part_to_v3_block(
        {
            "id": "part_image",
            "type": "resource_ref",
            "resource_id": "res_image",
            "resource_revision": "1",
            "name": "diagram.png",
            "media_type": "image/png",
            "metadata": {
                "workspace_id": "ws_vision",
                "delivery": {
                    "representation": "native",
                    "evidence_source": "live_handshake",
                    "reason": "selected model accepts image input",
                },
            },
        }
    )

    assert block["delivery"] == {
        "representation": "native",
        "evidence_source": "live_handshake",
        "reason": "selected model accepts image input",
    }


def test_resource_context_is_private_from_transcript_and_points_agent_to_tools(
    tmp_path: Path,
) -> None:
    from .conftest import complete_turn

    agent = FakeClioAgent(answer="inspected")
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=agent)
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(
            client,
            workspace_id,
            name="notes.md",
            content=b"private attachment content\n",
            media_type="text/markdown",
        )
        sid = client.post(
            "/v1/sessions",
            json={"title": "resource context", "workspace_id": workspace_id},
        ).json()["id"]
        complete_turn(
            client,
            sid,
            "Summarize the attachment",
            json_override={
                "client_message_id": "resource_private_context",
                "parts": [
                    {"type": "text", "text": "Summarize the attachment"},
                    {
                        "type": "resource_ref",
                        "resource_id": resource["id"],
                        "resource_revision": str(resource["revision"]),
                    },
                ],
            },
        )

        prompt, called_sid = agent.calls[0]
        assert called_sid == sid
        assert "Workspace attachments (private runtime context)" in prompt
        assert "notes.md" in prompt
        assert "No structured converter was selected" in prompt
        assert "workspace_resource_read" in prompt
        stored = app.state.resource_store.get(workspace_id, str(resource["id"]))
        assert stored is not None
        assert str(app.state.resource_store.content_path(stored)) not in prompt
        assert str(resource["workspace_path"]) in prompt
        assert "Filesystem tools may read or transform that copy" in prompt

        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        user = next(row for row in messages if row["id"] == "resource_private_context")
        visible_text = "\n".join(
            str(part.get("text") or "") for part in user["parts"] if part["type"] == "text"
        )
        assert visible_text == "Summarize the attachment"
        assert "private runtime context" not in visible_text
        assert "private attachment content" not in visible_text


def test_ready_resource_copies_to_another_agent_owned_workspace(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent(answer="unused"))
    with TestClient(app) as client:
        source_workspace = _workspace(client, tmp_path / "source", "source")
        destination_workspace = _workspace(client, tmp_path / "destination", "destination")
        source = _upload(
            client,
            source_workspace,
            name="paper.pdf",
            content=b"%PDF-1.4\nsource",
            media_type="application/pdf",
        )

        response = client.post(
            f"/v1/workspaces/{source_workspace}/resources/{source['id']}/copy",
            json={"destination_workspace_id": destination_workspace},
        )

        assert response.status_code == 201, response.text
        copied = response.json()
        assert copied["id"] != source["id"]
        assert copied["workspace_id"] == destination_workspace
        assert copied["sha256"] == source["sha256"]
        copied_path = Path(str(copied["workspace_path"]))
        assert copied_path.is_relative_to(tmp_path / "destination")
        assert copied_path.read_bytes() == b"%PDF-1.4\nsource"
        assert copied_path != Path(str(source["workspace_path"]))


def test_resource_context_exposes_durable_local_conversion_task_before_remote_job(
    tmp_path: Path,
) -> None:
    from .conftest import complete_turn

    agent = FakeClioAgent(answer="inspected")
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=agent)
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(
            client,
            workspace_id,
            name="paper.pdf",
            content=b"%PDF-1.4\n",
            media_type="application/pdf",
        )
        record = app.state.resource_store.get(workspace_id, str(resource["id"]))
        assert record is not None
        app.state.resource_processing_store.save_state(
            record,
            ResourceProcessingRecord(
                workspace_id=workspace_id,
                resource_id=record.id,
                resource_revision=record.revision,
                source_sha256=record.sha256,
                processor="test-docling",
                processor_url="http://processor.test",
                state="submitted",
            ),
        )
        sid = client.post(
            "/v1/sessions",
            json={"title": "queued conversion", "workspace_id": workspace_id},
        ).json()["id"]
        complete_turn(
            client,
            sid,
            "Read the PDF",
            json_override={
                "client_message_id": "queued_conversion_context",
                "parts": [
                    {"type": "text", "text": "Read the PDF"},
                    {
                        "type": "resource_ref",
                        "resource_id": record.id,
                        "resource_revision": str(record.revision),
                    },
                ],
            },
        )

        prompt, called_sid = agent.calls[0]
        assert called_sid == sid
        assert f"resource-processing:{record.id}:v{record.revision}" in prompt
        assert "wait once with workspace_resource_wait" in prompt
        assert "Do not repeatedly poll inspection" in prompt
        assert "No structured converter was selected" not in prompt


def test_native_resource_context_states_the_input_and_keeps_conversion_grounding(
    tmp_path: Path,
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent(answer="unused"))
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(
            client,
            workspace_id,
            name="paper.pdf",
            content=b"%PDF-1.4\n",
            media_type="application/pdf",
        )
        record = app.state.resource_store.get(workspace_id, str(resource["id"]))
        assert record is not None
        app.state.resource_processing_store.save_state(
            record,
            ResourceProcessingRecord(
                workspace_id=workspace_id,
                resource_id=record.id,
                resource_revision=record.revision,
                source_sha256=record.sha256,
                processor="test-docling",
                processor_url="http://processor.test",
                state="processing",
                job_id="remote-processing-job",
            ),
        )
        sid = client.post(
            "/v1/sessions",
            json={"title": "native while converting", "workspace_id": workspace_id},
        ).json()["id"]

        blocks = describe_resource_parts(
            app,
            sid,
            [
                Part(
                    type="resource_ref",
                    resource_id=record.id,
                    resource_revision=str(record.revision),
                    name=record.name,
                    metadata={"delivery": {"representation": "native"}},
                )
            ],
        )

        assert len(blocks) == 1
        # The model is told what its INPUT contains -- state-meaning grounding.
        assert "included directly in this model input" in blocks[0]
        # ...and still learns the conversion state, which the prompt handcuff
        # ("use it now", "Do not inspect or wait for conversion") deleted along
        # with every derivative sentence. A model holding the original may still
        # legitimately want the structured conversion; forbidding it a tool is
        # not this block's job.
        assert "Structured conversion is still" in blocks[0]
        assert PROCESSING_QUERY_TOOL in blocks[0]
        # No behavioural prohibition is injected.
        assert "Do not inspect" not in blocks[0]
        assert "use it now" not in blocks[0]


def test_resource_conversion_wait_uses_one_stable_local_task_to_completion(
    tmp_path: Path,
) -> None:
    class _DelayedDocumentProcessor(_CompleteDocumentProcessor):
        async def submit(self, record: object, content_path: Path) -> dict[str, Any]:
            del record
            assert content_path.read_text(encoding="utf-8") == "# Structured\n"
            return {"id": "remote_doc_job", "status": "processing"}

        async def status(self, job_id: str) -> dict[str, Any]:
            assert job_id == "remote_doc_job"
            return {
                "id": job_id,
                "status": "complete",
                "result": {
                    "document": {"structure": {"texts": [{"text": "Structured"}]}},
                    "derivatives": {
                        "schema": "clio.resource-derivatives.v1",
                        "entries": [
                            {
                                "id": "markdown",
                                "name": "structured.md",
                                "kind": "markdown",
                                "media_type": "text/markdown",
                                "content": "# Structured\n",
                            }
                        ],
                    },
                },
            }

    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=FakeClioAgent(answer="unused"),
    )
    app.state.resource_converter_factory = ResourceConverterFactory([_DelayedDocumentProcessor()])
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        created = client.post(
            f"/v1/workspaces/{workspace_id}/resources",
            json={
                "name": "structured.md",
                "size": len(b"# Structured\n"),
                "media_type": "text/markdown",
            },
        ).json()
        appended = client.patch(
            created["upload_url"],
            headers={"Upload-Offset": "0"},
            content=b"# Structured\n",
        )
        assert appended.status_code == 204
        record = app.state.resource_store.get(workspace_id, str(created["id"]))
        assert record is not None
        task_id = resource_processing_task_id(record)
        submitted = app.state.resource_processing_store.state(record)
        assert submitted.job_id == "remote_doc_job"
        assert task_id != submitted.job_id

        waited = asyncio.run(
            wait_for_workspace_resource_processing(app, workspace_id, task_id, 1.0)
        )

        assert waited["task_id"] == task_id
        assert waited["terminal"] is True
        assert waited["timed_out"] is False
        assert waited["processing"]["state"] == "complete"
        assert waited["processing"]["job_id"] == "remote_doc_job"
        assert "events" not in waited["processing"]


def test_resource_conversion_wait_times_out_without_cancelling_work(tmp_path: Path) -> None:
    class _LongDocumentProcessor(_CompleteDocumentProcessor):
        async def submit(self, record: object, content_path: Path) -> dict[str, Any]:
            del record, content_path
            return {"id": "remote_long_job", "status": "processing"}

        async def status(self, job_id: str) -> dict[str, Any]:
            return {
                "id": job_id,
                "status": "processing",
                "progress": 42,
                "progress_kind": "stage",
                "stage": "docling",
                "message": "Docling is processing the document",
            }

    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=FakeClioAgent(answer="unused"),
    )
    app.state.resource_converter_factory = ResourceConverterFactory([_LongDocumentProcessor()])
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        created = client.post(
            f"/v1/workspaces/{workspace_id}/resources",
            json={
                "name": "structured.md",
                "size": len(b"# Structured\n"),
                "media_type": "text/markdown",
            },
        ).json()
        assert (
            client.patch(
                created["upload_url"],
                headers={"Upload-Offset": "0"},
                content=b"# Structured\n",
            ).status_code
            == 204
        )
        record = app.state.resource_store.get(workspace_id, str(created["id"]))
        assert record is not None

        waited = asyncio.run(
            wait_for_workspace_resource_processing(
                app, workspace_id, resource_processing_task_id(record), 0.01
            )
        )

        assert waited["terminal"] is False
        assert waited["timed_out"] is True
        assert waited["processing"]["state"] == "processing"
        assert waited["processing"]["progress"] == 42
        assert waited["processing"]["progress_kind"] == "stage"
        assert waited["processing"]["stage"] == "docling"
        assert waited["processing"]["message"] == "Docling is processing the document"
        assert "events" not in waited["processing"]
        assert app.state.resource_processing_store.state(record).state == "processing"


def test_resource_processing_can_be_cancelled_before_remote_job_assignment(
    tmp_path: Path,
) -> None:
    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=FakeClioAgent(answer="unused"),
    )
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(
            client,
            workspace_id,
            name="paper.pdf",
            content=b"%PDF-1.4\n",
            media_type="application/pdf",
        )
        record = app.state.resource_store.get(workspace_id, str(resource["id"]))
        assert record is not None
        app.state.resource_processing_store.save_state(
            record,
            ResourceProcessingRecord(
                workspace_id=workspace_id,
                resource_id=record.id,
                resource_revision=record.revision,
                source_sha256=record.sha256,
                processor="test-docling",
                processor_url="http://processor.test",
                state="submitted",
            ),
        )

        response = client.post(
            f"/v1/workspaces/{workspace_id}/resources/{record.id}/processing/cancel"
        )

        assert response.status_code == 200, response.text
        cancelled = response.json()
        assert cancelled["state"] == "cancelled"
        assert cancelled["job_id"] == ""
        assert cancelled["cancellation"]["remote_cancelled"] is False


def test_bounded_resource_read_returns_original_text_without_custody_path(
    tmp_path: Path,
) -> None:
    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=FakeClioAgent(answer="unused"),
    )
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(
            client,
            workspace_id,
            name="answer.md",
            content=b"The answer is CALDERA-71.\n",
            media_type="text/markdown",
        )

        result = read_workspace_resource_text(app, workspace_id, str(resource["id"]))

        assert result["content"] == "The answer is CALDERA-71.\n"
        assert result["representation"] == "original"
        assert result["truncated"] is False
        assert "path" not in result


# ---------------------------------------------------------------------------
# S2 hardening: materialization moves off GET and is unified behind one
# never-raising, state-updating, retrying owner (materialize_once); names
# are SANITIZED, never rejected. gact-tui root cause B's secondary risk was
# that GET /resources re-materialized (copied bytes for) every resource on
# every read and raised on the first failure, so one bad resource broke the
# whole workspace's resource list.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "notes.md",
        "data (1).csv",
        "report_final.v2.pdf",
        "no-dots",
        "console.log",
        "Q3: notes?.md",
        "bad<name.txt",
        "bad>name.txt",
        'bad"name.txt',
        "bad|name.txt",
        "bad?name.txt",
        "bad*name.txt",
        "a:b.txt",
        "C:",
        "C:evil.txt",
        "notes.",
        "CON",
        "con.txt",
        "NUL",
        "com1.log",
        "LPT9",
    ],
)
def test_safe_name_preserves_names_only_windows_forbids(name: str) -> None:
    """The display name is never rejected, or altered, for being Windows-unsafe.

    _safe_name only rejects a name that cannot identify any single file at
    all (empty, ".", "..", or containing control characters — see the
    still-rejected cases below) or that still carries a path component after
    the last "/" is taken. Everything else, including every character or
    shape Windows forbids in a real filename, passes through UNCHANGED as
    the display name — "Q3: notes?.md" is an ordinary thing to name an
    attachment and must not 400. windows_safe_filename (below) is what
    sanitizes a name for the filesystem, not this function.
    """

    assert _safe_name(name) == name


@pytest.mark.parametrize("invalid_name", ["", ".", "..", "\x07bell.txt", "notes\x00.md"])
def test_safe_name_still_rejects_names_that_identify_no_file(invalid_name: str) -> None:
    """The basic protections _safe_name always had are untouched by S2.

    A name that cannot identify one real file at all, or that carries an
    unprintable control character, is still refused outright — sanitizing
    those into something plausible would be inventing a name the user never
    gave, which is a different failure mode than "Windows dislikes this
    character".
    """

    with pytest.raises(ValueError):
        _safe_name(invalid_name)


def test_safe_name_normalizes_incidental_surrounding_whitespace() -> None:
    """A bare leading/trailing space is trimmed, not preserved.

    This is a pre-existing, format-only normalization (unrelated to the
    Windows-character sanitizing question): a name's outer whitespace was
    always trimmed before path-splitting, so "notes. " becomes "notes."
    (only the trailing SPACE is whitespace; the dot is untouched here).
    """

    assert _safe_name("notes ") == "notes"
    assert _safe_name(" notes.md") == "notes.md"
    assert _safe_name("notes. ") == "notes."


@pytest.mark.parametrize(
    ("unsafe_name", "expected"),
    [
        ("bad<name.txt", "bad_name.txt"),
        ("bad>name.txt", "bad_name.txt"),
        ('bad"name.txt', "bad_name.txt"),
        ("bad|name.txt", "bad_name.txt"),
        ("bad?name.txt", "bad_name.txt"),
        ("bad*name.txt", "bad_name.txt"),
        ("a:b.txt", "a_b.txt"),
        ("C:", "C_"),
        ("C:evil.txt", "C_evil.txt"),
        ("notes.", "notes"),
        ("notes. ", "notes"),
        ("notes ", "notes"),
        ("CON", "_CON"),
        ("con.txt", "_con.txt"),
        ("NUL", "_NUL"),
        ("com1.log", "_com1.log"),
        ("LPT9", "_LPT9"),
        ("Q3: notes?.md", "Q3_ notes_.md"),
        ("nested/evil.txt", "nested_evil.txt"),
        ("nested\\evil.txt", "nested_evil.txt"),
        ("notes.md", "notes.md"),
        ("...", "_"),
    ],
)
def test_windows_safe_filename_sanitizes_rather_than_rejects(
    unsafe_name: str, expected: str
) -> None:
    assert windows_safe_filename(unsafe_name) == expected


def test_create_resource_accepts_a_windows_unsafe_display_name(tmp_path: Path) -> None:
    """No 400 for a name Windows would refuse as a literal filename.

    The display name is preserved verbatim; only the on-disk working copy
    gets a sanitized filename.
    """

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent(answer="unused"))
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")

        resource = _upload(
            client,
            workspace_id,
            name="Q3: notes?.md",
            content=b"hello",
            media_type="text/markdown",
        )

        assert resource["name"] == "Q3: notes?.md"
        assert resource["materialization"]["state"] == "ready"
        workspace_copy = Path(str(resource["workspace_path"]))
        assert workspace_copy.name == "Q3_ notes_.md"
        assert workspace_copy.read_bytes() == b"hello"


def test_a_resource_whose_materialization_fails_does_not_break_get_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One resource's materialization failure must never 409 the whole list.

    Regression for gact-tui S2 (root cause B, secondary risk): GET
    /workspaces/{id}/resources re-materialized every resource on every read
    and raised 409 on the first failure, so one failing resource made the
    entire list unreadable. The failure is now recorded as a typed
    ``materialization`` state on that one resource, and every other resource
    still lists and reads fine. Names are sanitized rather than rejected now
    (windows_safe_filename), so the fault is injected directly rather than
    relying on a name-shaped failure.
    """

    import clio_agent.gact.resource_materialization as materialization_module

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent(answer="unused"))
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")

        good = _upload(
            client, workspace_id, name="notes.md", content=b"hello", media_type="text/markdown"
        )
        assert good["materialization"]["state"] == "ready"
        assert good["workspace_path"]

        created = client.post(
            f"/v1/workspaces/{workspace_id}/resources",
            json={"name": "cursed.md", "size": 4, "media_type": "text/markdown"},
        ).json()
        original_materialize = materialization_module.materialize_resource_for_app

        def _fail_for_cursed(app: Any, record: Any) -> Any:
            if record.id == created["id"]:
                raise OSError("simulated disk failure")
            return original_materialize(app, record)

        monkeypatch.setattr(
            materialization_module, "materialize_resource_for_app", _fail_for_cursed
        )
        appended = client.patch(
            created["upload_url"], headers={"Upload-Offset": "0"}, content=b"evil"
        )
        assert appended.status_code == 204
        monkeypatch.undo()

        listed = client.get(f"/v1/workspaces/{workspace_id}/resources")
        assert listed.status_code == 200, listed.text
        rows = {row["id"]: row for row in listed.json()["resources"]}
        assert rows[good["id"]]["materialization"]["state"] == "ready"
        assert rows[created["id"]]["materialization"]["state"] == "failed"
        assert "simulated disk failure" in rows[created["id"]]["materialization"]["reason"]
        # The bytes are still in custody and the resource is still usable --
        # only its workspace-tree mirror copy failed.
        assert rows[created["id"]]["state"] == "ready"

        single = client.get(f"/v1/workspaces/{workspace_id}/resources/{created['id']}")
        assert single.status_code == 200, single.text
        assert single.json()["materialization"]["state"] == "failed"


def test_get_resources_never_materializes_or_copies_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET (list and single) must never re-run materialization or copy bytes.

    Materialization is now a create-complete / final-PATCH / ready-copy /
    agent-ready-touch-point-only operation. A GET that still triggered it
    would duplicate the filesystem copy on every read and reintroduce the
    whole-list 409 this slice removes.
    """

    import clio_agent.gact.resource_materialization as materialization_module

    calls: list[str] = []
    original = materialization_module.materialize_resource_for_app

    def _spy(app: Any, record: Any) -> Any:
        calls.append(record.id)
        return original(app, record)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent(answer="unused"))
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(
            client, workspace_id, name="notes.md", content=b"hello", media_type="text/markdown"
        )
        assert resource["materialization"]["state"] == "ready"

        monkeypatch.setattr(materialization_module, "materialize_resource_for_app", _spy)
        calls.clear()

        for _ in range(3):
            listed = client.get(f"/v1/workspaces/{workspace_id}/resources")
            assert listed.status_code == 200, listed.text
            single = client.get(f"/v1/workspaces/{workspace_id}/resources/{resource['id']}")
            assert single.status_code == 200, single.text

        assert calls == []


def test_failed_materialization_is_retried_at_the_next_agent_touch_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed materialization gets a fresh attempt at the next ready-touch point.

    GET list/get-one never retry (moving materialization off them is the
    whole point of this slice), but the agent's own resource-listing tool is
    a ready-touch point and DOES retry, via the same never-raising
    materialize_once.
    """

    import clio_agent.gact.resource_materialization as materialization_module

    def _always_fail(app: Any, record: Any) -> Any:
        raise OSError("disk unavailable")

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent(answer="unused"))
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        created = client.post(
            f"/v1/workspaces/{workspace_id}/resources",
            json={"name": "flaky.md", "size": 4, "media_type": "text/markdown"},
        ).json()

        original_materialize = materialization_module.materialize_resource_for_app
        monkeypatch.setattr(materialization_module, "materialize_resource_for_app", _always_fail)
        appended = client.patch(
            created["upload_url"], headers={"Upload-Offset": "0"}, content=b"text"
        )
        assert appended.status_code == 204

        via_get = client.get(f"/v1/workspaces/{workspace_id}/resources/{created['id']}").json()
        assert via_get["materialization"]["state"] == "failed"

        # GET alone must never retry, even while the fault would otherwise clear.
        monkeypatch.setattr(
            materialization_module, "materialize_resource_for_app", original_materialize
        )
        via_get_again = client.get(
            f"/v1/workspaces/{workspace_id}/resources/{created['id']}"
        ).json()
        assert via_get_again["materialization"]["state"] == "failed"

        # The agent's own listing IS a ready-touch point.
        listed = list_workspace_resources(app, workspace_id)
        healed = next(row for row in listed["resources"] if row["id"] == created["id"])
        assert healed["materialization"]["state"] == "ready"
        assert healed["workspace_path"]


def test_legacy_pending_resource_is_materialized_at_the_next_agent_touch_point(
    tmp_path: Path,
) -> None:
    """A resource whose stored index predates the `materialization` field
    defaults to "pending" forever unless something retries it. GET never
    does (S2); the agent's own resource listing is a ready-touch point that
    does, and heals it.
    """

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent(answer="unused"))
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(
            client, workspace_id, name="legacy.md", content=b"hello", media_type="text/markdown"
        )
        assert resource["materialization"]["state"] == "ready"

        # Simulate a legacy record: its workspace-tree copy is gone and its
        # materialization was never tracked (defaults to "pending", exactly
        # like a record loaded from an index written before this field
        # existed).
        Path(resource["workspace_path"]).unlink()
        app.state.resource_store.set_materialization(
            resource["id"], ResourceMaterialization(state="pending")
        )

        via_get = client.get(f"/v1/workspaces/{workspace_id}/resources/{resource['id']}").json()
        assert via_get["materialization"]["state"] == "pending"
        assert not Path(resource["workspace_path"]).exists()

        listed = list_workspace_resources(app, workspace_id)
        healed = next(row for row in listed["resources"] if row["id"] == resource["id"])
        assert healed["materialization"]["state"] == "ready"
        assert Path(healed["workspace_path"]).read_bytes() == b"hello"


def test_inspect_workspace_resource_retries_materialization(tmp_path: Path) -> None:
    """inspect_workspace_resource is also a ready-touch point."""

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent(answer="unused"))
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(
            client, workspace_id, name="legacy.md", content=b"hello", media_type="text/markdown"
        )
        Path(resource["workspace_path"]).unlink()
        app.state.resource_store.set_materialization(
            resource["id"], ResourceMaterialization(state="failed", reason="disk unavailable")
        )

        inspected = inspect_workspace_resource(app, workspace_id, str(resource["id"]))

        assert inspected["resource"]["materialization"]["state"] == "ready"
        assert Path(inspected["resource"]["workspace_path"]).read_bytes() == b"hello"


def test_turn_enrichment_records_and_retries_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-turn attachment enrichment is a ready-touch point too.

    describe_resource_parts used to call the underlying copy directly and
    only ever reported a failure within that one turn's prompt -- it never
    updated the resource's own state, so nothing else could learn about (or
    retry) the failure. Routing through materialize_once means the failure
    is recorded on the resource, and a later touch point (including a
    subsequent turn) can heal it.
    """

    import clio_agent.gact.resource_materialization as materialization_module

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent(answer="unused"))
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        created = client.post(
            f"/v1/workspaces/{workspace_id}/resources",
            json={"name": "flaky.md", "size": 4, "media_type": "text/markdown"},
        ).json()

        original_materialize = materialization_module.materialize_resource_for_app

        def _always_fail(app: Any, record: Any) -> Any:
            raise OSError("disk unavailable")

        monkeypatch.setattr(materialization_module, "materialize_resource_for_app", _always_fail)
        appended = client.patch(
            created["upload_url"], headers={"Upload-Offset": "0"}, content=b"text"
        )
        assert appended.status_code == 204

        sid = client.post(
            "/v1/sessions", json={"title": "flaky", "workspace_id": workspace_id}
        ).json()["id"]
        part = Part(
            type="resource_ref",
            resource_id=created["id"],
            resource_revision=str(created["revision"]),
            name=created["name"],
        )

        blocks = describe_resource_parts(app, sid, [part])
        assert "could not be prepared" in blocks[0]
        assert "disk unavailable" in blocks[0]
        stored = app.state.resource_store.get(workspace_id, created["id"])
        assert stored is not None
        assert stored.materialization.state == "failed"

        monkeypatch.setattr(
            materialization_module, "materialize_resource_for_app", original_materialize
        )
        blocks_again = describe_resource_parts(app, sid, [part])
        assert "has an agent-usable working copy" in blocks_again[0]
        stored_again = app.state.resource_store.get(workspace_id, created["id"])
        assert stored_again is not None
        assert stored_again.materialization.state == "ready"
