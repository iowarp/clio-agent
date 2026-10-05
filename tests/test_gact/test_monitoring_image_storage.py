"""Deployment-owned image storage must isolate every engine and download command."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.infrastructure import node_service_stack
from tests.test_gact.test_monitoring_services import manifest


def test_service_folder_requires_podman() -> None:
    """Docker data volumes cannot be misrepresented as a relocated image store."""
    with pytest.raises(ValueError, match="require Podman"):
        manifest("cmf", image_storage="service", container_runtime="docker")
    with pytest.raises(ValueError, match="Choose engine storage"):
        manifest("cmf", image_storage="arbitrary")
    config = manifest("cmf", image_storage="service", container_runtime="podman")
    assert config["image_storage"] == "service"


def test_redis_private_configuration_remains_readable_by_the_mapped_owner() -> None:
    """The image entrypoint must not switch away from the selected host owner's UID."""
    redis = manifest("flowcept", container_runtime="podman")["components"][0]
    assert redis["entrypoint"] == "redis-server"
    assert redis["arguments"] == ["/etc/redis.conf"]
    assert redis["secrets"] == ["REDISCLI_AUTH"]
    assert redis["check"] == ["sh", "-c", 'test "$(redis-cli --raw ping)" = PONG']


def test_commands_use_owned_image_runtime_and_download_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pull, inspect and lifecycle commands all use the same store after reconnect."""
    (tmp_path / "manifest.json").write_text(
        json.dumps({"container_runtime": "podman", "image_storage": "service"})
    )
    monkeypatch.setenv("CONTAINER_HOST", "ssh://another-machine")
    monkeypatch.setenv("CONTAINER_CONNECTION", "unrelated")
    monkeypatch.setenv("CONTAINERS_STORAGE_CONF", "/unrelated/storage.conf")
    monkeypatch.setenv("TMPDIR", "/var/tmp")
    monkeypatch.setattr(
        node_service_stack, "podman_runroot", lambda _: Path("/run/user/1000/clio-owned")
    )

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert arguments[:2] == ["podman", "--remote=false"]
        assert arguments[2:6] == [
            "--root",
            str(tmp_path / "containers/images"),
            "--runroot",
            str(Path("/run/user/1000/clio-owned")),
        ]
        environment = kwargs["env"]
        assert environment["TMPDIR"] == str(tmp_path / "containers/downloads")
        assert not any(
            key in environment
            for key in ("CONTAINER_HOST", "CONTAINER_CONNECTION", "CONTAINERS_STORAGE_CONF")
        )
        return subprocess.CompletedProcess(arguments, 0, stdout="owned", stderr="")

    monkeypatch.setattr(node_service_stack.subprocess, "run", run)
    for arguments in (["pull", "image"], ["ps", "-a"], ["network", "ls"]):
        assert node_service_stack.command(tmp_path, arguments) == "owned"


def test_engine_storage_preserves_existing_configuration(tmp_path: Path) -> None:
    """Old manifests and explicit engine storage keep their established semantics."""
    env = {"TMPDIR": "/configured", "CONTAINER_HOST": "ssh://configured"}
    for engine in ("docker", "podman"):
        program, actual = node_service_stack.engine_invocation(
            tmp_path, {"container_runtime": engine}, env
        )
        assert program == [engine] and actual is env
    assert not (tmp_path / "containers").exists()


def test_store_path_redirection_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse a symlinked ancestor without creating files through it."""
    resolve = Path.resolve

    def redirected(path: Path, *args: Any, **kwargs: Any) -> Path:
        if "containers" in path.parts:
            return tmp_path / "foreign"
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected)
    with pytest.raises(ValueError, match="cannot leave"):
        node_service_stack.engine_invocation(
            tmp_path, {"container_runtime": "podman", "image_storage": "service"}, None
        )
    assert not (tmp_path / "foreign").exists()


