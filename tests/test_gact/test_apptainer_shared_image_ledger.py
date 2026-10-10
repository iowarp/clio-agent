"""The shared Apptainer image store is pruned through the ledger, ownership-scoped.

Every service whose pull reported a SIF (``CLIO_SHARED_IMAGE``) holds it; its
uninstall deletes it only when no other service on the target holds it, and
the store's layer cache goes with the last SIF. Run against a real shell with
a fake ``apptainer``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from clio_agent.gact.infrastructure.container_runtime import image_store_key, pull_commands
from clio_agent.gact.infrastructure.deployment_ledger import held_elsewhere
from clio_agent.gact.infrastructure.models import CommandResult, OwnedResource, ServiceRecord
from clio_agent.gact.infrastructure.resource_ledger import (
    removal_commands,
    shared_image_recorder,
    unheld,
)
from clio_agent.gact.infrastructure.store import InfrastructureStore

IMAGE = "ghcr.io/ggml-org/llama.cpp@sha256:" + "cd" * 32
OTHER = "docker.io/vllm/vllm-openai@sha256:" + "ef" * 32

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("timeout"), reason="POSIX shell script"
)


def _env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "apptainer"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$APPTAINER_CACHEDIR $*" >> "{tmp_path / "calls.txt"}"\n'
        'out=""; for a; do case "$a" in *.partial) out="$a";; esac; done\n'
        '[ -n "$out" ] && { mkdir -p "$APPTAINER_CACHEDIR/blob"; echo sif > "$out"; }\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def _run(spec, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [spec.program, *spec.args], env=env, capture_output=True, text=True, check=False
    )


def _pull(tmp_path: Path, env: dict[str, str], image: str, name: str) -> list[OwnedResource]:
    images = tmp_path / name / "images"
    images.mkdir(parents=True, exist_ok=True)
    rows: list[OwnedResource] = []
    for spec in pull_commands("apptainer", image, str(images), str(tmp_path / "store"), name):
        done = _run(spec, env)
        assert done.returncode == 0, done.stderr
        rows = shared_image_recorder()(CommandResult(exit_code=0, stdout=done.stdout))
    return rows


def test_a_pull_reports_the_sif_it_used_pulled_or_reused(tmp_path: Path) -> None:
    env = _env(tmp_path)
    sif = str(tmp_path / "store" / f"{image_store_key(IMAGE)}.sif")

    rows = _pull(tmp_path, env, IMAGE, "a")
    assert [(row.kind, row.ref, row.runtime) for row in rows] == [
        ("shared_image", sif, "apptainer")
    ]
    assert [row.ref for row in _pull(tmp_path, env, IMAGE, "b")] == [sif]


def test_the_recorder_ignores_anything_but_an_absolute_sif() -> None:
    out = "CLIO_SHARED_IMAGE relative.sif\nCLIO_SHARED_IMAGE /store/x.img\nnoise\n"
    assert shared_image_recorder()(CommandResult(exit_code=0, stdout=out)) == []


def test_a_held_image_stays_and_the_last_holder_removes_it_and_the_cache(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    store = tmp_path / "store"
    llama = _pull(tmp_path, env, IMAGE, "a")
    _pull(tmp_path, env, IMAGE, "b")  # a second service holds the same SIF
    vllm = _pull(tmp_path, env, OTHER, "c")
    assert (store / "cache").is_dir()

    # Uninstall of "a" while "b" holds it: nothing to remove.
    assert removal_commands(unheld(llama, {llama[0].ref}), "linux") == []

    # The last holder of the llama SIF: deleted, the vLLM SIF and the cache stay.
    for spec in removal_commands(unheld(llama, set()), "linux"):
        assert _run(spec, env).returncode == 0
    assert not Path(llama[0].ref).exists()
    assert not Path(llama[0].ref + ".ref").exists()
    assert Path(vllm[0].ref).exists()
    assert (store / "cache").is_dir()

    # The last SIF in the store: the layer cache goes too (the store dir stays).
    for spec in removal_commands(vllm, "linux"):
        assert _run(spec, env).returncode == 0
    assert not (store / "cache").exists()
    assert store.is_dir()
    assert "cache clean -f" in (tmp_path / "calls.txt").read_text()


def test_an_unsafe_shared_image_ref_is_never_removed() -> None:
    rows = [
        OwnedResource(kind="shared_image", ref="/", runtime="apptainer"),
        OwnedResource(kind="shared_image", ref="/a/b/not-a-sif", runtime="apptainer"),
    ]
    assert removal_commands(rows, "linux") == []


def _record(service_id: str, refs: list[str], target_id: str = "local") -> ServiceRecord:
    return ServiceRecord(
        id=f"{target_id}:{service_id}",
        service_id=service_id,
        target_id=target_id,
        variant_id="cuda",
        owned_resources=[
            OwnedResource(kind="shared_image", ref=ref, runtime="apptainer") for ref in refs
        ],
    )


def test_held_elsewhere_counts_other_services_on_the_same_target(tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infra.json")
    store.put_service(_record("llama_cpp", ["/s/a.sif"]))
    store.put_service(_record("vllm", ["/s/b.sif"]))
    store.put_service(_record("ollama", ["/s/c.sif"], target_id="remote"))

    assert held_elsewhere(store, "local", "llama_cpp") == {"/s/b.sif"}
    assert held_elsewhere(store, "local", "ollama") == {"/s/a.sif", "/s/b.sif"}
