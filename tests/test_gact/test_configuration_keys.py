"""Service-action configuration keys a service would ignore are a typed 400 (F006)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.infrastructure.configuration_keys import (
    UnknownConfigurationKeyError,
    validate_configuration_keys,
)
from clio_agent.gact.infrastructure.models import (
    InfrastructureOperation,
    ServiceActionRequest,
    ServiceRecord,
)
from clio_agent.gact.routes.infrastructure import register_infrastructure_routes


@pytest.mark.parametrize(
    ("key", "hint"),
    [
        ("tool_call_parser", "'param.tool_call_parser'"),
        ("max_model_len", "'param.max_model_len'"),
        ("--max-model-len", "'param.max_model_len'"),
        ("modle", "accepted keys are"),
        ("param.no_such_knob", "its server parameters are"),
    ],
)
def test_unprefixed_or_unknown_vllm_keys_are_rejected(key: str, hint: str) -> None:
    """Live F006: tool_call_parser/max_model_len without param. launched vLLM without them."""
    with pytest.raises(UnknownConfigurationKeyError) as error:
        validate_configuration_keys("vllm", {"model": "/models/qwen", key: "x"})
    assert error.value.key == key and repr(key) in str(error.value)
    assert hint in str(error.value)
    assert "param.<parameter>" in error.value.accepted


@pytest.mark.parametrize(
    ("service_id", "configuration"),
    [
        (
            "vllm",
            {
                "model": "/models/qwen",
                "model_revision": "f" * 40,
                "param.max_model_len": "32768",
                "port": "37153",
                "container_runtime": "apptainer",
                "storage.service_directory": "/data/vllm",
                "shareable": "false",
            },
        ),
        ("ollama", {"model": "qwen2.5:0.5b", "param.num_parallel": "2"}),
        ("llama_cpp", {"hf_model": "Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M"}),
        ("web_search", {"contact_email": "alice@example.org", "task_backend_port": "8090"}),
        (
            "clio_agent",
            {
                "port": "17800",
                "desktop_id": "desktop",
                "keep_running": "false",
                "on_conflict": "connect",
                "conflict_pid": "42",
                "conflict_root": "/home/alice/clio",
            },
        ),
        ("flowcept", {"container_runtime": "apptainer", "redis_port": "16379"}),
        # "Reinstall from scratch" applies to every service's install.
        ("web_search", {"container_runtime": "apptainer", "install.from_scratch": "true"}),
        ("ollama", {"install.from_scratch": "true"}),
        ("not_a_service", {"anything": "goes to the operation's own error"}),
    ],
)
def test_declared_and_control_keys_are_accepted(
    service_id: str, configuration: dict[str, str]
) -> None:
    validate_configuration_keys(service_id, configuration)


def test_installed_configuration_echoed_back_is_accepted() -> None:
    """The UI resends the installed configuration, including keys CLIO derived itself."""
    installed = {"model": "/models/qwen", "native_owner": "abc", "compatibility_profile": "p"}
    validate_configuration_keys("vllm", installed, installed)
    with pytest.raises(UnknownConfigurationKeyError, match="native_owner"):
        validate_configuration_keys("vllm", installed)


def test_param_keys_on_a_service_without_parameters_are_rejected() -> None:
    with pytest.raises(UnknownConfigurationKeyError, match="no server parameters"):
        validate_configuration_keys("web_search", {"param.threads": "4"})


def test_http_rejects_an_ignored_key_before_queuing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = FastAPI()
    register_infrastructure_routes(app, tmp_path)
    queued: list[ServiceActionRequest] = []

    def start_action(service_id: str, request: ServiceActionRequest) -> InfrastructureOperation:
        queued.append(request)
        return InfrastructureOperation(
            service_id=service_id, target_id=request.target_id, action=request.action
        )

    monkeypatch.setattr(app.state.infrastructure_runtime, "start_action", start_action)
    route = "/v1/infrastructure/services/vllm/actions"
    body: dict[str, Any] = {"action": "install", "variant_id": "cuda"}
    with TestClient(app) as client:
        rejected = client.post(
            route, json={**body, "configuration": {"model": "m", "tool_call_parser": "hermes"}}
        )
        assert rejected.status_code == 400
        detail = rejected.json()["detail"]
        assert detail["error"] == "unknown_configuration_key"
        assert detail["key"] == "tool_call_parser"
        assert "param.tool_call_parser" in detail["message"]
        assert queued == []
        accepted = client.post(
            route,
            json={**body, "configuration": {"model": "m", "param.tool_call_parser": "hermes"}},
        )
        assert accepted.status_code == 202, accepted.text
        app.state.infrastructure_store.put_service(
            ServiceRecord(
                id="local:vllm",
                service_id="vllm",
                target_id="local",
                variant_id="cuda",
                configuration={"model": "m", "native_owner": "abc"},
            )
        )
        echoed = client.post(
            route,
            json={
                **body,
                "action": "start",
                "configuration": {"model": "m", "native_owner": "abc"},
            },
        )
        assert echoed.status_code == 202, echoed.text
    assert len(queued) == 2
