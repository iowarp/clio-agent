"""Saved local and self-hosted servers: the store and ``/v1/providers/servers``.

The store writes the user ``config.yaml`` (redirected to a tmp file here);
the routes are driven through ``build_app`` with the live handshake replaced,
so each test states exactly what the server at an address answered.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from clio_agent.gact import local_server_store as store
from clio_agent.gact.app import build_app
from clio_agent.gact.routes import local_servers


@pytest.fixture()
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config" / "config.yaml"
    monkeypatch.setattr(store, "user_config_path", lambda: path)
    return path


@pytest.fixture()
def probes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Replace the live handshake: ``:9999`` answers with two models, anything else is down."""
    calls: list[tuple[str, str]] = []

    async def fake_check(preset: Any, address: str) -> dict[str, Any]:
        calls.append((preset.id, address))
        up = ":9999" in address
        return {
            "reachable": up,
            "connectivity": "ok" if up else "unreachable",
            "models": ["qwen3-8b", "gemma-3"] if up else [],
            "error": "" if up else "connection refused",
            "checked_at": "2026-09-26T00:00:00+00:00",
        }

    monkeypatch.setattr(local_servers, "check_address", fake_check)
    return calls


@pytest.fixture()
def client(tmp_path: Path, config_file: Path, probes: list[tuple[str, str]]) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


def test_address_normalization() -> None:
    assert store.normalize_server_address("127.0.0.1:1235") == "http://127.0.0.1:1235/v1"
    assert store.normalize_server_address("https://gpu-7:8000/v1/") == "https://gpu-7:8000/v1"
    with pytest.raises(store.LocalServerStoreError):
        store.normalize_server_address("")
    with pytest.raises(store.LocalServerStoreError):
        store.normalize_server_address("ftp://host/x")


def test_store_keeps_every_other_key_in_the_user_config(config_file: Path) -> None:
    config_file.parent.mkdir(parents=True)
    config_file.write_text(
        yaml.safe_dump({"debug": {"level": "high"}, "providers": {"other": 1}}), encoding="utf-8"
    )

    store.add_server(address="127.0.0.1:1235", preset_id="lm_studio", label="LM Studio")

    document = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert document["debug"] == {"level": "high"}
    assert document["providers"]["other"] == 1
    assert document["providers"]["servers"] == [
        {
            "id": "lm_studio",
            "preset_id": "lm_studio",
            "label": "LM Studio",
            "address": "http://127.0.0.1:1235/v1",
        }
    ]


def test_a_catalog_runtime_has_one_entry_and_custom_servers_get_unique_ids(
    config_file: Path,
) -> None:
    store.add_server(address="127.0.0.1:1235", preset_id="lm_studio")
    store.add_server(address="127.0.0.1:1236", preset_id="lm_studio")
    first = store.add_server(address="gpu-7:8000", label="GPU node")
    second = store.add_server(address="gpu-8:8000", label="GPU node")

    assert [e.id for e in store.list_servers()] == ["lm_studio", first.id, second.id]
    assert store.saved_address_for_preset("lm_studio") == "http://127.0.0.1:1236/v1"
    assert first.id == "server-gpu-node" and second.id == "server-gpu-node-2"
    assert first.preset_id == store.CUSTOM_SERVER_PRESET_ID
    # A custom server never overrides its preset's own address.
    assert store.saved_address_for_preset(store.CUSTOM_SERVER_PRESET_ID) is None


def test_update_and_remove(config_file: Path) -> None:
    entry = store.add_server(address="gpu-7:8000", label="GPU node")
    updated = store.update_server(entry.id, address="gpu-7:9000", label="Big node")
    assert (updated.address, updated.label) == ("http://gpu-7:9000/v1", "Big node")
    store.remove_server(entry.id)
    assert store.list_servers() == []
    with pytest.raises(KeyError):
        store.remove_server(entry.id)
    with pytest.raises(KeyError):
        store.update_server("nope", address="x:1")


def test_a_malformed_section_is_a_typed_error_and_discovery_ignores_it(config_file: Path) -> None:
    config_file.parent.mkdir(parents=True)
    config_file.write_text("providers:\n  servers: 7\n", encoding="utf-8")
    with pytest.raises(store.LocalServerStoreError):
        store.list_servers()
    assert store.saved_address_for_preset("lm_studio") is None


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


