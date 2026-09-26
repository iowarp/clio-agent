"""CLIO-owned infrastructure persistence, drivers, runtime, and HTTP contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from clio_agent.gact.app import build_app
from clio_agent.gact.infrastructure.drivers import WEB_SEARCH_IMAGE, build_driver_plan
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CreateTargetRequest,
    ExternalServiceConnectionRequest,
    InfrastructureOperation,
    ServiceActionRequest,
    SshRoute,
    TargetFacts,
)
from clio_agent.gact.infrastructure.probe import _resolve_command
from clio_agent.gact.infrastructure.runtime import InfrastructureRuntime
from clio_agent.gact.infrastructure.store import InfrastructureStore


def test_store_persists_owner_targets_and_marks_interrupted_operations(tmp_path: Path) -> None:
    path = tmp_path / "infrastructure.json"
    store = InfrastructureStore(path)
    target = store.create_target(
        CreateTargetRequest(
            label="Homelab",
            kind="ssh",
            ssh=SshRoute(profile="homelab", jump_hosts=["bastion"]),
        )
    )
    operation = store.put_operation(
        InfrastructureOperation(
            service_id="web_search",
            target_id=target.id,
            action="install",
            state="running",
        )
    )

    restored = InfrastructureStore(path)

    assert restored.target(target.id) is not None
    assert restored.target(target.id).ssh.jump_hosts == ["bastion"]  # type: ignore[union-attr]
    assert restored.target(target.id).transport_state == "state_unknown"  # type: ignore[union-attr]
    interrupted = restored.operation(operation.id)
    assert interrupted is not None
    assert interrupted.state == "failed"
    assert interrupted.error == "operation_interrupted"


def test_store_rejects_duplicate_jump_hosts(tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infrastructure.json")

    with pytest.raises(ValueError, match="duplicates"):
        store.create_target(
            CreateTargetRequest(
                label="Invalid",
                kind="ssh",
                ssh=SshRoute(profile="target", jump_hosts=["bastion", "bastion"]),
            )
        )

    with pytest.raises(ValueError, match="dedicated absolute"):
        store.create_target(
            CreateTargetRequest(
                label="Unsafe root",
                kind="ssh",
                install_root="/mnt/common/../escape",
                ssh=SshRoute(profile="target"),
            )
        )


def test_windows_command_resolution_prefers_real_executable_over_extensionless_shim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("clio_agent.gact.infrastructure.probe.platform.system", lambda: "Windows")
    monkeypatch.setattr(
        "clio_agent.gact.infrastructure.probe.shutil.which",
        lambda name: {
            "docker.exe": r"C:\Program Files\Docker\docker.exe",
            "docker": r"C:\Program Files\Docker\docker",
        }.get(name),
    )

    assert _resolve_command("docker") == r"C:\Program Files\Docker\docker.exe"


def test_web_search_plan_is_pinned_and_allowlisted() -> None:
    facts = TargetFacts(
        target_id="homelab",
        label="Homelab",
        os="linux",
        arch="x86_64",
        docker_available=True,
        docker_installed=True,
        uv_available=True,
        transport_state="connected",
    )

    plan = build_driver_plan(
        service_id="web_search",
        action="install",
        variant_id="container",
        configuration={"contact_email": "alice@example.org", "task_backend_port": "8090"},
        facts=facts,
    )

    assert plan.commands[0].program == "docker"
    assert plan.commands[0].args == ["pull", WEB_SEARCH_IMAGE]
    assert plan.commands[1].program == "docker"
    assert "0.0.0.0:8089:8080" in plan.commands[1].args
    assert plan.connection_port == 8089

    local_plan = build_driver_plan(
        service_id="web_search",
        action="install",
        variant_id="container",
        configuration={},
        facts=facts.model_copy(update={"target_id": "local"}),
    )
    assert "127.0.0.1:8089:8080" in local_plan.commands[1].args

    uninstall_plan = build_driver_plan(
        service_id="web_search",
        action="uninstall",
        variant_id="container",
        configuration={},
        facts=facts,
    )
    assert uninstall_plan.commands[0].settle_seconds == 1.0


def test_install_root_places_container_state_under_the_selected_location() -> None:
    facts = TargetFacts(
        target_id="homelab",
        label="Homelab",
        os="linux",
        arch="x86_64",
        docker_available=True,
        docker_installed=True,
        transport_state="connected",
    )
    target = InfrastructureStore(None).create_target(
        CreateTargetRequest(
            label="Homelab",
            kind="ssh",
            install_root="/srv/clio",
            ssh=SshRoute(profile="homelab"),
        )
    )

    plan = build_driver_plan(
        service_id="web_search",
        action="install",
        variant_id="container",
        configuration={},
        facts=facts,
        target=target,
    )

    assert plan.commands[1].program == "mkdir"
    assert plan.commands[1].args == ["-p", "--", "/srv/clio/services/web-search"]
    assert "/srv/clio/services/web-search:/var/lib/clio-web-search" in plan.commands[2].args


class _FakeTransports:
    def __init__(self) -> None:
        self.forward_calls = 0

    async def execute(self, target_id: str, spec: Any) -> CommandResult:
        del target_id
        if spec.program in {"sh", "powershell"}:
            return CommandResult(exit_code=0, stdout="linux|x86_64|none|1|1|1\n")
        return CommandResult(exit_code=0, stdout="running\n")

    async def forward(
        self, target_id: str, remote_port: int, preferred_local_port: int | None = None
    ) -> str:
        del target_id
        self.forward_calls += 1
        return f"http://127.0.0.1:{preferred_local_port or remote_port + 10000}"


@pytest.mark.asyncio
async def test_runtime_keeps_service_record_in_managing_clio(tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infrastructure.json")
    target = store.create_target(
        CreateTargetRequest(
            label="SSH only",
            kind="ssh",
            ssh=SshRoute(profile="ssh-only"),
        )
    )
    store.set_transport_state(target.id, "connected")
    runtime = InfrastructureRuntime(store, _FakeTransports())  # type: ignore[arg-type]
    request = ServiceActionRequest(
        target_id=target.id,
        action="status",
        variant_id="container",
    )

    operation = runtime.start_action("web_search", request)
    for _ in range(100):
        current = store.operation(operation.id)
        if current is not None and current.state in {"succeeded", "failed"}:
            break
        await asyncio.sleep(0.01)

    current = store.operation(operation.id)
    assert current is not None
    assert current.state == "succeeded"
    service = store.service(target.id, "web_search")
    assert service is not None
    assert service.target_id == target.id
    assert service.state == "running"


@pytest.mark.asyncio
async def test_connection_strategy_prefers_direct_then_falls_back_to_forward(
    tmp_path: Path,
) -> None:
    store = InfrastructureStore(tmp_path / "infrastructure.json")
    target = store.create_target(
        CreateTargetRequest(
            label="Homelab",
            kind="ssh",
            ssh=SshRoute(host="10.0.0.102", user="alice"),
        )
    )
    transports = _FakeTransports()

    async def reachable(_url: str) -> bool:
        return True

    runtime = InfrastructureRuntime(
        store,
        transports,  # type: ignore[arg-type]
        endpoint_reachable=reachable,
    )
    assert await runtime._resolve_connection(target.id, 8089) == (  # noqa: SLF001
        "http://10.0.0.102:8089",
        "direct",
    )
    assert transports.forward_calls == 0

    async def unreachable(_url: str) -> bool:
        return False

    runtime = InfrastructureRuntime(
        store,
        transports,  # type: ignore[arg-type]
        endpoint_reachable=unreachable,
    )
    assert await runtime._resolve_connection(target.id, 8089) == (  # noqa: SLF001
        "http://127.0.0.1:18089",
        "ssh_forward",
    )
    assert transports.forward_calls == 1
    assert await runtime._resolve_connection(  # noqa: SLF001
        target.id,
        8089,
        previous_url="http://127.0.0.1:19000",
        previous_strategy="ssh_forward",
    ) == ("http://127.0.0.1:19000", "ssh_forward")


@pytest.mark.asyncio
async def test_start_waits_for_a_reachable_direct_endpoint_before_forwarding(
    tmp_path: Path,
) -> None:
    store = InfrastructureStore(tmp_path / "infrastructure.json")
    target = store.create_target(
        CreateTargetRequest(
            label="Homelab",
            kind="ssh",
            ssh=SshRoute(host="10.0.0.102", user="alice"),
        )
    )
    transports = _FakeTransports()
    attempts = 0

    async def warming_up(_url: str) -> bool:
        nonlocal attempts
        attempts += 1
        return attempts == 3

    runtime = InfrastructureRuntime(
        store,
        transports,  # type: ignore[arg-type]
        endpoint_reachable=warming_up,
    )

    assert await runtime._resolve_connection(  # noqa: SLF001
        target.id, 8089, wait_for_direct=True
    ) == ("http://10.0.0.102:8089", "direct")
    assert attempts == 3
    assert transports.forward_calls == 0


@pytest.mark.asyncio
async def test_local_start_waits_for_loopback_endpoint(tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infrastructure.json")
    attempts = 0

    async def warming_up(_url: str) -> bool:
        nonlocal attempts
        attempts += 1
        return attempts == 3

    runtime = InfrastructureRuntime(
        store,
        _FakeTransports(),  # type: ignore[arg-type]
        endpoint_reachable=warming_up,
    )

    assert await runtime._resolve_connection(  # noqa: SLF001
        "local", 8089, wait_for_direct=True
    ) == ("http://127.0.0.1:8089", "loopback")
    assert attempts == 3


@pytest.mark.asyncio
async def test_external_https_health_uses_resolved_bearer_without_storing_it(
    tmp_path: Path,
) -> None:
    observed: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json={"status": "ok"})

    store = InfrastructureStore(tmp_path / "infrastructure.json")
    runtime = InfrastructureRuntime(
        store,
        _FakeTransports(),  # type: ignore[arg-type]
        credential_resolver=lambda provider, reference: (
            "secret-token" if (provider, reference) == ("infrastructure", "nsf") else ""
        ),
        http_transport=httpx.MockTransport(answer),
    )

    row = await runtime.create_external_connection(
        ExternalServiceConnectionRequest(
            service_id="web_search",
            label="Public NSF search",
            url="https://search.example.edu",
            credential_ref="nsf",
        )
    )

    assert row.reachable is True
    assert observed[0].url.scheme == "https"
    assert observed[0].headers["authorization"] == "Bearer secret-token"
    assert "secret-token" not in (tmp_path / "infrastructure.json").read_text(encoding="utf-8")


class _SlowTransports(_FakeTransports):
    async def execute(self, target_id: str, spec: Any) -> CommandResult:
        if spec.program == "sh":
            return await super().execute(target_id, spec)
        await asyncio.sleep(60)
        return CommandResult(exit_code=0)


@pytest.mark.asyncio
async def test_cancelled_install_is_not_recorded_or_replayed(tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infrastructure.json")
    target = store.create_target(
        CreateTargetRequest(label="Remote", kind="ssh", ssh=SshRoute(profile="remote"))
    )
    store.set_transport_state(target.id, "connected")
    runtime = InfrastructureRuntime(store, _SlowTransports())  # type: ignore[arg-type]
    operation = runtime.start_action(
        "web_search",
        ServiceActionRequest(
            target_id=target.id,
            action="install",
            variant_id="container",
            configuration={},
        ),
    )
    await asyncio.sleep(0.05)

    cancelled = await runtime.cancel(operation.id)

    assert cancelled.state == "cancelled"
    assert store.service(target.id, "web_search") is None
    restored = InfrastructureStore(tmp_path / "infrastructure.json")
    assert restored.operation(operation.id).state == "cancelled"  # type: ignore[union-attr]
    assert restored.service(target.id, "web_search") is None


def test_http_contract_exposes_context_local_target_and_external_connections(
    tmp_path: Path,
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    app.state.infrastructure_runtime._http_transport = httpx.MockTransport(  # noqa: SLF001
        lambda _request: httpx.Response(200)
    )
    with TestClient(app) as client:
        targets = client.get("/v1/infrastructure/targets")
        assert targets.status_code == 200
        assert targets.json()["targets"][0]["id"] == "local"
        assert targets.json()["targets"][0]["label"] == "This CLIO's computer"

        direct = client.post(
            "/v1/infrastructure/targets",
            json={"label": "Public endpoint", "kind": "direct"},
        )
        assert direct.status_code == 201
        assert (
            client.get(
                "/v1/infrastructure/catalog",
                params={"target_id": direct.json()["id"]},
            ).status_code
            == 409
        )

        created = client.post(
            "/v1/infrastructure/service-connections",
            json={
                "service_id": "web_search",
                "label": "NSF search",
                "url": "https://search.example.org",
            },
        )
        assert created.status_code == 201
        assert created.json()["managed"] is False

        listed = client.get("/v1/infrastructure/service-connections")
        assert listed.status_code == 200
        assert listed.json()["connections"][0]["label"] == "NSF search"

        connection_id = created.json()["id"]
        updated = client.put(
            f"/v1/infrastructure/service-connections/{connection_id}",
            json={
                "service_id": "web_search",
                "label": "Updated NSF search",
                "url": "https://search.example.edu",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["label"] == "Updated NSF search"
        assert (
            client.post(f"/v1/infrastructure/service-connections/{connection_id}/check").status_code
            == 200
        )
        assert (
            client.delete(f"/v1/infrastructure/service-connections/{connection_id}").status_code
            == 204
        )


def test_http_target_validation_keeps_remote_to_remote_out_of_the_contract(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        response = client.post(
            "/v1/infrastructure/targets",
            json={
                "label": "Homelab",
                "kind": "ssh",
                "ssh": {"profile": "homelab", "jump_hosts": ["bastion", "gateway"]},
            },
        )

        assert response.status_code == 201
        assert response.json()["ssh"]["jump_hosts"] == ["bastion", "gateway"]
        target_id = response.json()["id"]
        updated = client.put(
            f"/v1/infrastructure/targets/{target_id}",
            json={
                "label": "Homelab renamed",
                "kind": "ssh",
                "install_root": "/mnt/common/alice/clio",
                "ssh": {"profile": "homelab", "jump_hosts": ["bastion", "gateway"]},
            },
        )
        assert updated.status_code == 200
        assert updated.json()["install_root"] == "/mnt/common/alice/clio"


def test_create_target_accepts_a_null_key_file_as_no_key_configured(tmp_path: Path) -> None:
    """A host with no private key round-trips identity_file as `null` (#1438).

    The desktop's Rust bridge serializes `Option<String>::None` as JSON
    `null`, not an absent field or `""`. The wire contract must accept that
    shape for a host that authenticates with a password or an SSH agent
    instead of a key file, rather than rejecting the whole deploy request
    with a generic 422 before it ever reaches OpenSSH.
    """

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        response = client.post(
            "/v1/infrastructure/targets",
            json={
                "label": "Delta",
                "kind": "ssh",
                "ssh": {
                    "profile": "delta",
                    "host": "delta.example.edu",
                    "user": "alice",
                    "identity_file": None,
                },
            },
        )

        assert response.status_code == 201
        assert response.json()["ssh"]["identity_file"] == ""


def test_create_target_accepts_a_null_install_root_as_the_default_home(tmp_path: Path) -> None:
    """A host with no configured install root round-trips it as `null` too.

    Same Rust `Option<String>` shape as identity_file (#1438): a host left
    at "use the remote user's home directory" sends `install_root: null`,
    which must not fail request validation either.
    """

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        response = client.post(
            "/v1/infrastructure/targets",
            json={
                "label": "Delta",
                "kind": "ssh",
                "install_root": None,
                "ssh": {"host": "delta.example.edu"},
            },
        )

        assert response.status_code == 201
        assert response.json()["install_root"] == ""


def test_transport_attachment_requires_bearer_and_updates_durable_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_AUTH_TOKEN", "bridge-token")
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        created = client.post(
            "/v1/infrastructure/targets",
            json={"label": "Homelab", "kind": "ssh", "ssh": {"profile": "homelab"}},
            headers={"Authorization": "Bearer bridge-token"},
        ).json()
        path = f"/v1/infrastructure/targets/{created['id']}/transport"
        with pytest.raises(WebSocketDisconnect) as refusal:
            with client.websocket_connect(path, subprotocols=["clio-bearer.wrong"]):
                pass
        assert refusal.value.code == 4401

        token = "YnJpZGdlLXRva2Vu"  # urlsafe base64 for bridge-token
        with client.websocket_connect(
            path,
            subprotocols=["clio.infrastructure.v1", f"clio-bearer.{token}"],
        ) as websocket:
            target = app.state.infrastructure_store.target(created["id"])
            assert target.transport_state == "connected"
            # The client (desktop webview) requests "clio.infrastructure.v1";
            # the server must echo it back in the handshake response, or the
            # connection completes with no subprotocol negotiated at all
            # (#1440 — the jump-host/DUO deploy that "cannot enter the Utah
            # cluster" fails here, not in OpenSSH itself).
            assert websocket.accepted_subprotocol == "clio.infrastructure.v1"

        target = app.state.infrastructure_store.target(created["id"])
        assert target.transport_state == "disconnected"
