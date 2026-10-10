"""The managed model router: an upstream LiteLLM Proxy CLIO configures and supervises.

CLIO generates the proxy's configuration from the target's running vLLM
instances (secrets only as environment references; keys only through a
launch's stdin), restarts it when instances start, stop or are removed,
proves its identity at readiness, binds it as one provider, refuses a model
it cannot serve with a typed error, and retires its saved server on uninstall.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact import local_server_store as saved
from clio_agent.gact.infrastructure import node_service
from clio_agent.gact.infrastructure.drivers import build_driver_plan
from clio_agent.gact.infrastructure.model_instances import ROUTER_SERVICE
from clio_agent.gact.infrastructure.model_router import (
    CONFIG_FILE,
    DIGEST_FIELD,
    IDLE_FIELD,
    LITELLM_VERSION,
    MASTER_KEY_VARIABLE,
    MODELS_FIELD,
    MODELS_FILE,
    ROUTER_VARIANT,
    RoutedModel,
    RouterInputs,
    config_digest,
    model_names,
    routed_models,
    router_config,
    router_inputs,
    router_model_problem,
    router_target,
)
from clio_agent.gact.infrastructure.model_router_runtime import ModelRouterMixin
from clio_agent.gact.infrastructure.models import (
    InfrastructureOperation,
    InfrastructureTarget,
    ServiceActionRequest,
    ServiceRecord,
    TargetFacts,
)
from clio_agent.gact.infrastructure.server_access import (
    deployment_key_ref,
    launch_key,
    retire_saved_servers,
    store_key,
)
from clio_agent.gact.infrastructure.store import InfrastructureStore
from clio_agent.gact.providers.config import removed_deployment_error, router_model_error
from clio_agent.gact.routes import local_servers

ROUTER_URL = "http://127.0.0.1:41500"


@pytest.fixture(autouse=True)
def _isolated_user_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_USER_DIR", str(tmp_path / "user"))


@pytest.fixture()
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config" / "config.yaml"
    monkeypatch.setattr(saved, "user_config_path", lambda: path)
    return path


def _facts() -> TargetFacts:
    return TargetFacts.model_validate(
        {
            "target_id": "node",
            "label": "Node",
            "os": "linux",
            "arch": "x86_64",
            "accelerator": "nvidia",
            "uv_available": True,
            "hostname": "node",
            "agent_data_root": "/data/clio",
        }
    )


def _instance(
    service_id: str, model: str, port: int, state: str = "running", **extra: str
) -> ServiceRecord:
    return ServiceRecord(
        id=f"local:{service_id}",
        service_id=service_id,
        target_id="local",
        variant_id="native-cuda",
        configuration={"model": model, "port": str(port), **extra},
        state=state,  # type: ignore[arg-type]
        connection_url=f"http://127.0.0.1:{port}",
    )


def _router(state: str = "running", **configuration: str) -> ServiceRecord:
    return ServiceRecord(
        id=f"local:{ROUTER_SERVICE}",
        service_id=ROUTER_SERVICE,
        target_id="local",
        variant_id=ROUTER_VARIANT,
        configuration={"port": "41500", **configuration},
        state=state,  # type: ignore[arg-type]
        connection_url=ROUTER_URL,
    )


MODELS = (
    RoutedModel("Qwen/Qwen3-4B", "vllm@qwen3-4b", "Qwen/Qwen3-4B", 41001, True),
    RoutedModel("/m/qwen3-1.7b", "vllm@qwen3-1-7b", "/m/qwen3-1.7b", 41002, False),
)


# --------------------------------------------------------------------------- pinning


def test_the_proxy_is_pinned_to_clios_own_litellm() -> None:
    assert importlib.metadata.version("litellm") == LITELLM_VERSION


# --------------------------------------------------------------------------- configuration


def test_the_configuration_names_each_instance_and_no_secret() -> None:
    config = router_config(MODELS)

    entries = config["model_list"]
    assert entries == [
        {
            "model_name": "Qwen/Qwen3-4B",
            "litellm_params": {
                "model": "hosted_vllm/Qwen/Qwen3-4B",
                "api_base": "http://127.0.0.1:41001/v1",
                "api_key": "os.environ/CLIO_ROUTER_UPSTREAM_KEY_0",
            },
        },
        {
            "model_name": "/m/qwen3-1.7b",
            "litellm_params": {
                "model": "hosted_vllm//m/qwen3-1.7b",
                "api_base": "http://127.0.0.1:41002/v1",
                "api_key": "no-key",
            },
        },
    ]
    general = config["general_settings"]
    assert isinstance(general, dict)
    assert general["master_key"] == f"os.environ/{MASTER_KEY_VARIABLE}"
    assert general["store_model_in_db"] is False
    assert config["litellm_settings"]["telemetry"] is False  # type: ignore[index]
    assert config_digest(MODELS) != config_digest(MODELS[:1])


def test_names_are_stable_and_unique_across_instances() -> None:
    records = [
        _instance("vllm@b", "Qwen/Qwen3-4B", 41002),
        _instance("vllm", "Qwen/Qwen3-4B", 8000, state="stopped"),
        _instance("vllm@c", "/m/q", 41003),
        _instance("llama_cpp", "x", 8088),
    ]

    names = model_names(records, "local")

    assert list(names) == ["Qwen/Qwen3-4B", "Qwen/Qwen3-4B@b", "/m/q"]
    routed = routed_models(records, "local")
    assert [model.service_id for model in routed] == ["vllm@b", "vllm@c"]
    assert routed[0].name == "Qwen/Qwen3-4B@b"  # the stopped default keeps its name
    assert routed[0].served == "Qwen/Qwen3-4B" and routed[1].port == 41003


def test_inputs_load_each_keyed_instances_key(tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infra.json")
    store.put_service(_instance("vllm@a", "Qwen/Qwen3-4B", 41001))
    store.put_service(_instance("vllm@b", "/m/q", 41002, shareable="true"))

    inputs = router_inputs(store, "local", lambda target, service: f"key-{service}")

    assert [model.keyed for model in inputs.models] == [True, False]
    assert inputs.upstream_keys == (("vllm@a", "key-vllm@a"),)


# --------------------------------------------------------------------------- plan


def _plan(action: str, inputs: RouterInputs | None = None, key: str | None = "sk-clio-m") -> Any:
    return build_driver_plan(
        service_id=ROUTER_SERVICE,
        action=action,
        variant_id=ROUTER_VARIANT,
        configuration={"port": "41500"},
        facts=_facts(),
        target=InfrastructureTarget(id="node", label="Node", kind="ssh"),
        api_key=key,
        router=inputs,
    )


def test_install_builds_its_own_pinned_uv_environment() -> None:
    plan = _plan("install")

    body = json.loads(plan.commands[-1].stdin)
    manifest = body["manifest"]
    assert f'"litellm[proxy]=={LITELLM_VERSION}"' in manifest["project"]
    assert manifest["arguments"] == [
        "--config",
        CONFIG_FILE,
        "--host",
        "127.0.0.1",
        "--port",
        "41500",
        "--telemetry",
        "False",
    ]
    assert manifest["identity"] == {"kind": "openai", "served_models_file": MODELS_FILE}
    assert manifest["environment"]["LITELLM_TELEMETRY"] == "False"
    assert manifest["environment"]["DATABASE_URL"] == ""
    assert plan.configuration["storage.service_directory"].endswith(
        "/services/node/clio-model-router"
    )
    assert "secret_env" not in body and "api_key" not in body


def test_a_start_hands_keys_only_through_stdin() -> None:
    inputs = RouterInputs(models=MODELS, upstream_keys=(("vllm@qwen3-4b", "upstream-secret"),))

    plan = _plan("start", inputs)

    (start,) = plan.commands
    body = json.loads(start.stdin)
    assert body["api_key"] == "sk-clio-m"
    assert body["api_key_variable"] == MASTER_KEY_VARIABLE
    assert body["secret_env"] == {"CLIO_ROUTER_UPSTREAM_KEY_0": "upstream-secret"}
    assert json.loads(body["runtime_files"][MODELS_FILE]) == ["Qwen/Qwen3-4B", "/m/qwen3-1.7b"]
    rendered = body["runtime_files"][CONFIG_FILE]
    assert json.loads(rendered) == router_config(MODELS)
    every = [plan.configuration, body["manifest"], rendered]
    every += [[spec.program, *spec.args] for spec in plan.commands]
    every += [plan.readiness.health.args, plan.readiness.logs.stdin]
    for part in every:
        assert "upstream-secret" not in json.dumps(part)
        assert "sk-clio-m" not in json.dumps(part)
    assert plan.configuration[DIGEST_FIELD] == config_digest(MODELS)
    assert json.loads(plan.configuration[MODELS_FIELD]) == {
        "Qwen/Qwen3-4B": "vllm@qwen3-4b",
        "/m/qwen3-1.7b": "vllm@qwen3-1-7b",
    }
    assert plan.readiness.capability == "serving"
    # Readiness proves identity with the per-launch master key.
    assert json.loads(plan.readiness.health.stdin)["api_key"] == "sk-clio-m"


def test_a_start_with_nothing_to_route_or_no_key_is_refused() -> None:
    with pytest.raises(ValueError, match="router_without_models"):
        _plan("start", RouterInputs())
    with pytest.raises(ValueError, match="always runs with its key"):
        _plan("start", RouterInputs(models=MODELS), key=None)
    with pytest.raises(ValueError, match="no key for vllm@qwen3-4b"):
        _plan("start", RouterInputs(models=MODELS))


def test_the_router_key_is_made_fresh_for_every_launch() -> None:
    store_key("local", ROUTER_SERVICE, "sk-clio-old")
    store_key("local", "vllm@a", "instance-key")

    fresh = launch_key("local", ROUTER_SERVICE, "start", {"shareable": "true"})

    assert fresh and fresh.startswith("sk-clio-") and fresh != "sk-clio-old"
    assert launch_key("local", "vllm@a", "start", {}) == "instance-key"


# --------------------------------------------------------------------------- readiness identity


@contextmanager
def _proxy(key: str, models: list[str]) -> Iterator[int]:
    """A loopback stand-in for LiteLLM Proxy: refuses keyless /v1/models, lists ``models``."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path == "/v1/models" and self.headers.get("Authorization") != f"Bearer {key}":
                self.send_response(401)
                self.end_headers()
                return
            body = json.dumps({"data": [{"id": name} for name in models]}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _running(root: Path, port: int, routed: list[str]) -> None:
    node_service.write_json(
        root / "manifest.json", {"identity": {"kind": "openai", "served_models_file": MODELS_FILE}}
    )
    (root / MODELS_FILE).write_text(json.dumps(routed))
    node_service.write_json(
        root / "receipt.json",
        {"phase": "running", "pid": 1, "health_url": f"http://127.0.0.1:{port}/health/liveliness"},
    )


def test_readiness_requires_every_configured_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(node_service, "alive", lambda receipt: True)
    with _proxy("sk-clio-m", ["Qwen/Qwen3-4B", "/m/qwen3-1.7b"]) as port:
        _running(tmp_path, port, ["Qwen/Qwen3-4B", "/m/qwen3-1.7b"])
        assert node_service.observation(tmp_path, key="sk-clio-m")["serving"]
        assert not node_service.observation(tmp_path, key="sk-clio-other")["serving"]
        _running(tmp_path, port, ["Qwen/Qwen3-4B", "/m/missing"])
        assert not node_service.observation(tmp_path, key="sk-clio-m")["serving"]


# --------------------------------------------------------------------------- regeneration


class _Router(ModelRouterMixin):
    """The mixin with the runtime's router operation replaced by its observable effect."""

    def __init__(self, store: InfrastructureStore, failing: set[str] | None = None) -> None:
        self.store = store
        self.ran: list[str] = []
        self.failing = failing or set()

    async def _run_operation_locked(
        self, row: InfrastructureOperation, request: ServiceActionRequest
    ) -> None:
        self.ran.append(request.action)
        if request.action in self.failing:
            self.store.put_operation(row.model_copy(update={"state": "failed"}))
            return
        router = self.store.service(request.target_id, ROUTER_SERVICE)
        assert router is not None
        update: dict[str, Any] = {"state": "stopped"}
        if request.action == "start":
            models = routed_models(self.store.services(), request.target_id)
            configuration = {**router.configuration, DIGEST_FIELD: config_digest(models)}
            update = {"state": "running", "configuration": {**configuration, IDLE_FIELD: ""}}
        self.store.update_service(request.target_id, ROUTER_SERVICE, **update)
        self.store.put_operation(row.model_copy(update={"state": "succeeded"}))


def _store_with_router(tmp_path: Path, state: str = "running") -> InfrastructureStore:
    store = InfrastructureStore(tmp_path / "infra.json")
    store.put_service(_instance("vllm@a", "Qwen/Qwen3-4B", 41001))
    digest = config_digest(routed_models(store.services(), "local"))
    store.put_service(_router(state, **{DIGEST_FIELD: digest}))
    return store


def _refresh(runtime: _Router, service_id: str, action: str) -> None:
    asyncio.run(runtime._refresh_router("local", service_id, action))  # noqa: SLF001


def test_a_started_instance_restarts_the_router_with_it(tmp_path: Path) -> None:
    store = _store_with_router(tmp_path)
    runtime = _Router(store)

    _refresh(runtime, "vllm@a", "status")
    _refresh(runtime, "vllm@a", "start")  # the model list did not change
    assert runtime.ran == []

    store.put_service(_instance("vllm@b", "/m/q", 41002))
    _refresh(runtime, "vllm@b", "start")

    assert runtime.ran == ["stop", "start"]
    router = store.service("local", ROUTER_SERVICE)
    assert router is not None and router.state == "running"
    assert [op.service_id for op in store.operations()] == [ROUTER_SERVICE, ROUTER_SERVICE]


def test_a_stopped_or_removed_instance_leaves_the_router(tmp_path: Path) -> None:
    store = _store_with_router(tmp_path)
    store.put_service(_instance("vllm@b", "/m/q", 41002))
    digest = config_digest(routed_models(store.services(), "local"))
    store.update_service(
        "local", ROUTER_SERVICE, configuration={"port": "41500", DIGEST_FIELD: digest}
    )
    runtime = _Router(store)

    store.update_service("local", "vllm@b", state="stopped")
    _refresh(runtime, "vllm@b", "stop")
    assert runtime.ran == ["stop", "start"]

    store.delete_service("local", "vllm@b")
    _refresh(runtime, "vllm@b", "uninstall")
    assert runtime.ran == ["stop", "start"]  # already without vllm@b


def test_with_no_instance_left_the_router_idles_and_comes_back(tmp_path: Path) -> None:
    store = _store_with_router(tmp_path)
    runtime = _Router(store)

    store.update_service("local", "vllm@a", state="stopped")
    _refresh(runtime, "vllm@a", "stop")
    router = store.service("local", ROUTER_SERVICE)
    assert runtime.ran == ["stop"]
    assert router is not None and router.state == "stopped"
    assert router.configuration[IDLE_FIELD] == "true"

    store.update_service("local", "vllm@a", state="running")
    _refresh(runtime, "vllm@a", "start")
    assert runtime.ran == ["stop", "start"]


def test_a_router_the_person_stopped_stays_stopped(tmp_path: Path) -> None:
    store = _store_with_router(tmp_path, state="stopped")
    runtime = _Router(store)
    store.put_service(_instance("vllm@b", "/m/q", 41002))

    _refresh(runtime, "vllm@b", "start")
    _refresh(runtime, "llama_cpp", "start")
    _refresh(runtime, ROUTER_SERVICE, "start")
    assert runtime.ran == []


def test_an_uninstalled_router_is_not_started_even_if_it_idled(tmp_path: Path) -> None:
    store = _store_with_router(tmp_path, state="not_installed")
    store.update_service(
        "local", ROUTER_SERVICE, configuration={"port": "41500", IDLE_FIELD: "true"}
    )
    runtime = _Router(store)

    _refresh(runtime, "vllm@a", "start")

    assert runtime.ran == []


def test_a_failed_stop_does_not_start_a_second_router(tmp_path: Path) -> None:
    store = _store_with_router(tmp_path)
    store.put_service(_instance("vllm@b", "/m/q", 41002))
    runtime = _Router(store, failing={"stop"})

    _refresh(runtime, "vllm@b", "start")

    assert runtime.ran == ["stop"]


# --------------------------------------------------------------------------- binding


def _bind_app(store: InfrastructureStore, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def fake_check(preset: Any, address: str) -> dict[str, Any]:
        return {"reachable": True, "connectivity": "ok", "models": [], "error": ""}

    monkeypatch.setattr(local_servers, "check_address", fake_check)
    app = FastAPI()
    app.state.infrastructure_store = store
    local_servers.register_local_server_routes(
        app,
        [SimpleNamespace(id="vllm", label="vLLM")],  # type: ignore[list-item]
    )
    return TestClient(app)


def test_use_in_models_binds_the_router_as_one_provider(
    tmp_path: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = InfrastructureStore(tmp_path / "infra.json")
    store.put_service(_router())
    store.put_service(_instance("vllm@a", "Qwen/Qwen3-4B", 41001))
    client = _bind_app(store, monkeypatch)

    router = client.post("/v1/providers/servers", json={"address": ROUTER_URL, "preset_id": "vllm"})
    instance = client.post(
        "/v1/providers/servers", json={"address": "http://127.0.0.1:41001", "preset_id": "vllm"}
    )
    custom = client.post("/v1/providers/servers", json={"address": f"{ROUTER_URL}/v1"})

    assert router.status_code == 200, router.text
    assert router.json()["id"] == "server-model-router" and router.json()["custom"] is True
    assert router.json()["credential_ref"] == deployment_key_ref("local", ROUTER_SERVICE)
    assert instance.json()["label"] == "vLLM (a)"
    assert instance.json()["credential_ref"] == deployment_key_ref("local", "vllm@a")
    assert custom.json()["credential_ref"] == deployment_key_ref("local", ROUTER_SERVICE)
    # Neither replaced the engine's own entry (its default deployment's address).
    assert saved.get_server("vllm") is None


def test_uninstalling_the_router_retires_its_saved_server(
    tmp_path: Path, config_file: Path
) -> None:
    store = InfrastructureStore(tmp_path / "infra.json")
    entry = saved.add_server(
        address=f"{ROUTER_URL}/v1",
        label="Model router",
        credential_ref=deployment_key_ref("local", ROUTER_SERVICE),
    )
    other = saved.add_server(address="http://gpu-node:8000/v1", label="Mine")

    removed = retire_saved_servers(store, _router())

    assert removed == [entry.id]
    assert [row.id for row in saved.list_servers()] == [other.id]
    app = SimpleNamespace(state=SimpleNamespace(infrastructure_store=store))
    ref = {"provider_id": entry.id, "model_id": "Qwen/Qwen3-4B"}
    envelope = removed_deployment_error(app, ref, session_id="s", source="session")
    assert envelope is not None and envelope.error.error == "model_deployment_removed"


# --------------------------------------------------------------------------- typed errors


def test_router_target_is_read_from_the_saved_servers_key_ref() -> None:
    assert router_target(deployment_key_ref("ares-2", ROUTER_SERVICE)) == "ares-2"
    assert router_target(deployment_key_ref("ares", "vllm@a")) is None
    assert router_target("argonne:default") is None


def _router_session(tmp_path: Path) -> tuple[SimpleNamespace, InfrastructureStore, str]:
    store = InfrastructureStore(tmp_path / "infra.json")
    store.put_service(_instance("vllm@a", "Qwen/Qwen3-4B", 41001))
    store.put_service(_instance("vllm@b", "/m/q", 41002, state="stopped"))
    models = json.dumps({"Qwen/Qwen3-4B": "vllm@a"})
    store.put_service(_router(**{MODELS_FIELD: models}))
    entry = saved.add_server(
        address=f"{ROUTER_URL}/v1",
        label="Model router",
        credential_ref=deployment_key_ref("local", ROUTER_SERVICE),
    )
    return SimpleNamespace(state=SimpleNamespace(infrastructure_store=store)), store, entry.id


def test_a_model_whose_instance_stopped_is_refused_typed(tmp_path: Path, config_file: Path) -> None:
    app, store, provider = _router_session(tmp_path)
    ref = {"provider_id": provider, "model_id": "/m/q"}

    envelope = router_model_error(app, ref, session_id="s", source="session")

    assert envelope is not None
    assert envelope.error.error == "model_instance_stopped"
    assert envelope.error.recoverable is True
    assert "vllm@b" in envelope.error.message
    assert envelope.error.details["router"]["instance"] == "vllm@b"
    served = {"provider_id": provider, "model_id": "Qwen/Qwen3-4B"}
    assert router_model_error(app, served, session_id="s", source="session") is None
    prefixed = {"provider_id": provider, "model_id": "hosted_vllm/Qwen/Qwen3-4B"}
    assert router_model_error(app, prefixed, session_id="s", source="session") is None


def test_an_unrouted_model_or_a_stopped_router_is_refused_typed(
    tmp_path: Path, config_file: Path
) -> None:
    app, store, provider = _router_session(tmp_path)

    unknown = {"provider_id": provider, "model_id": "gemma-3"}
    envelope = router_model_error(app, unknown, session_id="s", source="session")
    assert envelope is not None and envelope.error.error == "model_not_routed"
    assert envelope.error.details["router"]["routed"] == ["Qwen/Qwen3-4B"]

    store.update_service("local", ROUTER_SERVICE, state="stopped")
    served = {"provider_id": provider, "model_id": "Qwen/Qwen3-4B"}
    envelope = router_model_error(app, served, session_id="s", source="session")
    assert envelope is not None and envelope.error.error == "model_router_stopped"


def test_a_saved_server_that_is_no_router_is_never_refused(
    tmp_path: Path, config_file: Path
) -> None:
    store = InfrastructureStore(tmp_path / "infra.json")
    entry = saved.add_server(
        address="http://127.0.0.1:41001/v1",
        label="vLLM (a)",
        credential_ref=deployment_key_ref("local", "vllm@a"),
    )
    app = SimpleNamespace(state=SimpleNamespace(infrastructure_store=store))
    ref = {"provider_id": entry.id, "model_id": "anything"}

    assert router_model_problem(store, entry.credential_ref, "anything") is None
    assert router_model_error(app, ref, session_id="s", source="session") is None
