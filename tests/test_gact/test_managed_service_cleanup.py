"""Cleanup tracking: a managed model server leaves nothing behind on its target.

A fake SSH target keeps real state (images, containers, directories) and
answers CLIO's commands the way a Linux host does, so every assertion is about
the target after the operation -- not about which commands were sent.
"""

from __future__ import annotations

import asyncio
import json
import posixpath
from pathlib import Path
from typing import Any

import httpx
import pytest

from clio_agent.gact.infrastructure.model_runtimes import OLLAMA_VERSION
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    CreateTargetRequest,
    ServiceActionRequest,
    SshRoute,
)
from clio_agent.gact.infrastructure.runtime import InfrastructureRuntime
from clio_agent.gact.infrastructure.store import InfrastructureStore

IMAGE = f"ollama/ollama:{OLLAMA_VERSION}"
HOME = "/home/alice"
PROBE = (
    "Linux|x86_64|none|1|1|0\n"
    "rt|docker|1|1|29.1.3|\n"
    "rt|podman|0|0||\n"
    "rt|apptainer|0|0||\n"
    f"id|1008|65534|{HOME}\n"
)
SERVER_CONFIG = (
    'time=2026-09-27T06:00:00Z level=INFO source=routes.go:1 msg="server config" '
    'env="map[HTTPS_PROXY: OLLAMA_CONTEXT_LENGTH:4096 OLLAMA_HOST:http://0.0.0.0:11434 '
    'OLLAMA_NUM_PARALLEL:2 OLLAMA_MODELS:/cache/models]"\n'
)


class FakeLinuxTarget:
    """Just enough of a Linux host running Docker to exercise the ledger."""

    def __init__(self, *, images: set[str] | None = None, dirs: set[str] | None = None) -> None:
        self.images = set(images or ())
        self.containers: dict[str, dict[str, Any]] = {}
        self.dirs = {"/", "/home", HOME} | set(dirs or ())
        self.fail_run = False
        self.exit_on_start = False
        self.daemon_can_remove_images = True
        self.commands: list[CommandSpec] = []

    def snapshot(self) -> tuple[set[str], set[str], set[str]]:
        return set(self.images), set(self.containers), set(self.dirs)

    async def execute(self, target_id: str, spec: CommandSpec) -> CommandResult:
        del target_id
        self.commands.append(spec)
        program, args = spec.program, spec.args
        if program == "sh" and args[0] == "-lc":
            return CommandResult(exit_code=0, stdout=PROBE)
        if program == "sh" and "CLIO_CREATED_DIR" in args[1]:
            return self._mkdir(args[2])
        if program == "sh" and "port_in_use" in args[1]:
            return CommandResult(exit_code=0)
        if program == "sh" and "curl" in args[1]:
            running = any(row["running"] for row in self.containers.values())
            return CommandResult(exit_code=0, stdout="ready\n" if running else "waiting\n")
        if program == "sh" and "still present" in args[1]:
            return self._verified_removal(args[1], args[2])
        if program == "sh" and args[1].startswith("rmdir"):
            path = args[2]
            if not any(other != path and other.startswith(path + "/") for other in self.dirs):
                self.dirs.discard(path)
            return CommandResult(exit_code=0)
        if program == "rm" and args[:2] == ["-rf", "--"]:
            self.dirs = {d for d in self.dirs if d != args[2] and not d.startswith(args[2] + "/")}
            return CommandResult(exit_code=0)
        if program == "docker":
            return self._docker(args)
        raise AssertionError(f"unexpected command {program} {args}")

    def _verified_removal(self, script: str, ref: str) -> CommandResult:
        if "rm --force" in script:
            self.containers.pop(ref, None)
            gone = ref not in self.containers
        elif " rmi " in f" {script}":
            if not self.daemon_can_remove_images:
                return CommandResult(exit_code=1, stdout=f"still present: {ref}")
            self.images.discard(ref)
            gone = ref not in self.images
        else:
            self.dirs = {d for d in self.dirs if d != ref and not d.startswith(ref + "/")}
            gone = ref not in self.dirs
        return CommandResult(
            exit_code=0 if gone else 1, stdout="" if gone else f"still present: {ref}"
        )

    def _mkdir(self, path: str) -> CommandResult:
        made: list[str] = []
        current = path
        while current not in self.dirs:
            made.insert(0, current)
            current = posixpath.dirname(current)
        self.dirs.update(made)
        lines = [f"{'CLIO_CREATED_DIR' if d == path else 'CLIO_CREATED_PARENT'} {d}" for d in made]
        return CommandResult(exit_code=0, stdout="\n".join(lines))

    def _docker(self, args: list[str]) -> CommandResult:
        verb = args[0]
        if verb == "rm":
            return CommandResult(exit_code=0 if self.containers.pop(args[-1], None) else 1)
        if verb == "image":
            return CommandResult(exit_code=0 if args[-1] in self.images else 1)
        if verb == "pull":
            self.images.add(args[-1])
            return CommandResult(exit_code=0)
        if verb == "rmi":
            if args[-1] not in self.images:
                return CommandResult(exit_code=1)
            self.images.discard(args[-1])
            return CommandResult(exit_code=0)
        if verb == "run":
            name = args[args.index("--name") + 1]
            if self.fail_run:
                # Like Docker: the container is created, then the start fails.
                self.containers[name] = {"running": False, "args": [], "env": []}
                return CommandResult(exit_code=125, stderr="docker: port is already allocated")
            env = [args[i + 1] for i, token in enumerate(args) if token == "--env"]
            volume = args[args.index("--volume") + 1].split(":")[0]
            assert volume in self.dirs, "the cache directory must exist before the run"
            self.containers[name] = {
                "running": not self.exit_on_start,
                "args": args[next(i for i, a in enumerate(args) if a in self.images) + 1 :],
                "env": env,
            }
            return CommandResult(exit_code=0, stdout="0123abcd\n")
        if verb == "inspect":
            row = self.containers.get(args[-1])
            if row is None:
                return CommandResult(exit_code=1, stderr="No such object")
            if args[2] == "{{.State.Status}}":
                return CommandResult(
                    exit_code=0, stdout="running\n" if row["running"] else "exited\n"
                )
            # Verbatim shape of the Desktop PTY: the separator survives, tabs do not.
            line = f"{json.dumps(row['args'])} |clio| {json.dumps(row['env'])}"
            return CommandResult(exit_code=0, stdout=f"{line}\r\n")
        if verb == "logs":
            row = self.containers.get(args[-1])
            if row is None or not row["running"]:
                return CommandResult(exit_code=0, stdout="Error: unknown flag --bogus\n")
            return CommandResult(exit_code=0, stdout=SERVER_CONFIG)
        if verb in {"stop", "start"}:
            row = self.containers.get(args[-1])
            if row is None:
                return CommandResult(exit_code=1)
            row["running"] = verb == "start"
            return CommandResult(exit_code=0, stdout=args[-1])
        if verb == "exec":
            return CommandResult(exit_code=0, stdout="pulling manifest\nsuccess\n")
        raise AssertionError(f"unexpected docker {args}")

    async def forward(
        self, target_id: str, remote_port: int, preferred_local_port: int | None = None
    ) -> str:
        del target_id
        return f"http://127.0.0.1:{preferred_local_port or remote_port + 20000}"


