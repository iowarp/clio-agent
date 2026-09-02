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
from clio_agent.gact.messaging import _dspy_images_from_parts
from clio_agent.gact.parts import Part
from clio_agent.gact.protocol.v3.message import part_to_v3_block
from clio_agent.gact.resource_custody import ResourceLimitError, ResourceStore
from clio_agent.gact.resource_processing import (
    ResourceConverterFactory,
    ResourceProcessingRecord,
    ResourceProcessingStore,
)
from clio_agent.gact.resource_tools import (
    inspect_workspace_resource,
    list_workspace_resources,
    read_workspace_resource_structure,
    read_workspace_resource_text,
    search_workspace_resource,
)
from tests.test_gact.test_post_messages import FakeClioAgent

pytestmark = pytest.mark.usefixtures("host_agent_executor")


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
        assert resource_root.exists()
        assert client.delete(f"/v1/workspaces/{workspace_id}").status_code == 204
        assert not resource_root.exists()
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
                "id": "codex",
                "health": "ready",
                "models": [
                    {
                        "model_id": "gpt-5.3-codex-spark",
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
                "model": {"provider_id": "codex", "model_id": "gpt-5.3-codex-spark"},
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
            "provider": "codex",
            "model": "gpt-5.3-codex-spark",
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


def test_a_result_without_a_derivative_manifest_is_refused_not_patched_up(
    tmp_path: Path,
) -> None:
    """The manifest is a contract term, so a missing one fails typed.

    The document service emits ``derivatives`` on every completed result
    (verified against clio-web-search: ``build_derivative_manifest`` always
    yields at least the markdown entry). The consumer-side shim that used to
    synthesize a manifest from ``markdown`` was therefore unreachable for
    anything the service produces today, and silently "fixing" a shape CLIO
    does not understand is exactly the repair core must not do.
    """

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

        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        user = next(row for row in messages if row["id"] == "resource_private_context")
        visible_text = "\n".join(
            str(part.get("text") or "") for part in user["parts"] if part["type"] == "text"
        )
        assert visible_text == "Summarize the attachment"
        assert "private runtime context" not in visible_text
        assert "private attachment content" not in visible_text


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
        assert f"query resource {record.id!r} with workspace_resource_inspect" in prompt
        assert "No structured converter was selected" not in prompt


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
