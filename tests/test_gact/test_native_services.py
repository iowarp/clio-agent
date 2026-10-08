"""Native service ownership, truthful readiness and pinned probe activation."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.infrastructure import node_service
from clio_agent.gact.infrastructure.drivers import build_driver_plan
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    InfrastructureTarget,
    ServiceActionRequest,
    TargetFacts,
)
from clio_agent.gact.infrastructure.native_vllm import (
    ATTENTION_PROFILE,
    CONNECTOR_REVISION,
    FLOWCEPT_REVISION,
    native_variants,
)
from clio_agent.gact.infrastructure.service_observation import parse_observation
from clio_agent.gact.infrastructure.service_readiness import ServerExitedError, wait_until_ready
from clio_agent.gact.infrastructure.service_settlement import settle_service
from clio_agent.gact.infrastructure.store import InfrastructureStore


def facts(**values: Any) -> TargetFacts:
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
            **values,
        }
    )


def plan(action: str = "install", **configuration: str) -> Any:
    return build_driver_plan(
        service_id="vllm",
        action=action,
        variant_id="native-cuda-attention",
        configuration={
            "model": "/data/models/qualified",
            "flowcept_settings": "/data/provenance/settings.yaml",
            **configuration,
        },
        facts=facts(),
        target=InfrastructureTarget(id="node", label="Node", kind="ssh"),
        api_key="test-secret-value",
    )


def test_native_compatibility_uses_host_facts_not_container_availability() -> None:
    assert all(item.compatible for item in native_variants(facts(docker_available=False)))
    for override in (
        {"os": "windows"},
        {"accelerator": "none"},
        {"uv_available": False},
        {"arch": "unknown"},
    ):
        assert not any(item.compatible for item in native_variants(facts(**override)))


def test_native_install_and_start_are_distinct_and_pinned() -> None:
    install = plan()
    assert install.readiness.capability == "installed"
    payload = json.loads(install.commands[-1].stdin)
    manifest = payload["manifest"]
    assert manifest["definition_version"] == ATTENTION_PROFILE
    assert "vllm==0.27.0" in manifest["project"]
    assert CONNECTOR_REVISION in manifest["project"] and FLOWCEPT_REVISION in manifest["project"]
    assert "test-secret" not in install.commands[-1].stdin
    script = manifest["launcher"]
    assert (
        script.index("install_probe()")
        < script.index("from flowcept")
        < script.index("runpy.run_module(")
    )
    assert "start_persistence=False" in script
    assert "VLLM_ENABLE_V1_MULTIPROCESSING" in script
    start = plan("start")
    assert start.readiness.capability == "serving"
    assert "test-secret-value" not in repr(start.commands[0].args)
    assert json.loads(start.commands[0].stdin)["api_key"] == "test-secret-value"
    assert start.failure_cleanup
    assert (
        json.loads(start.failure_cleanup[0].stdin)["require_operation_id"]
        == json.loads(start.commands[0].stdin)["operation_id"]
    )


def test_native_remove_retains_receipt_and_delete_is_separate() -> None:
    assert plan("uninstall").retain_record
    assert not plan("delete_data").retain_record
    with pytest.raises(ValueError, match="separate deletion"):
        build_driver_plan(
            service_id="ollama",
            action="delete_data",
            variant_id="cpu",
            configuration={},
            facts=facts(),
        )


def test_native_requires_host_model_and_flowcept_configuration() -> None:
    with pytest.raises(ValueError, match="downloaded model directory"):
        plan(model="org/model")
    with pytest.raises(ValueError, match="Flowcept settings"):
        plan(flowcept_settings="")


def test_structured_readiness_never_matches_running_false() -> None:
    definition = plan("start")
    responses = iter(
        [
            {
                "phase": "running",
                "installed": True,
                "running": True,
                "serving": False,
                "worker_alive": True,
            },
            {
                "phase": "running",
                "installed": True,
                "running": True,
                "serving": True,
                "worker_alive": True,
            },
        ]
    )
    calls = []

    async def execute(spec: Any) -> CommandResult:
        calls.append(spec)
        return CommandResult(exit_code=0, stdout=node_service.MARKER + json.dumps(next(responses)))

    asyncio.run(wait_until_ready(definition.readiness, execute, lambda progress: None, interval=0))
    assert len(calls) == 2
    observation = parse_observation(
        [
            node_service.MARKER
            + json.dumps({"phase": "stopped", "running": False, "installed": True})
        ]
    )
    assert observation.service_state == "stopped"

    async def stopped(spec: Any) -> CommandResult:
        return CommandResult(
            exit_code=0,
            stdout=node_service.MARKER + '{"phase":"failed","error":"installation failed"}',
        )

    with pytest.raises(ServerExitedError, match="installation failed"):
        asyncio.run(
            wait_until_ready(definition.readiness, stopped, lambda progress: None, interval=0)
        )


def test_settlement_preserves_data_and_installed_is_not_running(tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infra.json")
    runtime = SimpleNamespace(store=store)
    request = ServiceActionRequest(
        target_id="local",
        action="install",
        variant_id="native-cuda",
        configuration={"model": "/data/model"},
    )
    receipt = node_service.MARKER + '{"phase":"stopped","installed":true,"running":false}'
    asyncio.run(settle_service(runtime, "vllm", request, None, [receipt], retain_record=True))
    assert store.service("local", "vllm").state == "stopped"
    request.action = "uninstall"
    receipt = node_service.MARKER + '{"phase":"not_installed","installed":false,"running":false}'
    asyncio.run(settle_service(runtime, "vllm", request, None, [receipt], retain_record=True))
    assert store.service("local", "vllm").state == "not_installed"
    request.action = "delete_data"
    asyncio.run(settle_service(runtime, "vllm", request, None, [receipt]))
    assert store.service("local", "vllm") is None


def test_target_refuses_foreign_directory_and_changed_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(node_service, "locked", lambda root: nullcontext())
    root = tmp_path / "native"
    root.mkdir()
    (root / "user-file").write_text("keep")
    with pytest.raises(ValueError, match="non-empty"):
        node_service.prepare(root, "deployment-one")
    assert (root / "user-file").read_text() == "keep"
    owned = tmp_path / "owned"
    node_service.prepare(owned, "deployment-one")
    with pytest.raises(ValueError, match="another deployment"):
        node_service.prepare(owned, "deployment-two")


def test_a_directory_installed_on_another_host_names_that_host_and_the_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F020: a shared-filesystem directory from another HPC node explains how to recover."""
    monkeypatch.setattr(node_service, "locked", lambda root: nullcontext())
    owned = tmp_path / "owned"
    monkeypatch.setattr(node_service.socket, "gethostname", lambda: "gpua018")
    node_service.prepare(owned, "local:gpua018")
    monkeypatch.setattr(node_service.socket, "gethostname", lambda: "gpua078")
    with pytest.raises(
        ValueError, match="installed on host gpua018, not on this host .gpua078.; install"
    ):
        node_service.owner(owned, "local:gpua078")
    assert json.loads((owned / "owner.json").read_text())["host"] == "gpua018"


