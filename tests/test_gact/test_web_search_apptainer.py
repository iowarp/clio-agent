"""CLIO Web Search on Apptainer (and Podman): catalog gating, plans and the target-side scripts."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.infrastructure import web_search_apptainer as backend
from clio_agent.gact.infrastructure.drivers import (
    build_driver_plan,
    service_connection_port,
    service_definitions,
)
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    ContainerRuntimeFact,
    InfrastructureTarget,
    ManagedServiceDefinition,
    OwnedResource,
    TargetFacts,
)
from clio_agent.gact.infrastructure.plan import DriverPlan
from clio_agent.gact.infrastructure.web_search_service import (
    WEB_SEARCH_IMAGE,
    WEB_SEARCH_PINNED_IMAGE,
)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Apptainer is Linux-only")
TARGET = InfrastructureTarget(id="node", label="Node", kind="ssh")
SERVICE_DIR = "/data/clio/services/node/clio-web-search"


def facts(*runtimes: str, docker: bool = False) -> TargetFacts:
    return TargetFacts(
        target_id="node",
        label="Node",
        os="linux",
        arch="x86_64",
        docker_available=docker,
        docker_installed=docker,
        agent_data_root="/data/clio",
        hostname="node",
        container_runtimes=[
            ContainerRuntimeFact(name=name, installed=True, usable=True)  # type: ignore[arg-type]
            for name in runtimes
        ],
    )


def plan(action: str, configuration: dict[str, str] | None = None, **kwargs: Any) -> DriverPlan:
    return build_driver_plan(
        service_id="web_search",
        action=action,
        variant_id="container",
        configuration={"container_runtime": "apptainer", **(configuration or {})},
        facts=kwargs.pop("target_facts", facts("apptainer")),
        target=TARGET,
        **kwargs,
    )


def web_search(target_facts: TargetFacts) -> ManagedServiceDefinition:
    return next(row for row in service_definitions(target_facts) if row.id == "web_search")


def argv(spec: CommandSpec) -> str:
    return " ".join([spec.program, *spec.args])


# --- catalog ------------------------------------------------------------------------------


def test_apptainer_only_host_offers_web_search_with_the_pinned_image() -> None:
    row = web_search(facts("apptainer"))
    variant = row.variants[0]
    assert variant.compatible
    assert variant.artifact == WEB_SEARCH_PINNED_IMAGE
    digest = WEB_SEARCH_PINNED_IMAGE.rsplit("@sha256:", 1)[1]
    assert len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest)
    runtime = next(field for field in row.configuration_fields if field.id == "container_runtime")
    assert runtime.options == ["apptainer"]


def test_podman_only_host_offers_web_search() -> None:
    row = web_search(facts("podman"))
    assert row.variants[0].compatible
    assert row.variants[0].reason == "Runs with Podman."


def test_a_host_without_a_usable_runtime_says_why() -> None:
    bare = TargetFacts(
        target_id="node",
        label="Node",
        os="linux",
        arch="x86_64",
        container_runtimes=[
            ContainerRuntimeFact(name="docker", reason="not_installed"),
            ContainerRuntimeFact(name="podman", reason="not_installed"),
            ContainerRuntimeFact(name="apptainer", reason="not_installed"),
        ],
    )
    variant = web_search(bare).variants[0]
    assert not variant.compatible
    assert "No container runtime can run services here" in variant.reason
    assert "Apptainer is not installed." in variant.reason
    with pytest.raises(ValueError, match="No container runtime"):
        plan("install", {"container_runtime": ""}, target_facts=bare)


# --- Docker and Podman keep the image's own entrypoint --------------------------------------


def test_docker_install_is_unchanged_and_podman_uses_the_same_commands() -> None:
    docker = build_driver_plan(
        service_id="web_search",
        action="install",
        variant_id="container",
        configuration={"contact_email": "alice@example.org"},
        facts=facts("docker", docker=True),
    )
    assert [argv(spec) for spec in docker.commands] == [
        f"docker pull {WEB_SEARCH_IMAGE}",
        "docker run --detach --name clio-web-search --restart unless-stopped "
        "--publish 127.0.0.1:8089:8080 --publish 127.0.0.1:8090:6379 "
        "--volume clio-web-search-data:/var/lib/clio-web-search "
        "--env CLIO_WEB_SEARCH_TASK_BACKEND_PUBLIC_PORT=8090 "
        f"--env CLIO_WEB_SEARCH_CONTACT_EMAIL=alice@example.org {WEB_SEARCH_IMAGE}",
    ]
    assert docker.configuration and docker.configuration["container_runtime"] == "docker"
    podman = build_driver_plan(
        service_id="web_search",
        action="install",
        variant_id="container",
        configuration={},
        facts=facts("podman"),
    )
    assert [spec.program for spec in podman.commands] == ["podman", "podman"]
    # A deployment recorded before runtimes were negotiated is a Docker one.
    legacy = build_driver_plan(
        service_id="web_search",
        action="stop",
        variant_id="container",
        configuration={},
        facts=facts("apptainer"),
    )
    assert argv(legacy.commands[0]) == "docker stop clio-web-search"


def test_docker_verify_runs_one_search_inside_the_container_and_keeps_data_whole() -> None:
    verify = build_driver_plan(
        service_id="web_search",
        action="verify",
        variant_id="container",
        configuration={"container_runtime": "docker"},
        facts=facts("docker", docker=True),
    )
    spec = verify.commands[0]
    assert spec.args[:4] == ["exec", "--user", "65534:65534", "clio-web-search"]
    assert spec.args[-3:-1] == [
        "http://127.0.0.1:8080",
        "/var/lib/clio-web-search/evidence/verification.json",
    ]
    with pytest.raises(ValueError, match="available on Apptainer"):
        build_driver_plan(
            service_id="web_search",
            action="delete_data",
            variant_id="container",
            configuration={"container_runtime": "docker"},
            facts=facts("docker", docker=True),
        )


# --- Apptainer plans ------------------------------------------------------------------------


def test_apptainer_install_pins_the_image_and_keeps_secrets_out_of_argv() -> None:
    install = plan("install", {"contact_email": "alice@example.org"})
    joined = [argv(spec) for spec in install.commands]
    pulls = [line for line in joined if "clio-apptainer-pull" in line]
    assert len(pulls) == 2 and all(WEB_SEARCH_PINNED_IMAGE in line for line in pulls)
    # The shared, digest-keyed store beside every service folder on this host.
    assert all("/data/clio/services/node/apptainer-images " in line for line in pulls)
    run = install.commands[-1]
    assert "alice@example.org" not in " ".join(joined)
    assert run.stdin == "alice@example.org\n"
    assert backend.EMAIL_VARIABLE in run.args[1]
    launch = run.args[run.args.index("instance") - 1 :]
    assert launch[:6] == [
        "apptainer",
        "instance",
        "run",
        "--cleanenv",
        "--containall",
        "--writable-tmpfs",
    ]
    assert f"{SERVICE_DIR}/data:/var/lib/clio-web-search" in launch
    assert f"{SERVICE_DIR}/runtime/entrypoint.sh:{backend.ENTRYPOINT}:ro" in launch
    assert launch[-2:] == [f"{SERVICE_DIR}/images/clio-web-search.sif", "clio-web-search"]
    assert install.connection_port == 8089
    assert install.readiness is not None
    assert "/v1/capabilities" in install.readiness.health.args[1]
    configuration = install.configuration or {}
    assert configuration["deployment_id"].startswith("clio-ws-")
    assert configuration["storage.service_directory"] == SERVICE_DIR
    assert configuration["grobid_port"] == "18070"


def test_apptainer_install_ledger_holds_the_shared_sif_and_instance_but_not_the_data() -> None:
    install = plan("install")
    created: list[OwnedResource] = []
    for index, spec in enumerate(install.commands):
        recorder = install.recorders.get(index)
        if recorder is None:
            continue
        if "clio-apptainer-pull" in argv(spec):
            stdout = "CLIO_SHARED_IMAGE /data/clio/services/node/apptainer-images/x.sif\n"
        elif spec.args and spec.args[-1] == f"{SERVICE_DIR}/data":
            stdout = f"CLIO_CREATED_PARENT {SERVICE_DIR}\nCLIO_CREATED_DIR {SERVICE_DIR}/data\n"
        else:
            stdout = f"CLIO_CREATED_DIR {spec.args[-1]}\n" if spec.program == "sh" else ""
        created.extend(recorder(CommandResult(exit_code=0, stdout=stdout)))
    kinds = {(row.kind, row.ref) for row in created}
    assert ("shared_image", "/data/clio/services/node/apptainer-images/x.sif") in kinds
    assert ("container", "clio-web-search") in kinds
    assert ("parent_directory", SERVICE_DIR) in kinds
    assert ("directory", f"{SERVICE_DIR}/runtime") in kinds
    assert not any(row.ref == f"{SERVICE_DIR}/data" for row in created)


def test_apptainer_ports_are_private_validated_and_distinct() -> None:
    assert (
        service_connection_port("web_search", {"container_runtime": "apptainer", "port": "28089"})
        == 28089
    )
    assert service_connection_port("web_search", {"port": "28089"}) == 8089
    with pytest.raises(ValueError, match="distinct ports"):
        plan("install", {"searxng_port": "18070"})
    with pytest.raises(ValueError, match="from 1024"):
        plan("install", {"task_backend_port": "80"})


def test_apptainer_uninstall_is_ownership_scoped_and_delete_data_removes_the_folder() -> None:
    owned = [
        OwnedResource(kind="container", ref="clio-web-search", runtime="apptainer"),
        OwnedResource(kind="directory", ref=f"{SERVICE_DIR}/runtime"),
    ]
    uninstall = plan("uninstall", owned=owned)
    sif = f"{SERVICE_DIR}/images/clio-web-search.sif"
    assert uninstall.commands[0].args[-2:] == ["clio-web-search", sif]
    assert "foreign_instance" in uninstall.commands[0].args[1]
    assert not any(f"{SERVICE_DIR}/data" in spec.args for spec in uninstall.commands)
    delete = plan("delete_data", owned=owned)
    assert delete.commands[1].args[-1] == f"{SERVICE_DIR}/data"
    assert len(delete.commands) == len(uninstall.commands) + 1


def test_apptainer_verify_requires_the_owned_instance_then_searches_through_it() -> None:
    verify = plan("verify", {"port": "28089"})
    assert verify.commands[0].args[-1] == "require"
    search = verify.commands[1]
    assert search.args[:3] == ["exec", "--cleanenv", "instance://clio-web-search"]
    assert "http://127.0.0.1:28089" in search.args


def test_the_launcher_binds_every_listener_to_the_loopback() -> None:
    launcher = backend.LAUNCHER
    assert "0.0.0.0" not in launcher
    listeners = ("--bind 127.0.0.1", '--host 127.0.0.1 --port "$port"', "bindHost=127.0.0.1")
    for listener in listeners:
        assert listener in launcher
    assert launcher.count("--host 127.0.0.1") == 2  # gateway and SearXNG


@posix_only
def test_the_launcher_is_valid_posix_shell(tmp_path: Path) -> None:
    script = tmp_path / "entrypoint.sh"
    script.write_text(backend.LAUNCHER)
    subprocess.run(["sh", "-n", str(script)], check=True)


# --- target-side scripts against fake binaries ------------------------------------------------


def _layout(tmp_path: Path) -> backend.Layout:
    target = InfrastructureTarget(id="node", label="Node", kind="ssh", install_root=str(tmp_path))
    return backend._layout(facts("apptainer"), target, {})  # noqa: SLF001


def _tools(tmp_path: Path, listed_image: str | None, *, children: bool = True) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "calls.txt"
    row = f'echo "clio-web-search    4242          {listed_image}"' if listed_image else ":"
    (bin_dir / "apptainer").write_text(
        "#!/bin/sh\n"
        f'echo "$* | $APPTAINERENV_CLIO_WEB_SEARCH_CONTACT_EMAIL" >> "{calls}"\n'
        'if [ "$1 $2" = "instance list" ]; then\n'
        '  echo "INSTANCE NAME    PID    IP    IMAGE"\n'
        f'  [ -f "{tmp_path}/stopped" ] || {{ {row}; }}\n'
        "fi\n"
        f'if [ "$1 $2" = "instance stop" ]; then touch "{tmp_path}/stopped"; fi\n'
    )
    (bin_dir / "pgrep").write_text(
        f'#!/bin/sh\n[ "$1 $2" = "-P 4242" ] && exit {0 if children else 1}\nexit 2\n'
    )
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def _run(spec: CommandSpec, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [spec.program, *spec.args],
        input=spec.stdin or None,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _calls(tmp_path: Path) -> list[str]:
    path = tmp_path / "calls.txt"
    return path.read_text().splitlines() if path.exists() else []


@posix_only
def test_status_owns_only_the_instance_running_this_deployments_sif(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = tmp_path / "store.sif"
    store.write_text("sif")
    Path(layout.images_dir).mkdir(parents=True)
    os.symlink(store, layout.sif)
    cases = [
        (layout.sif, True, "running"),
        (os.path.realpath(store), True, "running"),  # listed by the resolved store file
        (layout.sif, False, "exited"),
        ("/elsewhere/clio-web-search.sif", True, "foreign_instance"),
        (None, True, "stopped"),
    ]
    for index, (listed, children, expected) in enumerate(cases):
        case = tmp_path / f"case{index}"
        case.mkdir()
        env = _tools(case, listed, children=children)
        assert _run(backend.status_command(layout), env).stdout.strip() == expected
    env = _tools(tmp_path / "case3", "/elsewhere/clio-web-search.sif")
    required = _run(backend.status_command(layout, require=True), env)
    assert required.returncode == 3


@posix_only
def test_stop_leaves_a_foreign_instance_alone_and_stops_the_owned_one(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    env = _tools(foreign, "/elsewhere/clio-web-search.sif")
    refused = _run(backend.stop_owned_command(layout), env)
    assert refused.returncode == 3 and "foreign_instance" in refused.stderr
    assert not any("instance stop" in line for line in _calls(foreign))
    owned = tmp_path / "owned"
    owned.mkdir()
    env = _tools(owned, layout.sif)
    stopped = _run(backend.stop_owned_command(layout), env)
    assert stopped.returncode == 0 and stopped.stdout.strip() == "stopped"
    assert any(line.startswith("instance stop clio-web-search") for line in _calls(owned))


@posix_only
def test_launch_requires_the_receipted_sif_and_passes_the_email_by_environment(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    Path(layout.images_dir).mkdir(parents=True)
    store = tmp_path / "store.sif"
    store.write_bytes(b"image-bytes")
    os.symlink(store, layout.sif)
    env = _tools(tmp_path, None)
    receipt = _run(backend.receipt_command(layout), env)
    digest = hashlib.sha256(b"image-bytes").hexdigest()
    assert receipt.returncode == 0 and f"CLIO_IMAGE_SHA256 {digest}" in receipt.stdout
    assert Path(layout.receipt).read_text().strip() == digest

    ports = backend._ports({})  # noqa: SLF001
    launch = backend.run_command(layout, ports, "alice@example.org")
    assert _run(launch, env).returncode == 0
    started = [line for line in _calls(tmp_path) if line.startswith("instance run")]
    assert len(started) == 1
    arguments, _, email = started[0].partition(" | ")
    assert email == "alice@example.org" and "alice@example.org" not in arguments
    assert f"CLIO_WEB_SEARCH_DEPLOYMENT_ID={layout.deployment_id}" in arguments

    store.write_bytes(b"tampered")
    refused = _run(launch, env)
    assert refused.returncode == 65 and "image_receipt_mismatch" in refused.stderr
    assert len([line for line in _calls(tmp_path) if line.startswith("instance run")]) == 1


@posix_only
def test_readiness_requires_this_deployments_identity(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}

    def answer(ready: bool, deployment: str) -> str:
        body = json.dumps(
            {"service": "clio-web-search", "task_backend": {"deployment_id": deployment}},
            separators=(",", ":"),
        )
        curl.write_text(
            "#!/bin/sh\n"
            'for a; do last="$a"; done\n'
            'case "$last" in\n'
            f"  */readyz) exit {0 if ready else 22} ;;\n"
            "  */v1/capabilities) printf '%s' "
            f"'{body}' ;;\n"
            "esac\n"
        )
        curl.chmod(0o755)
        return _run(backend.identity_command(layout, 8089), env).stdout.strip()

    assert answer(True, layout.deployment_id) == "ready"
    assert answer(True, "clio-ws-000000000000") == "foreign_endpoint"
    assert answer(False, layout.deployment_id) == "waiting"
