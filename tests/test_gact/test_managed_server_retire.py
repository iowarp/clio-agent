"""Uninstalling a managed model server retires the saved server entry CLIO linked to it.

"Use in Models" saves a managed deployment as a model server (linked by the
deployment's key ref, or -- keyless -- by engine and address). Uninstall
removes that entry; entries pointing elsewhere stay. A session still pinned to
the removed deployment gets a typed ``model_deployment_removed`` refusal until
the engine is deployed again or another server is saved for it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from clio_agent.gact import local_server_store as saved
from clio_agent.gact.infrastructure.models import ServiceRecord
from clio_agent.gact.infrastructure.server_access import deployment_key_ref, retire_saved_servers
from clio_agent.gact.infrastructure.store import InfrastructureStore
from clio_agent.gact.providers.config import removed_deployment_error


@pytest.fixture()
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config" / "config.yaml"
    monkeypatch.setattr(saved, "user_config_path", lambda: path)
    return path


def _record(service_id: str, url: str, state: str = "running") -> ServiceRecord:
    return ServiceRecord.model_validate(
        {
            "id": f"local:{service_id}",
            "variant_id": "cuda",
            "target_id": "local",
            "service_id": service_id,
            "state": state,
            "connection_url": url,
        }
    )


def test_uninstall_removes_the_linked_entry_and_keeps_others(
    config_file: Path, tmp_path: Path
) -> None:
    store = InfrastructureStore(tmp_path / "infra.json")
    saved.add_server(
        address="http://127.0.0.1:8088/v1",
        preset_id="llama_cpp",
        credential_ref=deployment_key_ref("local", "llama_cpp"),
    )
    other = saved.add_server(address="http://gpu-node:8000/v1", label="My vLLM")

    removed = retire_saved_servers(store, _record("llama_cpp", "http://127.0.0.1:8088"))

    assert removed == ["llama_cpp"]
    assert [entry.id for entry in saved.list_servers()] == [other.id]
    assert store.removed_deployment("llama_cpp") is not None


def test_keyless_deployment_entry_is_linked_by_engine_and_address(
    config_file: Path, tmp_path: Path
) -> None:
    store = InfrastructureStore(tmp_path / "infra.json")
    saved.add_server(address="http://127.0.0.1:11434/v1", preset_id="ollama")

    assert retire_saved_servers(store, _record("ollama", "http://127.0.0.1:11434")) == ["ollama"]


def test_entry_of_the_same_engine_elsewhere_stays(config_file: Path, tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infra.json")
    saved.add_server(address="http://other-host:8088/v1", preset_id="llama_cpp")

    assert retire_saved_servers(store, _record("llama_cpp", "http://127.0.0.1:8088")) == []
    assert saved.get_server("llama_cpp") is not None


def test_not_a_model_server_is_untouched(config_file: Path, tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infra.json")

    assert retire_saved_servers(store, _record("flowcept", "http://127.0.0.1:5000")) == []
    assert store.removed_deployment("flowcept") is None


def test_removed_deployment_survives_a_restart_and_clears(tmp_path: Path) -> None:
    path = tmp_path / "infra.json"
    InfrastructureStore(path).note_removed_deployment("local", "vllm", "http://127.0.0.1:37153")

    reloaded = InfrastructureStore(path)
    assert reloaded.removed_deployment("vllm") == {
        "target_id": "local",
        "service_id": "vllm",
        "address": "http://127.0.0.1:37153",
        "removed_at": reloaded.removed_deployment("vllm")["removed_at"],  # type: ignore[index]
    }
    reloaded.clear_removed_deployment("local", "vllm")
    assert InfrastructureStore(path).removed_deployment("vllm") is None


def _app(store: InfrastructureStore) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(infrastructure_store=store))


def test_a_pinned_session_gets_a_typed_refusal(config_file: Path, tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infra.json")
    store.note_removed_deployment("local", "llama_cpp", "http://127.0.0.1:8088")
    ref = {"provider_id": "llama_cpp", "model_id": "/models/q.gguf"}

    envelope = removed_deployment_error(_app(store), ref, session_id="sess_1", source="session")

    assert envelope is not None
    assert envelope.error.error == "model_deployment_removed"
    assert envelope.error.recoverable is True
    assert "choose_model" in envelope.error.details["recovery_actions"]
    other = {"provider_id": "vllm", "model_id": "m"}
    assert removed_deployment_error(_app(store), other, session_id="s", source="session") is None


def test_no_refusal_once_replaced(config_file: Path, tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infra.json")
    store.note_removed_deployment("local", "llama_cpp", "http://127.0.0.1:8088")
    ref = {"provider_id": "llama_cpp", "model_id": "m"}

    saved.add_server(address="http://other-host:8088/v1", preset_id="llama_cpp")
    assert removed_deployment_error(_app(store), ref, session_id="s", source="session") is None

    saved.remove_server("llama_cpp")
    store.put_service(_record("llama_cpp", "http://127.0.0.1:9000"))
    assert removed_deployment_error(_app(store), ref, session_id="s", source="session") is None