def test_cleanup_cannot_stop_a_previous_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(node_service, "locked", lambda root: nullcontext())
    monkeypatch.setattr(node_service, "checked_root", lambda raw: Path(raw))
    root = tmp_path / "owned"
    node_service.prepare(root, "owner")
    node_service.write_json(
        root / "receipt.json", {"phase": "stopped", "operation_id": "old-operation"}
    )
    monkeypatch.setattr(
        node_service, "stop", lambda root: pytest.fail("Must not stop a previous operation")
    )
    result = node_service.control(
        {
            "root": str(root),
            "owner": "owner",
            "action": "stop",
            "require_operation_id": "failed-new-operation",
        }
    )
    assert result["phase"] == "stopped"


def test_failure_cleanup_of_a_refused_foreign_host_start_has_nothing_to_undo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F020: the cleanup stop after a refused start must not repeat the refusal."""
    monkeypatch.setattr(node_service, "locked", lambda root: nullcontext())
    root = tmp_path / "owned"
    monkeypatch.setattr(node_service.socket, "gethostname", lambda: "gpua078")
    node_service.prepare(root, "local:gpua078")
    node_service.write_json(root / "receipt.json", {"phase": "stopped", "operation_id": "install"})
    monkeypatch.setattr(node_service.socket, "gethostname", lambda: "gpua012")
    monkeypatch.setattr(node_service, "stop", lambda root: pytest.fail("Must not stop"))
    request = {"root": str(root), "owner": "local:gpua012", "action": "stop"}
    with pytest.raises(ValueError, match="installed on host gpua078"):
        node_service.control(dict(request))  # a person's own stop still explains the host
    result = node_service.control({**request, "require_operation_id": "refused-start"})
    assert result["phase"] == "untouched" and not result["running"]


def test_reused_pid_is_not_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(node_service, "identity", lambda pid: "new-boot:new-start")
    assert not node_service.alive({"pid": 12, "process_identity": "old-boot:old-start"})


def test_effective_artifacts_remain_after_runtime_removal(tmp_path: Path) -> None:
    environment = tmp_path / "environment"
    environment.mkdir()
    lockfile = environment / "uv.lock"
    lockfile.write_text("version = 1\n")
    before = node_service.effective_artifacts(tmp_path)
    node_service.write_json(tmp_path / "receipt.json", {"effective_artifacts": before})
    lockfile.unlink()
    assert node_service.effective_artifacts(tmp_path) == before
    assert len(before["python_lock_sha256"]) == 64


def test_provenance_verification_expires_with_configuration_or_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(node_service, "alive", lambda receipt: True)
    monkeypatch.setattr(node_service, "serves_identity", lambda root, receipt, key="": True)
    monkeypatch.setattr(node_service, "http_get", lambda url, key="": (200, b""))
    (tmp_path / "evidence").mkdir()
    node_service.write_json(
        tmp_path / "receipt.json",
        {
            "phase": "running",
            "configuration_revision": "config-a",
            "generation": "process-a",
            "health_url": "http://localhost:8008",
        },
    )
    proof = {
        "configuration_revision": "config-a",
        "generation": "process-a",
        "provenance_ingesting": True,
    }
    node_service.write_json(tmp_path / "evidence/verification.json", proof)
    assert node_service.observation(tmp_path)["provenance_ingesting"]
    for changed in ({"generation": "process-b"}, {"configuration_revision": "config-b"}):
        node_service.write_json(tmp_path / "evidence/verification.json", {**proof, **changed})
        assert not node_service.observation(tmp_path)["provenance_ingesting"]


def test_attention_launcher_runs_until_vllm_entrypoint(tmp_path: Path) -> None:
    """Execute the generated launcher against stub packages: it must reach vLLM's entrypoint."""
    import subprocess
    import sys

    stubs = tmp_path / "stubs"
    (stubs / "vllm" / "entrypoints" / "openai").mkdir(parents=True)
    for package in ("vllm", "vllm/entrypoints", "vllm/entrypoints/openai"):
        (stubs / package / "__init__.py").write_text("")
    (stubs / "vllm" / "entrypoints" / "openai" / "api_server.py").write_text(
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv, 'settings': os.environ['FLOWCEPT_SETTINGS_PATH']}))\n"
    )
    (stubs / "vllm_attn_connector.py").write_text("def install_probe():\n    return True\n")
    (stubs / "flowcept.py").write_text(
        "from contextlib import contextmanager\n"
        "@contextmanager\n"
        "def Flowcept(*args, **kwargs):\n"
        "    yield\n"
    )
    settings = tmp_path / "settings.yaml"
    settings.write_text("")
    root = tmp_path / "service"
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps({"flowcept_settings": str(settings), "workflow_id": "wf-test"})
    )
    script = root / "launcher.py"
    script.write_text(json.loads(plan().commands[-1].stdin)["manifest"]["launcher"])
    result = subprocess.run(
        [sys.executable, str(script), "--model", "/data/models/qualified"],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(stubs), "PATH": "/usr/bin:/bin"},
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed["settings"] == str(settings)
    transfer = json.loads(observed["argv"][observed["argv"].index("--kv-transfer-config") + 1])
    assert transfer["kv_connector_extra_config"] == {
        "workflow_id": "wf-test",
        "out_dir": str(root / "evidence"),
    }