def _ollama_ps(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/ps":
        return httpx.Response(
            200, json={"models": [{"name": "qwen2.5:0.5b", "context_length": 4096}]}
        )
    return httpx.Response(404)


def _runtime(
    tmp_path: Path, target: FakeLinuxTarget
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
        http_transport=httpx.MockTransport(_ollama_ps),
    )
    return runtime, store, row.id


async def _finish(
    runtime: InfrastructureRuntime,
    store: InfrastructureStore,
    service: str,
    request: ServiceActionRequest,
) -> Any:
    operation = runtime.start_action(service, request)
    for _ in range(500):
        current = store.operation(operation.id)
        if current is not None and current.state in {"succeeded", "failed", "cancelled"}:
            return current
        await asyncio.sleep(0.01)
    raise AssertionError("operation did not finish")


def _install(target_id: str, **extra: str) -> ServiceActionRequest:
    return ServiceActionRequest(
        target_id=target_id,
        action="install",
        variant_id="cpu",
        configuration={"model": "qwen2.5:0.5b", "param.num_parallel": "2", **extra},
    )


@pytest.mark.asyncio
async def test_install_records_everything_it_created_and_uninstall_removes_all_of_it(
    tmp_path: Path,
) -> None:
    target = FakeLinuxTarget()
    before = target.snapshot()
    runtime, store, target_id = _runtime(tmp_path, target)

    installed = await _finish(runtime, store, "ollama", _install(target_id))

    assert installed.state == "succeeded", installed.error
    record = store.service(target_id, "ollama")
    assert record is not None
    kinds = {(row.kind, row.ref) for row in record.owned_resources}
    service_dir = f"{HOME}/.local/share/clio/services/ares/clio-ollama"
    assert kinds == {
        ("parent_directory", f"{HOME}/.local"),
        ("parent_directory", f"{HOME}/.local/share"),
        ("parent_directory", f"{HOME}/.local/share/clio"),
        ("parent_directory", f"{HOME}/.local/share/clio/services"),
        ("parent_directory", f"{HOME}/.local/share/clio/services/ares"),
        ("parent_directory", service_dir),
        ("directory", f"{service_dir}/cache"),
        ("image", IMAGE),
        ("container", "clio-ollama"),
    }
    assert record.configuration["container_runtime"] == "docker"
    effective = {row.id: row for row in record.effective_parameters}
    assert (effective["num_parallel"].value, effective["num_parallel"].source) == (
        "2",
        "server_report",
    )
    assert (effective["context_length"].value, effective["context_length"].source) == (
        "4096",
        "server_report",
    )
    assert effective["loaded_context:qwen2.5:0.5b"].value == "4096"

    removed = await _finish(
        runtime,
        store,
        "ollama",
        ServiceActionRequest(target_id=target_id, action="uninstall", variant_id="cpu"),
    )

    assert removed.state == "succeeded", removed.error
    assert target.snapshot() == before
    assert store.service(target_id, "ollama") is None


@pytest.mark.asyncio
async def test_things_that_existed_before_the_deploy_are_never_removed(tmp_path: Path) -> None:
    preexisting_dirs = {f"{HOME}/.local", f"{HOME}/.local/share"}
    target = FakeLinuxTarget(images={IMAGE}, dirs=preexisting_dirs)
    runtime, store, target_id = _runtime(tmp_path, target)

    installed = await _finish(runtime, store, "ollama", _install(target_id))
    assert installed.state == "succeeded", installed.error
    record = store.service(target_id, "ollama")
    assert record is not None
    assert IMAGE not in {row.ref for row in record.owned_resources}

    await _finish(
        runtime,
        store,
        "ollama",
        ServiceActionRequest(target_id=target_id, action="uninstall", variant_id="cpu"),
    )

    assert IMAGE in target.images
    assert preexisting_dirs <= target.dirs
    assert not any(d.startswith(f"{HOME}/.local/share/clio") for d in target.dirs)
    assert target.containers == {}


@pytest.mark.asyncio
async def test_a_failed_deploy_removes_what_it_created_and_records_nothing(tmp_path: Path) -> None:
    target = FakeLinuxTarget()
    before = target.snapshot()
    target.fail_run = True
    runtime, store, target_id = _runtime(tmp_path, target)

    failed = await _finish(runtime, store, "ollama", _install(target_id))

    assert failed.state == "failed"
    assert "port is already allocated" in (failed.error or "")
    assert "Removed the" in failed.progress
    assert target.snapshot() == before
    assert store.service(target_id, "ollama") is None


@pytest.mark.asyncio
async def test_a_server_that_exits_before_answering_fails_with_its_logs_and_is_removed(
    tmp_path: Path,
) -> None:
    target = FakeLinuxTarget()
    before = target.snapshot()
    target.exit_on_start = True
    runtime, store, target_id = _runtime(tmp_path, target)

    failed = await _finish(runtime, store, "ollama", _install(target_id))

    assert failed.state == "failed"
    assert "stopped before it answered" in (failed.error or "")
    assert "unknown flag --bogus" in (failed.error or "")
    assert target.snapshot() == before


@pytest.mark.asyncio
async def test_lifecycle_actions_use_the_installed_configuration_not_the_form(
    tmp_path: Path,
) -> None:
    target = FakeLinuxTarget()
    runtime, store, target_id = _runtime(tmp_path, target)
    assert (await _finish(runtime, store, "ollama", _install(target_id))).state == "succeeded"
    target.commands.clear()

    stopped = await _finish(
        runtime,
        store,
        "ollama",
        ServiceActionRequest(
            target_id=target_id, action="stop", variant_id="cuda", configuration={}
        ),
    )

    assert stopped.state == "succeeded", stopped.error
    stop = [spec for spec in target.commands if spec.program == "docker" and spec.args[0] == "stop"]
    assert stop and stop[0].args == ["stop", "clio-ollama"]
    record = store.service(target_id, "ollama")
    assert record is not None and record.variant_id == "cpu"
    assert record.configuration["model"] == "qwen2.5:0.5b"
    assert len(record.owned_resources) == 9


class _NeverReadyTarget(FakeLinuxTarget):
    async def execute(self, target_id: str, spec: CommandSpec) -> CommandResult:
        if spec.program == "sh" and "curl" in spec.args[1]:
            self.commands.append(spec)
            return CommandResult(exit_code=0, stdout="waiting\n")
        return await super().execute(target_id, spec)


@pytest.mark.asyncio
async def test_the_ledger_is_durable_while_a_deploy_is_still_running_and_cancel_cleans_up(
    tmp_path: Path,
) -> None:
    target = _NeverReadyTarget()
    before = target.snapshot()
    runtime, store, target_id = _runtime(tmp_path, target)
    operation = runtime.start_action("ollama", _install(target_id))
    for _ in range(500):
        record = store.service(target_id, "ollama")
        if record is not None and any(row.kind == "container" for row in record.owned_resources):
            break
        await asyncio.sleep(0.01)

    # A CLIO that stopped right now would still know what to remove.
    restored = InfrastructureStore(tmp_path / "infrastructure.json").service(target_id, "ollama")
    assert restored is not None
    assert {row.kind for row in restored.owned_resources} >= {"container", "image", "directory"}

    cancelled = await runtime.cancel(operation.id)

    assert cancelled.state == "cancelled"
    assert "Removed the" in cancelled.progress
    assert target.snapshot() == before
    assert store.service(target_id, "ollama") is None


@pytest.mark.asyncio
async def test_an_incomplete_teardown_keeps_the_ledger_so_uninstall_can_finish(
    tmp_path: Path,
) -> None:
    target = FakeLinuxTarget()
    target.fail_run = True
    target.daemon_can_remove_images = False
    runtime, store, target_id = _runtime(tmp_path, target)

    failed = await _finish(runtime, store, "ollama", _install(target_id))

    assert failed.state == "failed"
    assert "Cleanup incomplete" in failed.progress
    record = store.service(target_id, "ollama")
    assert record is not None
    assert IMAGE in {row.ref for row in record.owned_resources}


@pytest.mark.asyncio
async def test_reinstall_after_a_reload_uses_the_installed_configuration(tmp_path: Path) -> None:
    target = FakeLinuxTarget()
    runtime, store, target_id = _runtime(tmp_path, target)
    assert (await _finish(runtime, store, "ollama", _install(target_id))).state == "succeeded"

    reinstalled = await _finish(
        runtime,
        store,
        "ollama",
        ServiceActionRequest(
            target_id=target_id, action="reinstall", variant_id="cuda", configuration={}
        ),
    )

    assert reinstalled.state == "succeeded", reinstalled.error
    record = store.service(target_id, "ollama")
    assert record is not None and record.variant_id == "cpu"
    assert record.configuration["param.num_parallel"] == "2"
    assert IMAGE in target.images
    assert IMAGE in {row.ref for row in record.owned_resources}
    await _finish(
        runtime,
        store,
        "ollama",
        ServiceActionRequest(target_id=target_id, action="uninstall", variant_id="cpu"),
    )
    assert IMAGE not in target.images


def test_discovery_learns_the_default_context_of_a_running_managed_ollama(tmp_path: Path) -> None:
    from clio_agent.gact.infrastructure.models import EffectiveParameter, ServiceRecord
    from clio_agent.gact.infrastructure.served_defaults import ollama_context_default_lookup

    store = InfrastructureStore(tmp_path / "infrastructure.json")
    lookup = ollama_context_default_lookup(store)
    record = ServiceRecord(
        id="ares:ollama",
        service_id="ollama",
        target_id="ares",
        variant_id="cpu",
        state="running",
        connection_url="http://127.0.0.1:58473",
        effective_parameters=[
            EffectiveParameter(
                id="context_length",
                label="Context length",
                value="4096",
                source="server_report",
                detail="Ollama server config (startup log)",
            )
        ],
    )
    store.put_service(record)

    fact = lookup("http://127.0.0.1:58473")
    assert fact is not None and fact.value == 4096
    assert "CLIO-managed Ollama on ares" in fact.detail
    assert lookup("http://127.0.0.1:9") is None
    store.put_service(record.model_copy(update={"state": "stopped"}))
    assert lookup("http://127.0.0.1:58473") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [("ollama", "llama_cpp"), ("llama_cpp", "ollama")])
async def test_parents_shared_by_two_deployments_go_with_the_last_uninstall(
    tmp_path: Path, order: tuple[str, str]
) -> None:
    """Live on ares: vLLM created ~/.local/share/clio/services/<host>, Ollama found it
    there; vLLM's uninstall could not remove it (not empty), Ollama had not recorded
    it, so it outlived both."""

    target = FakeLinuxTarget()
    before = target.snapshot()
    runtime, store, target_id = _runtime(tmp_path, target)
    first = await _finish(runtime, store, "ollama", _install(target_id))
    assert first.state == "succeeded", first.error
    second = await _finish(
        runtime,
        store,
        "llama_cpp",
        ServiceActionRequest(
            target_id=target_id,
            action="install",
            variant_id="cpu",
            configuration={"hf_model": "Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M"},
        ),
    )
    assert second.state == "succeeded", second.error

    for service in order:
        removed = await _finish(
            runtime,
            store,
            service,
            ServiceActionRequest(target_id=target_id, action="uninstall", variant_id="cpu"),
        )
        assert removed.state == "succeeded", removed.error

    assert target.snapshot() == before