def test_add_a_catalog_runtime_address_checks_it_and_it_becomes_the_probed_address(
    client: TestClient, probes: list[tuple[str, str]]
) -> None:
    body = client.post(
        "/v1/providers/servers", json={"preset_id": "lm_studio", "address": "127.0.0.1:9999"}
    ).json()

    assert body["id"] == "lm_studio" and body["custom"] is False
    assert body["label"] == "LM Studio"
    assert body["check"]["reachable"] is True
    assert body["check"]["models"] == ["qwen3-8b", "gemma-3"]
    assert probes == [("lm_studio", "http://127.0.0.1:9999/v1")]
    # The saved address is what the provider list and discovery now use.
    presets = {p["id"]: p for p in client.get("/v1/providers/lm").json()["presets"]}
    assert presets["lm_studio"]["api_base"] == "http://127.0.0.1:9999/v1"
    from clio_agent.gact.provider_catalog_snapshot import _resolve_presets

    assert _resolve_presets(client.app)["lm_studio"].api_base == "http://127.0.0.1:9999/v1"


def test_add_a_custom_server_list_check_and_remove(client: TestClient) -> None:
    added = client.post(
        "/v1/providers/servers", json={"address": "gpu-7:8000", "label": "GPU node"}
    ).json()
    assert added["custom"] is True and added["preset_id"] == "vllm"
    assert added["check"] == {
        "reachable": False,
        "connectivity": "unreachable",
        "models": [],
        "error": "connection refused",
        "checked_at": "2026-09-26T00:00:00+00:00",
    }

    listed = client.get("/v1/providers/servers").json()["servers"]
    assert [s["id"] for s in listed] == [added["id"]]
    assert listed[0]["check"]["reachable"] is False

    moved = client.patch(
        f"/v1/providers/servers/{added['id']}", json={"address": "gpu-7:9999"}
    ).json()
    assert moved["address"] == "http://gpu-7:9999/v1" and moved["check"]["reachable"] is True

    assert client.post(f"/v1/providers/servers/{added['id']}/check").json()["check"]["reachable"]
    assert client.delete(f"/v1/providers/servers/{added['id']}").json() == {"removed": added["id"]}
    assert client.get("/v1/providers/servers").json() == {"servers": []}


def test_list_with_check_probes_every_saved_address(
    client: TestClient, probes: list[tuple[str, str]]
) -> None:
    client.post("/v1/providers/servers", json={"preset_id": "ollama", "address": "10.0.0.5:11434"})
    client.post("/v1/providers/servers", json={"address": "gpu-7:9999"})
    probes.clear()

    servers = client.get("/v1/providers/servers", params={"check": "true"}).json()["servers"]

    assert sorted(probes) == [
        ("ollama", "http://10.0.0.5:11434/v1"),
        ("vllm", "http://gpu-7:9999/v1"),
    ]
    assert [s["check"]["reachable"] for s in servers] == [False, True]


def test_errors_are_typed(client: TestClient) -> None:
    unknown = client.post("/v1/providers/servers", json={"preset_id": "nope", "address": "x:1"})
    assert unknown.status_code == 404
    invalid = client.post("/v1/providers/servers", json={"address": "ftp://x/y"})
    assert invalid.status_code == 422
    assert "invalid_server" in invalid.text
    assert client.patch("/v1/providers/servers/missing", json={"address": "x:1"}).status_code == 404
    assert client.delete("/v1/providers/servers/missing").status_code == 404
    assert client.post("/v1/providers/servers/missing/check").status_code == 404


def test_removing_a_runtime_address_puts_the_preset_back_at_its_own(client: TestClient) -> None:
    client.post(
        "/v1/providers/servers", json={"preset_id": "lm_studio", "address": "127.0.0.1:9999"}
    )
    client.delete("/v1/providers/servers/lm_studio")
    presets = {p["id"]: p for p in client.get("/v1/providers/lm").json()["presets"]}
    assert presets["lm_studio"]["api_base"] == "http://127.0.0.1:1234/v1"