@pytest.mark.parametrize("attention", [False, True])
def test_launcher_keeps_vllm_ipc_sockets_within_unix_path_limit(
    tmp_path: Path, attention: bool
) -> None:
    """A deep service temp dir must not overflow AF_UNIX paths (live: ZMQError on Delta /work)."""
    import subprocess
    import sys

    from clio_agent.gact.infrastructure.native_vllm import launcher

    deep = tmp_path / ("d" * 60) / ("e" * 60)
    deep.mkdir(parents=True)
    root = tmp_path / "service"
    stubs = root / "stubs"
    (stubs / "vllm" / "entrypoints" / "openai").mkdir(parents=True)
    for package in ("vllm", "vllm/entrypoints", "vllm/entrypoints/openai"):
        (stubs / package / "__init__.py").write_text("")
    (stubs / "vllm" / "entrypoints" / "openai" / "api_server.py").write_text(
        "import os\nprint(os.environ['VLLM_RPC_BASE_PATH'])\n"
    )
    (stubs / "vllm_attn_connector.py").write_text("def install_probe():\n    return True\n")
    (stubs / "flowcept.py").write_text(
        "from contextlib import contextmanager\n"
        "@contextmanager\n"
        "def Flowcept(*args, **kwargs):\n"
        "    yield\n"
    )
    settings = tmp_path / "settings.yaml"
    settings.write_text("")
    (root / "manifest.json").write_text(
        json.dumps({"flowcept_settings": str(settings), "workflow_id": "wf-test"})
    )
    (root / "launch.py").write_text(launcher(attention))
    result = subprocess.run(
        [sys.executable, str(root / "launch.py")],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(stubs), "PATH": "/usr/bin:/bin", "TMPDIR": str(deep)},
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    base = result.stdout.strip().splitlines()[-1]
    assert len(f"{base}/{'0' * 36}") < 107
    assert Path(base).stat().st_mode & 0o077 == 0


