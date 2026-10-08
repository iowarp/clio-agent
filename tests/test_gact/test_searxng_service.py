"""CLIO's private SearXNG as a managed native service: catalog, plans and lifecycle."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import search_bootstrap
from clio_agent.gact.infrastructure.configuration_keys import (
    UnknownConfigurationKeyError,
    validate_configuration_keys,
)
from clio_agent.gact.infrastructure.drivers import (
    LOOPBACK_ONLY_SERVICES,
    build_driver_plan,
    service_connection_port,
    service_definitions,
)
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    InfrastructureTarget,
    ServiceActionRequest,
    TargetFacts,
)
from clio_agent.gact.infrastructure.node_service import MARKER
from clio_agent.gact.infrastructure.runtime import InfrastructureRuntime
from clio_agent.gact.infrastructure.searxng_service import (
    SEARXNG_COMMIT,
    SEARXNG_REQUIREMENTS,
    SERVICE_ID,
    VARIANT_ID,
    engine_key_variable,
    searxng_plan,
)
from clio_agent.gact.infrastructure.store import InfrastructureStore
from clio_agent.search.backend import register_local_endpoint_resolver, resolve_search_backend
from clio_agent.search.settings import DEFAULT_ENGINES, build_settings, is_opt_in_only


def facts(**values: Any) -> TargetFacts:
    return TargetFacts.model_validate(
        {
            "target_id": "local",
            "label": "This computer",
            "os": "linux",
            "arch": "x86_64",
            "uv_available": True,
            "hostname": "node",
            "agent_data_root": "/data/clio",
            **values,
        }
    )


def plan(action: str = "install", **configuration: str) -> Any:
    return build_driver_plan(
        service_id=SERVICE_ID,
        action=action,
        variant_id=VARIANT_ID,
        configuration=configuration,
        facts=facts(),
        target=InfrastructureTarget(id="local", label="This computer", kind="local"),
    )


def body(command: CommandSpec) -> dict[str, Any]:
    return json.loads(command.stdin or "{}")


def test_the_catalog_offers_a_native_variant_gated_by_host_facts() -> None:
    row = next(item for item in service_definitions(facts()) if item.id == SERVICE_ID)
    variant = row.variants[0]
    assert (row.recommended_variant, variant.id, variant.install_type) == (
        VARIANT_ID,
        "native",
        "native_uv",
    )
    assert variant.compatible and SEARXNG_COMMIT in variant.artifact
    mac = next(item for item in service_definitions(facts(os="macos")) if item.id == SERVICE_ID)
    assert mac.variants[0].compatible and "not live-tested" in mac.variants[0].reason
    windows = next(
        item for item in service_definitions(facts(os="windows")) if item.id == SERVICE_ID
    )
    assert not windows.variants[0].compatible
    assert "not live-tested" in windows.variants[0].reason
    assert "clio_web_search" in windows.variants[0].reason
    no_uv = next(
        item for item in service_definitions(facts(uv_available=False)) if item.id == SERVICE_ID
    )
    assert not no_uv.variants[0].compatible and "uv" in no_uv.variants[0].reason
    assert SERVICE_ID in LOOPBACK_ONLY_SERVICES


def test_windows_lifecycle_is_refused_with_the_reason() -> None:
    with pytest.raises(ValueError, match="POSIX-only"):
        searxng_plan("install", {}, facts(os="windows"), None)


def test_install_builds_searxng_in_its_own_pinned_environment() -> None:
    install = plan()
    assert install.readiness is not None and install.readiness.capability == "installed"
    assert [body(command)["action"] for command in install.commands] == ["prepare", "install"]
    manifest = body(install.commands[-1])["manifest"]
    assert manifest["searxng"]["commit"] == SEARXNG_COMMIT
    assert SEARXNG_COMMIT in manifest["searxng"]["source_url"]
    for requirement in SEARXNG_REQUIREMENTS:
        assert json.dumps(requirement) in manifest["project"]
    assert "granian==2.8.0" in manifest["project"]
    assert "clio-agent" not in manifest["project"] and "uwsgi" not in manifest["project"]
    assert manifest["post_install"] == "searxng_hook.py"
    assert manifest["identity"] == {"kind": "components", "hook": "searxng_hook.py"}
    assert manifest["health_path"] == "/healthz"
    assert {"searxng_hook.py", "verify.py", "clio_pwd_shim.py"} <= set(manifest["files"])
    assert "Granian" in manifest["launcher"]
    assert install.configuration["storage.service_directory"] == "/data/clio/services/node/searxng"


def test_generated_settings_are_private_loopback_json_and_secret_free() -> None:
    manifest = body(plan().commands[-1])["manifest"]
    settings = manifest["searxng"]["settings"]
    server = settings["server"]
    assert server["bind_address"] == "127.0.0.1"
    assert server["limiter"] is False and server["public_instance"] is False
    assert "secret_key" not in server
    assert "json" in settings["search"]["formats"]
    assert settings["valkey"] == {"url": False}
    assert settings["search"]["safe_search"] == 1
    assert settings["use_default_settings"]["engines"]["keep_only"] == list(DEFAULT_ENGINES)
    assert {row["name"] for row in settings["engines"]} == set(DEFAULT_ENGINES)
    assert not any(is_opt_in_only(row["name"]) for row in settings["engines"])
    assert "secret" not in json.dumps(settings).casefold()


def test_a_chinese_engine_in_the_form_needs_an_explicit_opt_in() -> None:
    plain = body(plan(engines="wikipedia,baidu").commands[-1])["manifest"]
    assert plain["searxng"]["settings"]["use_default_settings"]["engines"]["keep_only"] == [
        "wikipedia"
    ]
    opted = body(plan(engines="wikipedia,baidu", opt_in_engines="baidu").commands[-1])
    keep = opted["manifest"]["searxng"]["settings"]["use_default_settings"]["engines"]
    assert keep["keep_only"] == ["wikipedia", "baidu"]


def test_start_reuses_the_installed_manifest_exactly() -> None:
    install = plan()
    start = plan("start", **install.configuration)
    assert body(start.commands[0])["manifest"] == body(install.commands[-1])["manifest"]
    assert start.readiness is not None and start.readiness.capability == "serving"
    assert service_connection_port(SERVICE_ID, install.configuration) == 18890


def test_engine_api_keys_travel_only_in_the_start_environment() -> None:
    target = InfrastructureTarget(id="local", label="This computer", kind="local")
    base = build_settings()
    configuration = {"engines": "wikipedia,braveapi"}
    with pytest.raises(ValueError, match="needs an API key"):
        searxng_plan(
            "start", configuration, facts(), target, base_settings=base, engine_keys=lambda _e: ""
        )
    start = searxng_plan(
        "start",
        configuration,
        facts(),
        target,
        base_settings=base,
        engine_keys=lambda _e: "brave-key-123",
    )
    command = start.commands[0]
    payload = body(command)
    variable = engine_key_variable("braveapi")
    assert payload["secret_env"] == {variable: "brave-key-123"}
    assert "brave-key-123" not in json.dumps(payload["manifest"])
    assert all("brave-key-123" not in argument for argument in command.args)
    assert payload["manifest"]["searxng"]["engine_keys"] == {"braveapi": variable}
    install = searxng_plan("install", configuration, facts(), target, base_settings=base)
    assert "secret_env" not in body(install.commands[-1])


def test_lifecycle_actions_and_keys_are_allowlisted() -> None:
    assert body(plan("verify").commands[0])["action"] == "verify"
    assert body(plan("delete_data").commands[0])["action"] == "delete_data"
    assert body(plan("uninstall").commands[0])["action"] == "uninstall"
    validate_configuration_keys(SERVICE_ID, {"engines": "wikipedia", "safe_search": "2"})
    with pytest.raises(UnknownConfigurationKeyError):
        validate_configuration_keys(SERVICE_ID, {"secret_key": "x"})
    with pytest.raises(ValueError, match="port"):
        plan(port="80")


class FakeHost:
    """The native supervisor's observable behaviour, without running anything."""

    def __init__(self) -> None:
        self.installed = False
        self.running = False
        self.actions: list[str] = []

    def observation(self) -> dict[str, Any]:
        return {
            "phase": "running"
            if self.running
            else "stopped"
            if self.installed
            else "not_installed",
            "installed": self.installed,
            "running": self.running,
            "serving": self.running,
            "worker_alive": self.running,
        }

    async def execute(self, spec: CommandSpec) -> CommandResult:
        if spec.program != "python3":
            return CommandResult(exit_code=0, stdout="")
        action = body(spec)["action"]
        self.actions.append(action)
        if action == "install":
            self.installed = True
        elif action == "start":
            self.running = True
        elif action == "stop":
            self.running = False
        elif action == "uninstall":
            self.installed = self.running = False
        elif action == "logs":
            return CommandResult(exit_code=0, stdout="server.log\nok")
        return CommandResult(exit_code=0, stdout=MARKER + json.dumps(self.observation()))


