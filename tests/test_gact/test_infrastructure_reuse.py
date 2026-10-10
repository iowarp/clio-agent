"""The shared reuse preflight: verified identities only, reported, and bypassable."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.infrastructure import reuse
from clio_agent.gact.infrastructure.container_runtime import (
    image_reuse_check,
    image_store_key,
    pull_commands,
)
from clio_agent.gact.infrastructure.models import CommandResult

IMAGE = "localhost:1/clio/vllm@sha256:" + "ab" * 32
posix_only = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("timeout"), reason="POSIX shell script"
)


def test_a_marker_reuses_only_its_exact_identity(tmp_path: Path) -> None:
    marker = tmp_path / "env/.clio-installed"
    marker.parent.mkdir()
    python = tmp_path / "env/bin/python"

    assert not reuse.check_marker(marker, "sha256:1 rev")  # missing
    reuse.write_marker(marker, "sha256:1 rev", 42.0)
    assert reuse.check_marker(marker, "sha256:1 rev")  # match
    assert not reuse.check_marker(marker, "sha256:2 rev")  # mismatch
    assert not reuse.check_marker(marker, "sha256:1 rev", required=[python])  # incomplete
    python.parent.mkdir()
    python.write_text("")
    assert reuse.check_marker(marker, "sha256:1 rev", required=[python])
    assert reuse.read_marker(marker) == ("sha256:1 rev", 42.0)


def test_a_plain_text_marker_from_before_the_helper_is_its_identity(tmp_path: Path) -> None:
    marker = tmp_path / ".clio-installed"
    marker.write_text("sha256:abc 0123")
    assert reuse.read_marker(marker) == ("sha256:abc 0123", None)
    assert reuse.check_marker(marker, "sha256:abc 0123")
    assert not reuse.check_marker(marker, "sha256:abc 9999")


def test_a_sif_is_reused_only_with_a_sidecar_naming_exactly_its_image(tmp_path: Path) -> None:
    sif = tmp_path / "server.sif"
    assert not reuse.sif_matches(sif, IMAGE)  # missing
    sif.write_text("sif")
    assert not reuse.sif_matches(sif, IMAGE)  # no sidecar: an interrupted pull
    reuse.record_sif(sif, IMAGE, 300.0)
    assert reuse.sif_matches(sif, IMAGE)
    assert not reuse.sif_matches(sif, IMAGE.replace("ab", "cd"))  # another digest
    assert reuse.sif_seconds(sif) == 300.0


def test_a_uv_environment_matches_project_lock_and_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = tmp_path / "environment"
    (environment / ".venv/bin").mkdir(parents=True)
    (environment / ".venv/bin/python").write_text("")
    (environment / "pyproject.toml").write_text('[project]\nname="x"\n')
    (environment / "uv.lock").write_text("lock-1")
    monkeypatch.delenv(reuse.FROM_SCRATCH_ENV, raising=False)

    assert reuse.uv_environment_reuse(environment, "vllm-1") == ""  # never recorded
    reuse.record_uv_environment(environment, "vllm-1", 120.0)
    [found] = reuse.parse(reuse.uv_environment_reuse(environment, "vllm-1"))
    assert found.kind == "uv_environment" and found.saved_seconds == 120.0
    assert reuse.uv_environment_reuse(environment, "vllm-2") == ""  # another profile
    (environment / "uv.lock").write_text("lock-2")
    assert reuse.uv_environment_reuse(environment, "vllm-1") == ""  # another lock

    # Installing from scratch bypasses the match and removes the venv.
    (environment / "uv.lock").write_text("lock-1")
    monkeypatch.setenv(reuse.FROM_SCRATCH_ENV, "1")
    assert reuse.uv_environment_reuse(environment, "vllm-1") == ""
    assert not (environment / ".venv").exists()


def test_a_reuse_line_round_trips_and_reads_for_people() -> None:
    found = reuse.Reuse(
        kind="sif",
        thing="container image",
        identity="sha256:" + "0123456789abcdef" * 4,
        path="/s/x.sif",
        size_bytes=8_100_000_000,
        saved_seconds=420,
    )
    assert reuse.parse("noise\n" + reuse.line(found) + "\nmore") == [found]
    assert found.message() == "Reusing container image (sha256:0123456789ab); skipped 8.1 GB/~7m"
    assert reuse.Reuse("package", "Relay", "clio-relay==1").message() == (
        "Reusing Relay (clio-relay==1)"
    )


@posix_only
def test_the_shell_twin_prints_the_same_parseable_line() -> None:
    script = reuse.SHELL_FUNCTIONS + 'clio_reuse sif "an \\"image\\"" "sha256:ab" /s/x.sif 10 -\n'
    done = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=True)
    [found] = reuse.parse(done.stdout)
    assert found.thing == 'an "image"' and found.size_bytes == 10 and found.saved_seconds is None


def test_the_from_scratch_flag_is_operation_scoped() -> None:
    assert reuse.from_scratch({reuse.FROM_SCRATCH_KEY: "true"})
    assert not reuse.from_scratch({reuse.FROM_SCRATCH_KEY: "false"})
    assert not reuse.from_scratch({})
    assert reuse.without_transient({reuse.FROM_SCRATCH_KEY: "true", "model": "m"}) == {"model": "m"}


def _fake_apptainer(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "apptainer"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{tmp_path / "calls.txt"}"\n'
        'out=""; for a; do case "$a" in *.partial) out="$a";; esac; done\n'
        'echo sif > "$out"\n'
    )
    script.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def _run(spec: Any, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [spec.program, *spec.args], env=env, capture_output=True, text=True, check=False
    )


@posix_only
def test_the_apptainer_store_hit_is_reported_and_from_scratch_pulls_again(
    tmp_path: Path,
) -> None:
    env = _fake_apptainer(tmp_path)
    store, images = tmp_path / "store", tmp_path / "svc/images"
    images.mkdir(parents=True)
    pulls = pull_commands("apptainer", IMAGE, str(images), str(store), "clio-vllm")
    for spec in pulls:
        done = _run(spec, env)
        assert done.returncode == 0, done.stderr
        assert reuse.parse(done.stdout) == []  # a cold pull, then its quiet second attempt
    assert (store / f"{image_store_key(IMAGE)}.sif.took").is_file()

    [found] = reuse.parse(_run(pulls[0], env).stdout)
    assert found.kind == "sif" and found.identity == IMAGE and found.size_bytes == 4
    assert len((tmp_path / "calls.txt").read_text().splitlines()) == 1

    fresh = pull_commands("apptainer", IMAGE, str(images), str(store), "clio-vllm", fresh=True)
    for spec in fresh:
        done = _run(spec, env)
        assert done.returncode == 0 and reuse.parse(done.stdout) == []
    calls = (tmp_path / "calls.txt").read_text().splitlines()
    assert len(calls) == 2 and "--disable-cache" in calls[1]


def test_a_docker_image_is_reused_only_under_its_pinned_digest() -> None:
    image = "ghcr.io/ggml-org/llama.cpp@sha256:" + "cd" * 32
    check = image_reuse_check("docker", image, (5,))
    assert check is not None and check.skip == (5,)
    inspected = json.dumps([{"Id": "sha256:1", "Size": 2_000_000_000}])
    found = check.report(CommandResult(exit_code=0, stdout=inspected))
    assert found is not None and found.identity == "sha256:" + "cd" * 32
    assert found.size_bytes == 2_000_000_000
    assert check.report(CommandResult(exit_code=1, stderr="No such image")) is None  # missing
    assert image_reuse_check("docker", "ghcr.io/iowarp/clio-web-search:0.3.1", (1,)) is None
    assert image_reuse_check("apptainer", image, (1,)) is None  # the store handles it


def test_the_cmf_environment_rebuilds_from_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.infrastructure import node_service_stack_apptainer as backend
    from tests.test_gact.test_monitoring_apptainer import apptainer_manifest, owned_root

    root = owned_root(tmp_path)
    manifest = apptainer_manifest("cmf")
    (root / "cmf-server.lock").write_text(manifest["files"]["cmf-server.lock"])
    (root / "source").mkdir()
    calls: list[list[str]] = []

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        python = root / "cmf-venv/bin/python"
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(backend, "streamed", lambda _root, command, _env, _timeout: run(command))
    monkeypatch.delenv(reuse.FROM_SCRATCH_ENV, raising=False)
    backend.install_environment(root, manifest)
    backend.install_environment(root, manifest)
    assert len(calls) == 1
    monkeypatch.setenv(reuse.FROM_SCRATCH_ENV, "1")
    backend.install_environment(root, manifest)
    assert len(calls) == 2


def test_a_supervised_install_ships_the_helper_and_streams_its_install_log() -> None:
    from tests.test_gact.test_native_services import plan

    install = plan()
    body = json.loads(install.commands[-1].stdin)
    assert body["reuse_helper"] == reuse.source() and "from_scratch" not in body
    assert install.readiness is not None
    assert install.readiness.log_path.endswith("/logs/install.log")
    fresh = json.loads(plan(**{reuse.FROM_SCRATCH_KEY: "true"}).commands[-1].stdin)
    assert fresh["from_scratch"] is True
    # The bypass is not part of the manifest, so a later start still matches it.
    assert fresh["manifest"] == body["manifest"]
    start = plan("start")
    assert start.readiness is not None and start.readiness.log_path.endswith("/server.log")


@posix_only
def test_an_installed_clio_of_this_version_is_reused(tmp_path: Path) -> None:
    from clio_agent.gact.infrastructure.clio_agent_deploy import install_command

    root = tmp_path / "clio"
    python = root / "clio-agent/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\necho 0.9.4.18\n")
    python.chmod(0o755)
    (root / "bin").mkdir()
    (root / "bin/clio").write_text("#!/bin/sh\n")
    (root / "bin/clio").chmod(0o755)

    spec = install_command(str(root), "0.9.4.18")
    done = subprocess.run(
        [spec.program, *spec.args], capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stderr
    [found] = reuse.parse(done.stdout)
    assert found.kind == "clio_agent" and found.identity == "clio-agent==0.9.4.18"
    assert install_command(str(root), "0.9.4.18", fresh=True).args[-1] == "1"


def test_relay_reports_uvs_own_already_installed_verdict() -> None:
    from clio_agent.gact.infrastructure.drivers import RELAY_VERSION, _relay_plan
    from clio_agent.gact.infrastructure.models import InfrastructureTarget, SshRoute

    target = InfrastructureTarget(
        id="hpc", label="HPC", kind="ssh", ssh=SshRoute(host="login.example", user="alice")
    )
    configuration = {
        "cluster_name": "hpc",
        "agent_bin": "/opt/agent",
        "relay_artifact_sha256": "a" * 64,
    }
    plan = _relay_plan("install", configuration, target)
    check = plan.reuse_checks[0]
    assert check.skip == ()
    already = f"`clio-relay=={RELAY_VERSION}` is already installed"
    verdict = CommandResult(exit_code=0, stderr=already)
    found = check.report(verdict)
    assert found is not None and found.identity == f"clio-relay=={RELAY_VERSION}"
    assert check.report(CommandResult(exit_code=0, stdout="Installed 1 executable")) is None
    fresh = _relay_plan("install", {**configuration, reuse.FROM_SCRATCH_KEY: "1"}, target)
    assert "--reinstall" in fresh.commands[0].args and "--reinstall" not in plan.commands[0].args