def test_plain_launcher_survives_vllm_spawning_its_engine_core(tmp_path: Path) -> None:
    """A spawned child re-imports the launcher; it must not start a second server (live on Delta)."""
    import subprocess
    import sys

    from clio_agent.gact.infrastructure.native_vllm import launcher

    stubs = tmp_path / "stubs"
    (stubs / "vllm" / "entrypoints" / "openai").mkdir(parents=True)
    for package in ("vllm", "vllm/entrypoints", "vllm/entrypoints/openai"):
        (stubs / package / "__init__.py").write_text("")
    (stubs / "engine_core.py").write_text("def run():\n    pass\n")
    (stubs / "vllm" / "entrypoints" / "openai" / "api_server.py").write_text(
        "import multiprocessing\n"
        "import engine_core\n"
        "child = multiprocessing.get_context('spawn').Process(target=engine_core.run)\n"
        "child.start()\n"
        "child.join()\n"
        "print('served', child.exitcode)\n"
    )
    script = tmp_path / "launch.py"
    script.write_text(launcher(False))
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(stubs), "PATH": "/usr/bin:/bin"},
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["served", "0"], result.stderr


def test_service_worker_exposes_its_environment_tools_first(tmp_path: Path) -> None:
    """vLLM's flashinfer JIT runs ``ninja`` from the service venv (live: FileNotFoundError)."""
    env = node_service.worker_environment(
        tmp_path,
        {"environment": {"VLLM_CPU_KVCACHE_SPACE": "4"}},
        {"PATH": "/usr/bin:/bin", "VIRTUAL_ENV": "/clio/.venv", "PYTHONPATH": "/clio/src"},
    )
    assert env["PATH"].split(os.pathsep) == [
        str(tmp_path / "environment/.venv/bin"),
        "/usr/bin",
        "/bin",
    ]
    assert "VIRTUAL_ENV" not in env and "PYTHONPATH" not in env
    assert env["TMPDIR"] == str(tmp_path / "tmp")
    assert env["VLLM_CPU_KVCACHE_SPACE"] == "4"


@contextmanager
def _model_server(key: str | None, models: list[str]) -> Iterator[int]:
    """A loopback OpenAI-style server; ``key=None`` means it enforces no key."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path == "/v1/models" and key is not None:
                if self.headers.get("Authorization") != f"Bearer {key}":
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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _running_receipt(root: Path, port: int, identity: dict[str, str]) -> None:
    node_service.write_json(root / "manifest.json", {"identity": identity})
    node_service.write_json(
        root / "receipt.json",
        {"phase": "running", "pid": 1, "health_url": f"http://127.0.0.1:{port}/health"},
    )


def test_readiness_refuses_a_server_that_accepts_keyless_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F010/F025: a 200 from someone else's open server on the port is not "serving"."""
    monkeypatch.setattr(node_service, "alive", lambda receipt: True)
    with _model_server(None, ["/models/ours"]) as port:
        _running_receipt(tmp_path, port, {"kind": "openai", "served_model": "/models/ours"})
        assert not node_service.observation(tmp_path)["serving"]
        assert not node_service.observation(tmp_path, key="k")["serving"]


