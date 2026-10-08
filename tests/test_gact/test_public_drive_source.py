"""Anonymous Drive folder access uses gdrive-fsspec and requests login only on refusal."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.auth.credentials import AnonymousCredentials
from googleapiclient.discovery import build
from googleapiclient.http import HttpMockSequence

from clio_agent.gact.routes.connected_storage import register_connected_storage_routes
from clio_agent.gact.storage import linked
from clio_agent.gact.storage.models import SourceRecord


@pytest.mark.parametrize("private", [False, True])
def test_public_drive_browse_or_private_login_after_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, private: bool
) -> None:
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    monkeypatch.setenv("CLIO_STORAGE_GOOGLE_API_KEY", "clio-test-api-key")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = FastAPI()
    app.state.workspaces = {"w": SimpleNamespace(root_path=str(workspace))}
    register_connected_storage_routes(app)
    response = (
        {"error": {"message": "Forbidden"}}
        if private
        else {
            "files": [
                {
                    "id": "file1",
                    "name": "data.txt",
                    "mimeType": "text/plain",
                    "size": "4",
                    "version": "1",
                    "modifiedTime": "2026-01-01T00:00:00Z",
                }
            ]
        }
    )
    calls: list[object] = []

    def google_client(*args: Any, credentials: object, **kwargs: Any) -> Any:
        calls.append(credentials)
        assert isinstance(credentials, AnonymousCredentials)
        assert kwargs["developerKey"] == "clio-test-api-key"
        responses = [({"status": "404" if private else "200"}, json.dumps(response).encode())]
        if not private:
            responses.insert(
                0,
                (
                    {"status": "200"},
                    json.dumps(
                        {
                            "id": "public-folder",
                            "mimeType": "application/vnd.google-apps.folder",
                        }
                    ).encode(),
                ),
            )
        http = HttpMockSequence(responses)
        monkeypatch.setattr(http, "close", lambda: None, raising=False)
        request = http.request

        def check_request(uri: str, *args: Any, **kwargs: Any) -> Any:
            assert parse_qs(urlsplit(uri).query)["key"] == ["clio-test-api-key"]
            assert "authorization" not in {key.lower() for key in kwargs.get("headers", {})}
            return request(uri, *args, **kwargs)

        monkeypatch.setattr(http, "request", check_request)
        return build(
            "drive", "v3", http=http, developerKey=kwargs["developerKey"], static_discovery=True
        )

    monkeypatch.setattr(linked, "build", google_client)
    with TestClient(app) as client:
        created = client.post(
            "/v1/workspaces/w/sources",
            json={
                "provider": "google_drive",
                "root": "public-folder",
                "label": "Data",
            },
        )
        assert created.status_code == 201
        source = created.json()
        assert source["access_without_signin"] and not source["authenticated"]
        path = f"/v1/workspaces/w/sources/{source['id']}"
        result = client.post(path + "/browse", json={})
        if private:
            assert result.status_code == 403, result.text
            assert "Sign in to Google Drive" in result.text
            status = client.get("/v1/workspaces/w/sources").json()["sources"][0]
            assert not status["access_without_signin"]
            assert client.post(path + "/browse", json={}).status_code == 403
            assert len(calls) == 1
        else:
            assert result.status_code == 200, result.text
            assert result.json()["entries"][0]["path"] == "data.txt"
            link_result = client.post(path + "/link", json={})
            from tests.test_gact.storage_operation_helpers import complete_indexing

            complete_indexing(client, path, link_result)
            record = app.state.connected_storage.store.get("source", source["id"], SourceRecord)
            assert record.linked_manifest_id and not record.sign_in_required
            assert len(calls) == 2


@pytest.mark.parametrize(
    ("status", "reason", "message"),
    [
        (403, "forbidden", "Method doesn't allow unregistered callers"),
        (400, "API_KEY_INVALID", "API key not valid"),
        (403, "SERVICE_DISABLED", "Drive API disabled"),
        (403, "API_KEY_SERVICE_BLOCKED", "API not allowed"),
        (403, "userRateLimitExceeded", "Quota exceeded"),
        (429, "RESOURCE_EXHAUSTED", "Too many requests"),
    ],
)
def test_application_or_quota_failure_does_not_make_folder_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, reason: str, message: str
) -> None:
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    monkeypatch.setenv("CLIO_STORAGE_GOOGLE_API_KEY", "clio-test-api-key")
    app = FastAPI()
    app.state.workspaces = {"w": SimpleNamespace(root_path=str(tmp_path))}
    register_connected_storage_routes(app)

    def google_client(*args: Any, **kwargs: Any) -> Any:
        payload = {"error": {"message": message, "errors": [{"reason": reason}]}}
        http = HttpMockSequence([({"status": str(status)}, json.dumps(payload).encode())])
        monkeypatch.setattr(http, "close", lambda: None, raising=False)
        return build("drive", "v3", http=http, static_discovery=True)

    monkeypatch.setattr(linked, "build", google_client)
    with TestClient(app) as client:
        source = client.post(
            "/v1/workspaces/w/sources",
            json={"provider": "google_drive", "root": "public-folder", "label": "Data"},
        ).json()
        response = client.post(f"/v1/workspaces/w/sources/{source['id']}/browse", json={})
        assert response.status_code == 409, response.text
        assert "clio-test-api-key" not in response.text
        assert "Sign in to Google Drive to access this folder" not in response.text
        current = client.get("/v1/workspaces/w/sources").json()["sources"][0]
        assert current["access_without_signin"]
        record = app.state.connected_storage.store.get("source", source["id"], SourceRecord)
        assert not record.sign_in_required


def test_missing_public_application_key_does_not_request_or_infer_folder_privacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    monkeypatch.delenv("CLIO_STORAGE_GOOGLE_API_KEY", raising=False)
    app = FastAPI()
    app.state.workspaces = {"w": SimpleNamespace(root_path=str(tmp_path))}
    register_connected_storage_routes(app)
    with TestClient(app) as client:
        source = client.post(
            "/v1/workspaces/w/sources",
            json={"provider": "google_drive", "root": "public-folder", "label": "Data"},
        ).json()
        response = client.post(f"/v1/workspaces/w/sources/{source['id']}/browse", json={})
        assert response.status_code == 409
        assert "Public Google Drive access is not set up" in response.text
        assert client.get("/v1/workspaces/w/sources").json()["sources"][0]["access_without_signin"]
