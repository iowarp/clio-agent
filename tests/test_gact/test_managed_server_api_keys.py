"""Managed model servers are protected by a key CLIO generates, stores and sends.

vLLM and llama.cpp get a per-deployment API key by default. The key reaches the
server through the environment, read from the command's stdin: it is never a
command-line argument (other users of a shared login node can read every
process's arguments) and never lands in an operation log or the
infrastructure store. Ollama has no key support and says so. A deployment made
shareable runs with no key and says who can use it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from clio_agent.gact.infrastructure.drivers import build_driver_plan, service_definitions
from clio_agent.gact.infrastructure.models import (
    CommandSpec,
    ContainerRuntimeFact,
    CreateTargetRequest,
    ServiceActionRequest,
    SshRoute,
    TargetFacts,
    TargetIdentity,
)
from clio_agent.gact.infrastructure.runtime import InfrastructureRuntime
from clio_agent.gact.infrastructure.server_access import (
    SHAREABLE_FIELD,
    deployment_key_ref,
    managed_credential_ref,
)
from clio_agent.gact.infrastructure.store import InfrastructureStore
from clio_agent.providers.api_key_store import ProviderApiKeyStore, stored_api_key
from tests.test_gact.test_managed_service_cleanup import FakeLinuxTarget, _finish

HF_MODEL = "Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M"
SECRET_READ = "read -r clio_secret"


@pytest.fixture(autouse=True)
def _isolated_user_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_USER_DIR", str(tmp_path / "user"))


def _facts(runtime: str = "docker", os: str = "linux") -> TargetFacts:
    runtimes = [
        ContainerRuntimeFact(
            name=name,  # type: ignore[arg-type]
            installed=name == runtime,
            usable=name == runtime,
            reason=None if name == runtime else "not_installed",
        )
        for name in ("docker", "podman", "apptainer")
    ]
    return TargetFacts(
        target_id="ares",
        label="ares",
        os=os,
        arch="x86_64",
        docker_installed=runtime == "docker",
        docker_available=runtime == "docker",
        transport_state="connected",
        container_runtimes=runtimes,
        identity=TargetIdentity(uid=1008, gid=65534),
        home="/home/alice" if os == "linux" else "C:\\Users\\alice",
    )


def _plan(service: str, *, api_key: str | None, runtime: str = "docker", os: str = "linux"):
    configuration = (
        {"hf_model": HF_MODEL}
        if service == "llama_cpp"
        else {"model": "Qwen/Qwen2.5-0.5B-Instruct"}
        if service == "vllm"
        else {"model": "qwen2.5:0.5b"}
    )
    return build_driver_plan(
        service_id=service,
        action="install",
        variant_id="cpu",
        configuration=configuration,
        facts=_facts(runtime, os),
        api_key=api_key,
    )


def _launch(commands: tuple[CommandSpec, ...]) -> CommandSpec:
    return next(
        spec
        for spec in commands
        if "run" in spec.args
        and any(p in spec.args for p in ("docker", "podman", "apptainer"))
        or spec.args[:1] == ["run"]
        or spec.args[:2] == ["instance", "run"]
        or (spec.program == "powershell" and "'run'" in spec.args[-1])
    )


# --------------------------------------------------------------------------- the launch


@pytest.mark.parametrize(
    ("service", "variable", "runtime"),
    [
        ("llama_cpp", "LLAMA_API_KEY", "docker"),
        ("llama_cpp", "LLAMA_API_KEY", "podman"),
        ("vllm", "VLLM_API_KEY", "docker"),
        ("vllm", "VLLM_API_KEY", "podman"),
    ],
)
def test_the_key_reaches_the_server_by_stdin_never_the_command_line(
    service: str, variable: str, runtime: str
) -> None:
    plan = _plan(service, api_key="k-123", runtime=runtime)

    launch = _launch(plan.commands)
    assert launch.program == "sh" and launch.args[0] == "-c"
    assert SECRET_READ in launch.args[1]
    assert f'export {variable}="$clio_secret"' in launch.args[1]
    assert launch.args[2:5] == ["sh", runtime, "run"]
    # The container takes the variable by NAME, its value from the environment.
    env_names = [launch.args[i + 1] for i, a in enumerate(launch.args) if a == "--env"]
    assert variable in env_names
    assert launch.stdin == "k-123\n"
    for spec in plan.commands:
        assert all("k-123" not in arg for arg in [spec.program, *spec.args])


def test_apptainer_gets_the_key_through_its_own_environment_prefix() -> None:
    plan = _plan("llama_cpp", api_key="k-123", runtime="apptainer")

    launch = _launch(plan.commands)
    assert 'export APPTAINERENV_LLAMA_API_KEY="$clio_secret"' in launch.args[1]
    assert launch.args[2:6] == ["sh", "apptainer", "instance", "run"]
    assert launch.stdin == "k-123\n"
    assert all("k-123" not in arg for arg in launch.args)


def test_a_windows_docker_launch_reads_the_key_in_powershell() -> None:
    plan = _plan("llama_cpp", api_key="k-123", os="windows")

    launch = _launch(plan.commands)
    assert launch.program == "powershell"
    assert "$env:LLAMA_API_KEY = [Console]::In.ReadLine()" in launch.args[-1]
    assert launch.stdin == "k-123\n"
    assert "k-123" not in launch.args[-1]


def test_a_shareable_deployment_launches_with_no_key() -> None:
    plan = _plan("llama_cpp", api_key=None)

    launch = _launch(plan.commands)
    assert launch.program == "docker"
    assert "LLAMA_API_KEY" not in " ".join(launch.args)
    assert launch.stdin == ""


def test_ollama_has_no_key_support_and_refuses_one() -> None:
    with pytest.raises(ValueError, match="no API key support"):
        _plan("ollama", api_key="k-123")


def test_the_catalog_says_which_engines_take_a_key() -> None:
    rows = {row.id: row for row in service_definitions(_facts())}

    assert rows["vllm"].supports_api_key is True
    assert rows["llama_cpp"].supports_api_key is True
    assert rows["ollama"].supports_api_key is False


# --------------------------------------------------------------------------- the runtime


class KeyedTarget(FakeLinuxTarget):
    """The fake Linux host (which runs the stdin secret wrapper the way ``sh`` does)."""

    def key_of(self, name: str) -> str:
        return self.environments.get(name, {}).get("LLAMA_API_KEY", "")


class LlamaServer:
    """What the forwarded llama.cpp answers: /props and chat, refusing a missing key."""

    def __init__(self, target: KeyedTarget, *, enforces: bool = True) -> None:
        self.target = target
        self.enforces = enforces
        self.seen: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        key = self.target.key_of("clio-llama-cpp")
        sent = request.headers.get("authorization", "")
        self.seen.append((request.url.path, sent))
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if self.enforces and key and sent != f"Bearer {key}":
            return httpx.Response(401, json={"error": {"message": "Invalid API Key"}})
        if request.url.path == "/props":
            return httpx.Response(
                200, json={"total_slots": 2, "default_generation_settings": {"n_ctx": 4096}}
            )
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(400, json={"error": {"message": "messages is required"}})
        return httpx.Response(404)


def _runtime(
    tmp_path: Path, target: KeyedTarget, server: Any
) -> tuple[InfrastructureRuntime, InfrastructureStore, str]:
    store = InfrastructureStore(tmp_path / "infrastructure.json")
    row = store.create_target(
        CreateTargetRequest(label="ares", kind="ssh", ssh=SshRoute(host="ares", user="alice"))
    )
    store.set_transport_state(row.id, "connected")

    async def unreachable(_url: str) -> bool:
        return False

    runtime = InfrastructureRuntime(
        store,
        target,  # type: ignore[arg-type]
        endpoint_reachable=unreachable,
        http_transport=httpx.MockTransport(server),
    )
    return runtime, store, row.id


def _llama(target_id: str, action: str = "install", **extra: str) -> ServiceActionRequest:
    return ServiceActionRequest(
        target_id=target_id,
        action=action,  # type: ignore[arg-type]
        variant_id="cpu",
        configuration={"hf_model": HF_MODEL, **extra},
    )


@pytest.mark.asyncio
async def test_install_generates_stores_and_sends_a_key_and_checks_it_is_enforced(
    tmp_path: Path,
) -> None:
    target = KeyedTarget()
    server = LlamaServer(target)
    runtime, store, target_id = _runtime(tmp_path, target, server)

    done = await _finish(runtime, store, "llama_cpp", _llama(target_id))

    assert done.state == "succeeded", done.error
    key = target.key_of("clio-llama-cpp")
    assert len(key) >= 32
    ref = deployment_key_ref(target_id, "llama_cpp")
    assert ProviderApiKeyStore().load(ref) == key
    record = store.service(target_id, "llama_cpp")
    assert record is not None and record.access is not None
    assert record.access.mode == "api_key"
    assert record.access.verified is True
    # CLIO's own reads of the server carry the key: /props answered.
    assert ("/props", f"Bearer {key}") in server.seen
    assert {row.id: row.value for row in record.effective_parameters}["parallel"] == "2"
    # The key is nowhere but the secret store.
    assert key not in (tmp_path / "infrastructure.json").read_text(encoding="utf-8")
    assert key not in (done.logs + (done.error or "") + done.progress)


@pytest.mark.asyncio
async def test_reinstall_rotates_the_key_and_uninstall_removes_it(tmp_path: Path) -> None:
    target = KeyedTarget()
    runtime, store, target_id = _runtime(tmp_path, target, LlamaServer(target))
    ref = deployment_key_ref(target_id, "llama_cpp")

    await _finish(runtime, store, "llama_cpp", _llama(target_id))
    first = ProviderApiKeyStore().load(ref)
    reinstalled = await _finish(runtime, store, "llama_cpp", _llama(target_id, "reinstall"))
    second = ProviderApiKeyStore().load(ref)

    assert reinstalled.state == "succeeded", reinstalled.error
    assert second and second != first
    assert target.key_of("clio-llama-cpp") == second

    removed = await _finish(runtime, store, "llama_cpp", _llama(target_id, "uninstall"))
    assert removed.state == "succeeded", removed.error
    assert ProviderApiKeyStore().load(ref) == ""


@pytest.mark.asyncio
async def test_a_shareable_deployment_has_no_key_and_says_who_can_use_it(tmp_path: Path) -> None:
    target = KeyedTarget()
    runtime, store, target_id = _runtime(tmp_path, target, LlamaServer(target))

    done = await _finish(
        runtime, store, "llama_cpp", _llama(target_id, **{SHAREABLE_FIELD: "true"})
    )

    assert done.state == "succeeded", done.error
    assert target.key_of("clio-llama-cpp") == ""
    assert ProviderApiKeyStore().load(deployment_key_ref(target_id, "llama_cpp")) == ""
    record = store.service(target_id, "llama_cpp")
    assert record is not None and record.access is not None
    assert record.access.mode == "shared"
    assert "anyone who can reach port 8088" in record.access.detail.casefold()


@pytest.mark.asyncio
async def test_a_server_that_does_not_refuse_a_missing_key_is_never_shown_as_protected(
    tmp_path: Path,
) -> None:
    target = KeyedTarget()
    runtime, store, target_id = _runtime(tmp_path, target, LlamaServer(target, enforces=False))

    done = await _finish(runtime, store, "llama_cpp", _llama(target_id))

    assert done.state == "succeeded", done.error
    record = store.service(target_id, "llama_cpp")
    assert record is not None and record.access is not None
    assert record.access.mode == "unprotected"
    assert "accepted a request without its key" in record.access.detail


@pytest.mark.asyncio
async def test_a_failed_install_removes_the_key_it_made(tmp_path: Path) -> None:
    target = KeyedTarget()
    target.fail_run = True
    runtime, store, target_id = _runtime(tmp_path, target, LlamaServer(target))

    done = await _finish(runtime, store, "llama_cpp", _llama(target_id))

    assert done.state == "failed"
    assert ProviderApiKeyStore().load(deployment_key_ref(target_id, "llama_cpp")) == ""


@pytest.mark.asyncio
async def test_ollama_is_shown_as_not_protected_with_the_reason(tmp_path: Path) -> None:
    target = KeyedTarget()

    def ollama(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(404)

    runtime, store, target_id = _runtime(tmp_path, target, ollama)

    done = await _finish(
        runtime,
        store,
        "ollama",
        ServiceActionRequest(
            target_id=target_id,
            action="install",
            variant_id="cpu",
            configuration={"model": "qwen2.5:0.5b"},
        ),
    )

    assert done.state == "succeeded", done.error
    record = store.service(target_id, "ollama")
    assert record is not None and record.access is not None
    assert record.access.mode == "unprotected"
    assert "no API key support" in record.access.detail
    assert "anyone who can reach port 11434" in record.access.detail


# --------------------------------------------------------------------------- the provider side


def test_a_saved_server_linked_to_a_deployment_resolves_that_deployments_key(
    tmp_path: Path,
) -> None:
    from clio_agent.gact import local_server_store

    ref = deployment_key_ref("t1", "llama_cpp")
    ProviderApiKeyStore().save(ref, "deployment-key")

    local_server_store.add_server(
        address="http://127.0.0.1:28088", preset_id="llama_cpp", credential_ref=ref
    )

    assert local_server_store.get_server("llama_cpp").credential_ref == ref  # type: ignore[union-attr]
    assert stored_api_key("llama_cpp") == "deployment-key"
    # A key the person saved for the provider itself still wins.
    ProviderApiKeyStore().save("llama_cpp", "own-key")
    assert stored_api_key("llama_cpp") == "own-key"


@pytest.mark.asyncio
async def test_saving_a_managed_deployment_as_a_server_links_its_key(tmp_path: Path) -> None:
    target = KeyedTarget()
    runtime, store, target_id = _runtime(tmp_path, target, LlamaServer(target))
    await _finish(runtime, store, "llama_cpp", _llama(target_id))
    record = store.service(target_id, "llama_cpp")
    assert record is not None and record.connection_url

    ref = managed_credential_ref(store, "llama_cpp", record.connection_url + "/v1")

    assert ref == deployment_key_ref(target_id, "llama_cpp")
    # Another address, or another preset, is never handed this key.
    assert managed_credential_ref(store, "llama_cpp", "http://10.0.0.9:8088/v1") == ""
    assert managed_credential_ref(store, "vllm", record.connection_url) == ""


@pytest.mark.asyncio
async def test_the_handshake_sends_the_key_on_every_request_it_makes() -> None:
    from clio_agent.providers.catalog import get_provider
    from clio_agent.providers.handshake import get_handshake_for
    from clio_agent.providers.handshake.base import HandshakeContext

    provider = get_provider("vllm")
    assert provider is not None
    handshake = get_handshake_for(provider.provider_kind, provider)
    ctx = HandshakeContext(
        provider_id="vllm",
        provider_kind=provider.provider_kind,
        api_base="http://127.0.0.1:8000/v1",
        api_key="deployment-key",
    )

    client = await handshake._open_client(ctx)
    try:
        assert client.headers.get("authorization") == "Bearer deployment-key"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_a_default_bind_without_a_key_uses_the_saved_deployments_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F011b: the default model bind must not stamp a placeholder over the deployment key."""
    from types import SimpleNamespace

    from clio_agent.gact import local_server_store
    from clio_agent.gact.providers.bind_configuration import prepare_bind_configuration
    from clio_agent.gact.types import LMProviderRequest
    from clio_agent.providers import handshake

    ref = deployment_key_ref("t1", "vllm")
    ProviderApiKeyStore().save(ref, "deployment-key")
    local_server_store.add_server(
        address="http://127.0.0.1:37153/v1", preset_id="vllm", credential_ref=ref
    )
    seen: list[str] = []

    async def fake_handshake(ctx: Any, **_: Any) -> None:
        seen.append(ctx.api_key)

    monkeypatch.setattr(handshake, "run_handshake", fake_handshake)
    app = SimpleNamespace(state=SimpleNamespace())
    req = LMProviderRequest(
        provider="openai",
        provider_id="vllm",
        api_base="http://127.0.0.1:37153/v1",
        model="qwen",
    )

    def ready() -> tuple[str, str, bool, str]:
        return "ready", "", True, ""

    cfg, _ = await prepare_bind_configuration(app, req, ready, ready)  # type: ignore[arg-type]

    assert cfg.api_key == "deployment-key"
    assert seen == ["deployment-key"]