def test_readiness_requires_our_key_and_served_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(node_service, "alive", lambda receipt: True)
    with _model_server("per-launch", ["/models/ours"]) as port:
        _running_receipt(tmp_path, port, {"kind": "openai", "served_model": "/models/ours"})
        assert node_service.observation(tmp_path, key="per-launch")["serving"]
        # Without the key (a later status call) a keyless refusal is the identity.
        assert node_service.observation(tmp_path)["serving"]
        assert not node_service.observation(tmp_path, key="another-launch")["serving"]
        _running_receipt(tmp_path, port, {"kind": "openai", "served_model": "/models/other"})
        assert not node_service.observation(tmp_path, key="per-launch")["serving"]


def test_readiness_without_a_declared_identity_is_not_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(node_service, "alive", lambda receipt: True)
    with _model_server("per-launch", []) as port:
        _running_receipt(tmp_path, port, {})
        assert not node_service.observation(tmp_path, key="per-launch")["serving"]


@pytest.mark.parametrize("up", [True, False])
def test_stack_readiness_requires_every_owned_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, up: bool
) -> None:
    """Container stacks (Apptainer instances run in their own session) prove identity by hook."""
    monkeypatch.setattr(node_service, "alive", lambda receipt: True)
    (tmp_path / "stack.py").write_text(
        "import sys\nsys.exit(0 if sys.argv[1] == 'running' and " + repr(up) + " else 1)\n"
    )
    with _model_server(None, []) as port:
        _running_receipt(tmp_path, port, {"kind": "components", "hook": "stack.py"})
        assert node_service.observation(tmp_path)["serving"] is up


def test_stack_identity_refuses_a_hook_outside_the_service_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(node_service, "alive", lambda receipt: True)
    with _model_server(None, []) as port:
        _running_receipt(tmp_path, port, {"kind": "components", "hook": "../stack.py"})
        assert not node_service.observation(tmp_path)["serving"]


def test_listeners_fall_back_to_a_loopback_probe_without_proc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS/Windows have no /proc/net: an answering loopback port still refuses a start."""
    monkeypatch.setattr(node_service.Path, "is_file", lambda self: False)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert node_service.listeners(port)
    assert not node_service.listeners(port)


def test_status_during_start_carries_the_per_launch_key_only_then() -> None:
    from clio_agent.gact.infrastructure.supervised_service import supervised_plan

    def plan(action: str) -> Any:
        return supervised_plan(
            action,
            directory="/srv/clio/services/vllm",
            ownership="o",
            manifest={},
            port=1,
            label="vLLM",
            configuration={},
            api_key="per-launch",
        )

    start = plan("start")
    assert start.readiness is not None
    assert json.loads(start.readiness.health.stdin)["api_key"] == "per-launch"
    assert "api_key" not in json.loads(plan("status").commands[0].stdin)


def test_vllm_identity_names_the_served_model() -> None:
    from clio_agent.gact.infrastructure.native_vllm import served_model

    assert served_model("/m", []) == "/m"
    assert served_model("/m", ["--served-model-name", "qwen"]) == "qwen"
    assert served_model("/m", ["--served-model-name=qwen"]) == "qwen"


def test_start_refuses_a_port_with_any_existing_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F010: the old SO_REUSEADDR probe bound beside a foreign 0.0.0.0 listener."""
    manifest = {"port": 0, "model_path": ""}
    monkeypatch.setattr(
        node_service, "observation", lambda root, health=True, key="": {"installed": True}
    )
    with socket.socket() as foreign:
        foreign.bind(("0.0.0.0", 0))
        foreign.listen()
        manifest["port"] = foreign.getsockname()[1]
        revision = node_service.hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest()
        node_service.write_json(
            tmp_path / "receipt.json", {"phase": "stopped", "configuration_revision": revision}
        )
        with pytest.raises(ValueError, match="already has a listener"):
            node_service.launch(tmp_path, {"action": "start", "manifest": manifest})
