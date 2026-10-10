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
    # Filesystem facts for clio_local_scratch: paths under $FAKE_NETWORK_FS are
    # "lustre", all others local; df reports $FAKE_FREE_KB free (default plenty).
    stat = bin_dir / "stat"
    stat.write_text(
        "#!/bin/sh\n"
        "for p; do :; done\n"
        'if [ -n "${FAKE_NETWORK_FS:-}" ]; then\n'
        '  case "$p" in "$FAKE_NETWORK_FS"*) echo lustre; exit 0 ;; esac\n'
        "fi\n"
        "echo xfs\n"
    )
    df = bin_dir / "df"
    df.write_text(
        "#!/bin/sh\n"
        'printf "Filesystem 1024-blocks Used Available Capacity Mounted\\n"\n'
        'printf "x 1 1 %s 1%%%% /\\n" "${FAKE_FREE_KB:-999999999}"\n'
    )
    stat.chmod(0o755)
    df.chmod(0o755)


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


def _network_scratch_pull(tmp_path: Path, free_kb: str) -> tuple[Path, Path, Path, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.txt"
    _fake_apptainer(bin_dir, calls)
    local = tmp_path / "local"
    local.mkdir()
    store, images, scratch = tmp_path / "store", tmp_path / "svc/images", tmp_path / "net/tmp"
    images.mkdir(parents=True)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": str(local),
        "FAKE_NETWORK_FS": str(tmp_path / "net"),
        "FAKE_FREE_KB": free_kb,
    }
    first, _ = pull_commands("apptainer", IMAGE, str(images), str(store), "clio-vllm", str(scratch))
    result = _run(first, env)
    assert result.returncode == 0, result.stderr
    return local, scratch, store, calls.read_text() + result.stdout + result.stderr


def test_a_network_temporary_converts_on_node_local_scratch(tmp_path: Path) -> None:
    local, scratch, store, output = _network_scratch_pull(tmp_path, "999999999")

    used = output.splitlines()[0].split()[1]
    assert used.startswith(f"{local}/clio-apptainer-")
    assert f"clio: Using node-local scratch {used} (target temporary is on lustre)" in output
    # The conversion directory is removed; the layer cache stays in the store.
    assert not Path(used).exists()
    assert output.splitlines()[0].startswith(f"{store}/cache ")
    assert (scratch / f".clio-pulled-{image_store_key(IMAGE)}").is_file()


def test_no_room_on_local_scratch_falls_back_with_a_warning(tmp_path: Path) -> None:
    local, scratch, _, output = _network_scratch_pull(tmp_path, "1000")

    assert output.splitlines()[0].split()[1] == str(scratch)
    assert "is on lustre and no node-local scratch has room" in output
    assert list(local.iterdir()) == []