def make_runtime(store: InfrastructureStore, host: FakeHost) -> InfrastructureRuntime:
    async def reachable(_url: str) -> bool:
        return True

    return InfrastructureRuntime(
        store,
        None,  # type: ignore[arg-type] - the local target never uses a transport
        local_executor=host.execute,
        endpoint_reachable=reachable,
    )


async def finish(runtime: InfrastructureRuntime, action: str) -> str:
    row = runtime.start_action(
        SERVICE_ID,
        ServiceActionRequest(target_id="local", action=action, variant_id=VARIANT_ID),
    )
    return await search_bootstrap._finish(runtime, row.id, 0.01)  # noqa: SLF001


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeHost:
    async def probe(target: Any, execute: Any = None) -> TargetFacts:
        del target, execute
        return facts(agent_data_root=str(tmp_path / "clio"))

    monkeypatch.setattr("clio_agent.gact.infrastructure.runtime.probe_target", probe)
    return FakeHost()


@pytest.mark.asyncio
async def test_lifecycle_with_a_fake_executor(host: FakeHost, tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infrastructure.json")
    runtime = make_runtime(store, host)
    assert await finish(runtime, "install") == "succeeded"
    record = store.service("local", SERVICE_ID)
    assert record is not None and record.state == "stopped"
    assert record.configuration["port"] == "18890"
    assert any(item.kind == "directory" for item in record.owned_resources)
    assert await finish(runtime, "start") == "succeeded"
    record = store.service("local", SERVICE_ID)
    assert record is not None and record.state == "running"
    assert record.connection_url == "http://127.0.0.1:18890"
    for action in ("status", "logs", "verify", "stop", "uninstall"):
        assert await finish(runtime, action) == "succeeded", action
    record = store.service("local", SERVICE_ID)
    assert record is not None and record.state == "not_installed"
    assert await finish(runtime, "delete_data") == "succeeded"
    assert store.service("local", SERVICE_ID) is None
    assert host.actions[:2] == ["prepare", "install"]


@pytest.mark.asyncio
async def test_first_run_installs_and_starts_then_respects_a_stop(
    host: FakeHost, tmp_path: Path
) -> None:
    store = InfrastructureStore(tmp_path / "infrastructure.json")
    runtime = make_runtime(store, host)
    settings = build_settings()
    assert await search_bootstrap.ensure_local_searxng(runtime, settings, poll_seconds=0.01) == (
        "started"
    )
    assert host.installed and host.running
    assert await search_bootstrap.ensure_local_searxng(runtime, settings, poll_seconds=0.01) == (
        "running"
    )
    assert await finish(runtime, "stop") == "succeeded"
    assert await search_bootstrap.ensure_local_searxng(runtime, settings, poll_seconds=0.01) == (
        "declined"
    )
    assert not host.running
    other = build_settings(backend="none")
    assert await search_bootstrap.ensure_local_searxng(runtime, other) == "not_selected"
    off = build_settings(auto_install=False)
    assert await search_bootstrap.ensure_local_searxng(runtime, off) == "auto_install_off"


@pytest.mark.asyncio
async def test_the_recorded_instance_is_what_web_search_uses(
    host: FakeHost, tmp_path: Path
) -> None:
    store = InfrastructureStore(tmp_path / "infrastructure.json")
    runtime = make_runtime(store, host)
    app = SimpleNamespace(state=SimpleNamespace(infrastructure_store=store))
    search_bootstrap.register_local_endpoint(app)
    try:
        backend = resolve_search_backend(build_settings(port=19999))
        assert backend.base_url() == "http://127.0.0.1:19999"  # type: ignore[attr-defined]
        await search_bootstrap.ensure_local_searxng(runtime, build_settings(), poll_seconds=0.01)
        assert backend.base_url() == "http://127.0.0.1:18890"  # type: ignore[attr-defined]
    finally:
        register_local_endpoint_resolver(None)
    await asyncio.sleep(0)
