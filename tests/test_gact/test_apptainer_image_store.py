"""The Apptainer pull script: a persistent digest-keyed image store, run in a real shell."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from clio_agent.gact.infrastructure.container_runtime import image_store_key, pull_commands

IMAGE = "docker.io/vllm/vllm-openai@sha256:" + "ab" * 32

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("timeout"), reason="POSIX shell script"
)


def _fake_apptainer(bin_dir: Path, calls: Path, *, fail: bool = False) -> None:
    script = bin_dir / "apptainer"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$APPTAINER_CACHEDIR $APPTAINER_TMPDIR $*" >> "{calls}"\n'
        + ("exit 3\n" if fail else 'out=""\n')
        + 'for a; do case "$a" in *.partial) out="$a";; esac; done\n'
        'echo sif > "$out"\n'
    )
    script.chmod(0o755)


def _run(spec, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [spec.program, *spec.args], env=env, capture_output=True, text=True, check=False
    )


def test_a_pull_fills_the_store_links_the_service_sif_and_is_reused(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.txt"
    _fake_apptainer(bin_dir, calls)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    store, images, scratch = tmp_path / "store", tmp_path / "svc/images", tmp_path / "scratch"
    images.mkdir(parents=True)
    first, second = pull_commands(
        "apptainer", IMAGE, str(images), str(store), "clio-vllm", str(scratch)
    )

    assert _run(first, env).returncode == 0
    assert _run(second, env).returncode == 0

    sif = store / f"{image_store_key(IMAGE)}.sif"
    assert sif.read_text() == "sif\n"
    assert (store / f"{sif.name}.ref").read_text() == IMAGE + "\n"
    assert os.readlink(images / "clio-vllm.sif") == str(sif)
    # One real pull (the second attempt reused it), with the layer cache in the
    # store and the conversion scratch in the target's temporary location.
    lines = calls.read_text().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"{store}/cache {scratch} pull --force {sif}.partial")

    # A new service (or a new node sharing the store) reuses it too.
    other = tmp_path / "svc2/images"
    other.mkdir(parents=True)
    again = pull_commands("apptainer", IMAGE, str(other), str(store), "clio-vllm", str(scratch))
    assert "reusing" in _run(again[0], env).stdout
    assert len(calls.read_text().splitlines()) == 1


def test_a_failed_pull_keeps_the_store_and_publishes_no_sif(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_apptainer(bin_dir, tmp_path / "calls.txt", fail=True)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    store, images = tmp_path / "store", tmp_path / "svc/images"
    images.mkdir(parents=True)
    first = pull_commands("apptainer", IMAGE, str(images), str(store), "clio-vllm")[0]

    assert _run(first, env).returncode not in first.allowed_exit_codes
    assert (store / "cache").is_dir()
    assert not (store / f"{image_store_key(IMAGE)}.sif.ref").exists()
    assert not (images / "clio-vllm.sif").exists()


def test_a_stale_sidecar_forces_a_fresh_pull(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.txt"
    _fake_apptainer(bin_dir, calls)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    store, images = tmp_path / "store", tmp_path / "svc/images"
    images.mkdir(parents=True)
    store.mkdir()
    key = image_store_key(IMAGE)
    (store / f"{key}.sif").write_text("old\n")  # no sidecar: an interrupted earlier pull
    first = pull_commands("apptainer", IMAGE, str(images), str(store), "clio-vllm")[0]

    assert _run(first, env).returncode == 0
    assert (store / f"{key}.sif").read_text() == "sif\n"
    assert len(calls.read_text().splitlines()) == 1
