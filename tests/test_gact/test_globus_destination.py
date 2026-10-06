"""Receiving collection ownership, platform paths and real transfer mapping checks."""

from pathlib import Path
from typing import Any

import globus_sdk
import pytest
from clio_schemas.connected_resources import ConnectedSource, ResourceOwner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.routes.connected_storage import register_connected_storage_routes
from clio_agent.gact.storage.globus import GlobusSource
from clio_agent.gact.storage.globus_destination import (
    GlobusDestination,
    personal_collection_path,
    receiving_destination,
)
from clio_agent.gact.storage.models import SourceConfiguration, SourceRecord, TransferOperation
from clio_agent.gact.storage.store import SourceStore

COLLECTION = "22222222-2222-4222-8222-222222222222"


def test_native_discovery_and_explicit_host_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SourceStore(tmp_path / "sources")
    monkeypatch.setattr(globus_sdk.LocalGlobusConnectPersonal, "endpoint_id", COLLECTION)
    discovered, origin = receiving_destination(store)
    assert discovered and discovered.collection_id == COLLECTION and origin == "detected"
    assert discovered.local_root == str(store.root)
    configured = GlobusDestination(
        collection_id=COLLECTION, collection_root="/mapped", local_root=str(tmp_path)
    )
    store.put("host-config", "globus-destination", configured)
    restored, origin = receiving_destination(SourceStore(store.root))
    assert restored == configured and origin == "configured"


def test_windows_mapping_and_missing_receiving_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert personal_collection_path(r"D:\data\agent", windows=True) == "/d/data/agent"
    assert personal_collection_path("/data/agent", windows=False) == "/data/agent"
    with pytest.raises(ValueError, match="mapped drive"):
        personal_collection_path(r"\\server\share\agent", windows=True)
    monkeypatch.setattr(globus_sdk.LocalGlobusConnectPersonal, "endpoint_id", None)
    assert receiving_destination(SourceStore(tmp_path)) == (None, "unavailable")


def test_host_mapping_api_rejects_unrelated_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    monkeypatch.setattr(globus_sdk.LocalGlobusConnectPersonal, "endpoint_id", None)
    app = FastAPI()
    register_connected_storage_routes(app)
    with TestClient(app) as client:
        before = client.get("/v1/storage/globus-destination").json()
        assert before["origin"] == "unavailable"
        unrelated = tmp_path / "other"
        unrelated.mkdir()
        payload = {
            "collection_id": COLLECTION,
            "collection_root": "/mapped",
            "local_root": str(unrelated),
        }
        assert client.put("/v1/storage/globus-destination", json=payload).status_code == 409
        payload["local_root"] = str(tmp_path)
        result = client.put("/v1/storage/globus-destination", json=payload)
        assert result.status_code == 200 and result.json()["origin"] == "configured"
        assert client.get("/v1/storage/globus-destination").json() == result.json()


@pytest.mark.parametrize("selected_paths", [None, ["data.txt"]])
def test_transfer_resolves_host_destination_and_pins_native_request(
    tmp_path: Path, selected_paths: list[str] | None
) -> None:
    store = SourceStore(tmp_path / "sources")
    receiving = GlobusDestination(
        collection_id=COLLECTION, collection_root="/mapped", local_root=str(tmp_path)
    )
    store.put("host-config", "globus-destination", receiving)
    record = SourceRecord(
        source=ConnectedSource(
            id="source_one",
            provider="globus",
            label="Inputs",
            root="/inputs",
            capabilities=GlobusSource.capabilities,
            owner=ResourceOwner(clio_id=store.clio_id, host_id="local"),
        ),
        principal="test",
        configuration=SourceConfiguration(collection_id="11111111-1111-4111-8111-111111111111"),
    )
    store.put("source", record.source.id, record)

    class Client:
        valid_mapping = False
        requests: list[dict[str, Any]] = []

        def operation_ls(self, endpoint: str, *, path: str, **kwargs: Any) -> list[dict[str, str]]:
            if endpoint == record.configuration.collection_id:
                return [{"name": "data.txt", "type": "file", "size": "4"}]
            assert endpoint == COLLECTION
            local = tmp_path / path.removeprefix("/mapped/")
            return (
                [{"name": item.name, "type": "file"} for item in local.iterdir()]
                if self.valid_mapping
                else []
            )

        def get_submission_id(self) -> dict[str, str]:
            return {"value": "stable-submission"}

        def submit_transfer(self, body: dict[str, Any]) -> dict[str, str]:
            self.requests.append(body)
            raise TimeoutError("lost response")

    client = Client()
    adapter = GlobusSource(record, "test-token", client=client)
    operation = store.begin_operation(record.source.id, "materialize")
    operation = store.update_operation(operation.id, selected_paths=selected_paths)
    with pytest.raises(ValueError, match="does not map"):
        adapter.submit(store, operation)
    assert not client.requests
    assert not list(store.root.rglob(".clio-receiving-*"))
    client.valid_mapping = True
    with pytest.raises(TimeoutError):
        adapter.submit(store, operation)
    operation = store.get("operation", operation.id, TransferOperation)
    assert (
        operation.native_request and operation.native_request["destination_endpoint"] == COLLECTION
    )
    item = operation.native_request["DATA"][0]
    assert item["source_path"] == ("/inputs/data.txt" if selected_paths else "/inputs")
    assert item["recursive"] is (selected_paths is None)
    # A changed host setting cannot reroute the already submitted transfer on retry.
    store.put(
        "host-config",
        "globus-destination",
        receiving.model_copy(update={"collection_root": "/different"}),
    )
    with pytest.raises(TimeoutError):
        adapter.submit(store, operation)
    assert client.requests[0] == client.requests[1]
    assert not list(store.root.rglob(".clio-receiving-*"))