def test_volatile_runtime_state_is_owned_and_reconnectable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Short runtime paths survive reconnect but cannot adopt another owner's state."""
    uid = tmp_path.stat().st_uid
    monkeypatch.setattr(node_service_stack.os, "getuid", lambda: uid, raising=False)
    monkeypatch.setattr(node_service_stack, "RUNTIME_PARENT", tmp_path / "run")
    (tmp_path / "run" / str(uid)).mkdir(parents=True)
    root = tmp_path / "long-service-folder" / ("nested" * 5)
    root.mkdir(parents=True)
    (root / "owner.json").write_text(json.dumps({"owner": "ours", "root": str(root)}))
    runtime = node_service_stack.podman_runroot(root)
    assert runtime.parent == tmp_path / "run" / str(uid)
    assert len(runtime.name) == 21
    assert node_service_stack.podman_runroot(root) == runtime
    (runtime / "clio-owner.json").write_text(json.dumps({"owner": "another"}))
    with pytest.raises(ValueError, match="another deployment"):
        node_service_stack.podman_runroot(root)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configuration",
    [
        {"container_runtime": "podman"},
        {"image_storage": "service"},
    ],
)
async def test_reinstall_cannot_orphan_an_established_image_store(
    tmp_path: Path, configuration: dict[str, str]
) -> None:
    """The runtime rejects storage changes before it stops or deploys anything."""
    from clio_agent.gact.infrastructure.models import ServiceActionRequest, ServiceRecord
    from tests.test_gact.test_managed_service_cleanup import FakeLinuxTarget, _finish, _runtime

    target = FakeLinuxTarget()
    runtime, store, target_id = _runtime(tmp_path, target)
    record = ServiceRecord(
        id=f"{target_id}:cmf",
        service_id="cmf",
        target_id=target_id,
        variant_id="managed",
        configuration={
            "storage.service_directory": "/data/owned/cmf",
            "container_runtime": "docker",
        },
    )
    store.put_service(record)
    before = target.snapshot()
    result = await _finish(
        runtime,
        store,
        "cmf",
        ServiceActionRequest(
            target_id=target_id,
            action="reinstall",
            variant_id="managed",
            configuration=configuration,
        ),
    )
    assert result.state == "failed" and "owns its container runtime" in result.error
    assert target.snapshot() == before
    assert store.service(target_id, "cmf").configuration == record.configuration


def test_source_build_changes_only_the_declared_retired_base(tmp_path: Path) -> None:
    """The qualified base override is explicit and never mutates the pinned checkout."""
    build = manifest("cmf")["build"]
    upstream = tmp_path / "upstream.Dockerfile"
    original = f"# Copyright HPE\nFROM {build['upstream_base']}\nRUN echo unchanged\n"
    upstream.write_text(original)
    result = node_service_stack.build_definition(tmp_path, upstream, build)
    assert upstream.read_text() == original
    assert result.read_text() == original.replace(build["upstream_base"], build["base_image"])
    upstream.write_text("FROM unexpected\n")
    with pytest.raises(ValueError, match="compatibility profile"):
        node_service_stack.build_definition(tmp_path, upstream, build)


def test_engine_failure_retains_bounded_redacted_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed install has actionable local logs without exposing generated credentials."""
    (tmp_path / "manifest.json").write_text(json.dumps({"container_runtime": "docker"}))
    (tmp_path / "credentials.json").write_text(json.dumps({"password": "not-for-the-transcript"}))
    monkeypatch.setattr(
        node_service_stack.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 125, stdout="x" * 20000, stderr="build failed not-for-the-transcript"
        ),
    )
    with pytest.raises(RuntimeError, match="Container build failed") as error:
        node_service_stack.command(tmp_path, ["build"])
    assert "not-for-the-transcript" not in str(error.value)
    log = (tmp_path / "logs/container-engine.log").read_text()
    assert "not-for-the-transcript" not in log
    assert log.endswith("build failed [redacted]") and len(log) <= 16000


def test_data_deletion_never_cleans_a_shared_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an explicit owned-store deletion can remove images, never a shared Docker store."""
    calls: list[list[str]] = []
    rows = iter(["", "sha256:owned", "", ""])

    def command(root: Path, arguments: list[str]) -> str:
        calls.append(arguments)
        return next(rows)

    monkeypatch.setattr(node_service_stack, "command", command)
    node_service_stack.delete_images(tmp_path, {"image_storage": "engine"})
    assert calls == []
    runtime = tmp_path / "volatile"
    runtime.mkdir()
    monkeypatch.setattr(node_service_stack, "podman_runroot", lambda _: runtime)
    node_service_stack.delete_images(tmp_path, {"image_storage": "service"})
    assert ["rmi", "--all"] in calls and not runtime.exists()


