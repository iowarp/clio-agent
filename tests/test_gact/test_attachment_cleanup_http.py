"""Exercise attachment rollback through the real workspace HTTP routes."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.storage.workspace_files import connected_file_entries

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def test_unsent_file_and_source_cleanup_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    workspace, upstream = tmp_path / "workspace", tmp_path / "original"
    workspace.mkdir()
    upstream.mkdir()
    (upstream / "a.txt").write_bytes(b"source")
    app = build_app(agent=None, sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        result = client.post("/v1/workspaces", json={"name": "drafts", "root_path": str(workspace)})
        assert result.status_code == 201, result.text
        wid = result.json()["id"]
        base = f"/v1/workspaces/{wid}"
        for pending in (True, False):
            upload_id = f"upload-{pending}"
            payload = {
                "name": "ordinary.txt",
                "size": 4,
                "client_upload_id": upload_id,
                "pending_attachment": pending,
            }
            result = client.post(base + "/resources", json=payload)
            assert result.status_code == 201, result.text
            resource_id = result.json()["id"]
            result = client.patch(
                base + f"/resources/{resource_id}/content",
                content=b"data",
                headers={"Upload-Offset": "0", "Content-Type": "application/offset+octet-stream"},
            )
            assert result.status_code == 204, result.text
            record = app.state.resource_store.get(wid, resource_id)
            path = Path(record.workspace_path)
            assert path.exists()
            result = client.post(
                base + "/resources/discard-upload", json={"client_upload_id": upload_id}
            )
            assert result.status_code == 204, result.text
            assert path.exists() is not pending
            if pending:
                assert not path.parent.exists()
                assert client.post(base + "/resources", json=payload).status_code == 400
            else:
                assert app.state.resource_store.get(wid, resource_id) is not None

        result = client.post(
            base + "/sources",
            json={"provider": "local", "root": str(upstream), "label": "Temporary folder"},
        )
        assert result.status_code == 201, result.text
        source_id = result.json()["id"]
        source = base + f"/sources/{source_id}"
        lease = client.post(source + "/draft").json()["id"]
        from tests.test_gact.storage_operation_helpers import complete_indexing

        complete_indexing(client, source, client.post(source + "/link", json={"draft_id": lease}))
        result = client.post(
            source + "/reference", json={"folder": True, "linked": True, "draft_id": lease}
        )
        assert result.status_code == 200, result.text
        folder_resource = result.json()["id"]
        folder_path = Path(app.state.resource_store.get(wid, folder_resource).workspace_path)
        assert connected_file_entries(app.state.connected_storage, wid, 100)[0]
        result = client.post(source + f"/draft/{lease}/discard")
        assert result.status_code == 200, result.text
        assert connected_file_entries(app.state.connected_storage, wid, 100)[0] == []
        assert app.state.resource_store.get(wid, folder_resource) is None
        assert not folder_path.parent.exists()
        assert (upstream / "a.txt").read_bytes() == b"source"
        assert (
            client.post(
                source + "/reference", json={"folder": True, "linked": True, "draft_id": lease}
            ).status_code
            == 409
        )
