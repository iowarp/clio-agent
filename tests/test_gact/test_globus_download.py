"""Filesystem downloads preserve native jobs, credentials and approved workspace visibility."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import globus_sdk
import httpx
import pytest

from clio_agent.gact.storage.auth import StorageAuth
from clio_agent.gact.storage.globus_consent import TRANSFER_SCOPE
from clio_agent.gact.storage.linked import link_folder
from clio_agent.gact.storage.models import CreateSource, SourceConfiguration
from clio_agent.gact.storage.service import StorageService
from clio_agent.gact.storage.workspace_files import connected_file_entries, resolve_connected_input

COLLECTION = "6c54cade-bde5-45c1-bdea-f4bd71dba2cc"
HTTPS_SCOPE = f"https://auth.globus.org/scopes/{COLLECTION}/https"


def test_globus_fsspec_link_and_workspace_visibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without receiving storage, linking fetches a selected file through globusfs."""
    from clio_agent.gact.storage import globus_download

    service = StorageService(tmp_path / "sources", tmp_path / "private" / "tokens.json")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    record = service.create(
        "w",
        CreateSource(
            provider="globus",
            label="Tutorial",
            root="/sample",
            configuration=SourceConfiguration(collection_id=COLLECTION),
        ),
    )
    payload = {
        "resource_server": "transfer.api.globus.org",
        "access_token": "transfer-only",
        "refresh_token": "transfer-refresh",
        "other_tokens": [
            {
                "resource_server": COLLECTION,
                "access_token": "collection-only",
                "scope": HTTPS_SCOPE,
            },
            {
                "resource_server": "unrelated",
                "access_token": "must-not-store",
                "scope": HTTPS_SCOPE,
            },
        ],
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        flow = service.auth.start(record)
        service.auth.complete(record, flow["flow_id"], "test-code", client=client)
    assert service.auth.token(record) == "transfer-only"
    assert service.auth.token(record, resource_server=COLLECTION) == "collection-only"
    with pytest.raises(PermissionError):
        service.auth.token(record, resource_server="unrelated")
    assert "must-not-store" not in service.auth._vault.read_entries().__repr__()

    monkeypatch.setattr(
        globus_sdk.TransferClient,
        "get_endpoint",
        lambda *_: {"id": COLLECTION, "https_server": "https://collection.test"},
    )
    monkeypatch.setattr(
        globus_sdk.TransferClient,
        "operation_ls",
        lambda *_, **__: [
            {"name": "data.txt", "type": "file", "size": 4, "last_modified": "revision-1"}
        ],
    )
    opened: list[str] = []

    class Filesystem:
        _session = None

        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["credentials"].token_for(COLLECTION) == "collection-only"
            assert kwargs["allow_redirects"] is False

        def open(self, path: str, mode: str, **kwargs: Any) -> io.BytesIO:
            assert mode == "rb"
            assert kwargs["headers"]["Authorization"] == "Bearer collection-only"
            opened.append(path)
            return io.BytesIO(b"data")

    monkeypatch.setattr(globus_download, "GlobusFileSystem", Filesystem)
    monkeypatch.setattr(
        globus_sdk.TransferClient,
        "submit_transfer",
        lambda *_: pytest.fail("Linking must not submit a native transfer"),
    )
    link_folder(service, record)
    assert opened == []
    assert not service.download_available(record)
    with pytest.raises(ValueError, match="transfer backend"):
        service.start_transfer(record, workspace)
    rows, truncated = connected_file_entries(service, "w", 100)
    assert not truncated and len(rows) == 1
    assert rows[0]["display_path"] == "Linked folders/Tutorial/data.txt"
    resolved = resolve_connected_input(service, "w", rows[0]["path"])
    assert resolved is not None and resolved.read_bytes() == b"data"
    assert opened == [f"globus://{COLLECTION}/sample/data.txt"]
    with pytest.raises(KeyError):
        resolve_connected_input(service, "other-workspace", rows[0]["path"])
    with pytest.raises(ValueError):
        resolve_connected_input(service, "w", rows[0]["path"] + "/../secret")
    service.disconnect(service.get("w", record.source.id))
    with pytest.raises(ValueError, match="disconnected"):
        resolve_connected_input(service, "w", rows[0]["path"])
    assert not StorageAuth(tmp_path / "private" / "tokens.json").connected(record)


def test_download_consent_retains_transfer_scope(tmp_path: Path) -> None:
    """An HTTPS consent request still returns the Transfer token needed for listing."""
    service = StorageService(tmp_path / "sources", tmp_path / "private" / "tokens.json")
    record = service.create(
        "w",
        CreateSource(
            provider="globus",
            label="Tutorial",
            root="/",
            configuration=SourceConfiguration(collection_id=COLLECTION),
        ),
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "resource_server": "transfer.api.globus.org",
                    "access_token": "test-transfer",
                },
            )
        )
    ) as client:
        flow = service.auth.start(record)
        service.auth.complete(record, flow["flow_id"], "test-code", client=client)
    service.auth.require_globus_consent(record, [HTTPS_SCOPE])
    assert not service.auth.connected(record)
    flow = service.auth.start(record)
    scopes = parse_qs(urlparse(flow["authorization_url"]).query)["scope"][0].split()
    assert set(scopes) == {HTTPS_SCOPE, TRANSFER_SCOPE, "offline_access"}