def test_data_deletion_refuses_remaining_containers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even an unexpected container in the private store blocks deletion."""
    monkeypatch.setattr(node_service_stack, "command", lambda *args: "remaining")
    with pytest.raises(ValueError, match="still has containers"):
        node_service_stack.delete_images(tmp_path, {"image_storage": "service"})


def test_legacy_cni_network_ownership_is_checked_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Podman 3 stores network labels inside its CNI args, not Docker's envelope."""
    (tmp_path / "owner.json").write_text(json.dumps({"owner": "our-owner"}))
    replies = iter(
        [
            "owned-network",
            json.dumps(
                [{"args": {"podman_labels": {node_service_stack.OWNER_LABEL: "our-owner"}}}]
            ),
        ]
    )
    monkeypatch.setattr(node_service_stack, "command", lambda *_: next(replies))
    assert node_service_stack.inspect(tmp_path, "network", "owned-network") is not None
    foreign = iter(
        [
            "owned-network",
            json.dumps(
                [{"args": {"podman_labels": {node_service_stack.OWNER_LABEL: "someone-else"}}}]
            ),
        ]
    )
    monkeypatch.setattr(node_service_stack, "command", lambda *_: next(foreign))
    with pytest.raises(ValueError, match="another deployment"):
        node_service_stack.inspect(tmp_path, "network", "owned-network")


def test_rootless_pod_uses_private_network_and_publishes_only_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep ports on the owned pod and database traffic inside its network namespace."""
    config = manifest("cmf", container_runtime="podman")
    (tmp_path / "owner.json").write_text(json.dumps({"owner": "our-owner"}))
    (tmp_path / "images.json").write_text(
        json.dumps(
            {
                **{row["name"]: row["image"] for row in config["components"]},
                "pod_infra": config["pod_infra_image"],
            }
        )
    )
    calls: list[list[str]] = []

    def command(root: Path, args: list[str], **kwargs: object) -> str:
        calls.append(args)
        return json.dumps({"host": {"security": {"rootless": True}}}) if args[0] == "info" else ""

    monkeypatch.setattr(node_service_stack, "command", command)
    monkeypatch.setattr(node_service_stack, "inspect", lambda *_: None)
    monkeypatch.setattr(node_service_stack.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(node_service_stack.os, "getgid", lambda: 1000, raising=False)
    node_service_stack.start(tmp_path, config)
    pod = next(row for row in calls if row[:2] == ["pod", "create"])
    assert pod[pod.index("--network") + 1] == "slirp4netns"
    assert pod[pod.index("--publish") + 1] == "127.0.0.1:8380:8080"
    runs = [row for row in calls if row[0] == "run"]
    assert len(runs) == 2
    assert all("--pod" in row and "--publish" not in row and "--network" not in row for row in runs)
    assert all(row[row.index("--user") + 1] == "0:0" for row in runs)
    assert "POSTGRES_HOST=127.0.0.1" in runs[1]


def test_pod_cleanup_refuses_unexpected_containers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pod's ownership label cannot authorize stopping an unrelated member."""
    config = manifest("cmf", container_runtime="podman")
    calls: list[list[str]] = []
    monkeypatch.setattr(node_service_stack, "collect_logs", lambda *_: None)
    monkeypatch.setattr(node_service_stack, "command", lambda root, args: calls.append(args))
    monkeypatch.setattr(
        node_service_stack,
        "inspect",
        lambda root, kind, name: {
            "InfraContainerID": "infra",
            "Containers": [{"Id": "other", "Name": "foreign"}],
        }
        if kind == "pod"
        else None,
    )
    with pytest.raises(ValueError, match="unexpected container"):
        node_service_stack.cleanup(tmp_path, config, remove=True)
    assert calls == []


def test_explicit_rootless_data_deletion_stays_inside_owned_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Database UID mappings need unshare; shared image stores are not reset or pruned."""
    config = manifest("cmf", container_runtime="podman")
    data = tmp_path / "data"
    data.mkdir()
    calls: list[list[str]] = []
    monkeypatch.setattr(node_service_stack, "inspect", lambda *_: None)

    def command(root: Path, args: list[str]) -> str:
        calls.append(args)
        if args[0] == "info":
            return json.dumps({"host": {"security": {"rootless": True}}})
        assert args[:3] == ["unshare", "python3", "-c"] and args[-1] == str(data)
        data.rmdir()
        return ""

    monkeypatch.setattr(node_service_stack, "command", command)
    node_service_stack.delete_podman_data(tmp_path, config)
    assert not data.exists() and len(calls) == 2
