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
    ServiceObservation,
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
from clio_agent.search import backend as search_backend
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
    assert windows.variants[0].compatible
    assert "no container" in windows.variants[0].reason
    no_uv = next(
        item for item in service_definitions(facts(uv_available=False)) if item.id == SERVICE_ID
    )
    assert not no_uv.variants[0].compatible and "uv" in no_uv.variants[0].reason
    assert SERVICE_ID in LOOPBACK_ONLY_SERVICES


def test_windows_lifecycle_uses_native_paths_and_stdin_instead_of_a_long_command_line() -> None:
    windows = facts(os="windows", agent_data_root="C:\\Users\\test\\clio-data")
    compiled = searxng_plan("install", {}, windows, None)
    assert (
        compiled.configuration["storage.service_directory"]
        == "C:\\Users\\test\\clio-data\\services\\node\\searxng"
    )
    for command in compiled.commands:
        assert len(" ".join(command.args)) < 2000
        assert body(command)["platform_helper"]
        assert "def control(" in body(command)["script"]


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
        self.fail_next = ""

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
        if action == self.fail_next:
            self.fail_next = ""
            return CommandResult(exit_code=1, stdout="", stderr=f"{action} refused")
        if action == "install":
            self.installed = True
        elif action == "start":
            if not self.installed:  # node_service refuses a start without its installation
                return CommandResult(
                    exit_code=1,
                    stdout="",
                    stderr="Install this exact native service configuration before starting it",
                )
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
        # This fake host is Linux even when the controller's pytest runs on Windows.
        return facts(agent_data_root="/data/clio")

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
async def test_a_failed_install_is_not_started_on_every_boot(
    host: FakeHost, tmp_path: Path
) -> None:
    store = InfrastructureStore(tmp_path / "infrastructure.json")
    runtime = make_runtime(store, host)
    app = SimpleNamespace(
        state=SimpleNamespace(infrastructure_store=store, infrastructure_runtime=runtime)
    )
    search_bootstrap.register_local_endpoint(app)
    host.fail_next = "install"
    settings = build_settings()
    try:
        # First run: the install fails, and the record it left says so.
        assert (
            await search_bootstrap.ensure_local_searxng(runtime, settings, poll_seconds=0.01)
            == "install_failed"
        )
        # Every later boot: no start that can only fail "install first".
        for _boot in range(3):
            assert (
                await search_bootstrap.ensure_local_searxng(runtime, settings, poll_seconds=0.01)
                == "not_installed"
            )
        assert [row.action for row in store.operations() if row.action == "start"] == []
        record = store.service("local", SERVICE_ID)
        assert record is not None and record.state == "not_installed"
        assert record.auto_start_skipped is not None
        assert record.auto_start_skipped.reason == "not_installed"
        assert search_backend._local_phase() == "not_installed"  # noqa: SLF001
        # Installing from Infrastructure clears the note; boot then starts it.
        assert await finish(runtime, "install") == "succeeded"
        record = store.service("local", SERVICE_ID)
        assert record is not None and record.auto_start_skipped is None
        assert (
            await search_bootstrap.ensure_local_searxng(runtime, settings, poll_seconds=0.01)
            == "started"
        )
    finally:
        register_local_endpoint_resolver(None)
    await asyncio.sleep(0)


def test_an_observation_without_an_installation_is_not_installed() -> None:
    # What a failed native install leaves: the supervisor wrote "stopped" over
    # "installing", but nothing is installed.
    failed = ServiceObservation(phase="stopped", installed=False, error="install exited 1")
    assert failed.service_state == "not_installed"
    assert ServiceObservation(phase="installing").service_state == "unknown"
    assert ServiceObservation(phase="stopped", installed=True).service_state == "stopped"


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


@pytest.mark.asyncio
async def test_the_lifecycle_phase_follows_operations_and_the_record(
    host: FakeHost, tmp_path: Path
) -> None:
    store = InfrastructureStore(tmp_path / "infrastructure.json")
    runtime = make_runtime(store, host)
    app = SimpleNamespace(
        state=SimpleNamespace(infrastructure_store=store, infrastructure_runtime=runtime)
    )
    search_bootstrap.register_local_endpoint(app)
    phase = search_backend._local_phase  # noqa: SLF001
    try:
        assert phase() is None
        row = runtime.start_action(
            SERVICE_ID,
            ServiceActionRequest(target_id="local", action="install", variant_id=VARIANT_ID),
        )
        assert phase() == "starting"
        assert await search_bootstrap._finish(runtime, row.id, 0.01) == "succeeded"  # noqa: SLF001
        assert phase() == "stopped"
        assert await finish(runtime, "start") == "succeeded"
        assert phase() is None
        assert await finish(runtime, "stop") == "succeeded"
        assert phase() == "stopped"
        host.fail_next = "start"
        assert await finish(runtime, "start") == "failed"
        assert phase() is None
    finally:
        register_local_endpoint_resolver(None)
    await asyncio.sleep(0)
