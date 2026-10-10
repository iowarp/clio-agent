"""Offline document packages retain lock integrity and install with real uv."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import zipfile
from pathlib import Path
from typing import Any

import pytest

from clio_agent.runtime.document_wheels import bundled_wheel_requirements


def wheel_bundle(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, Any]]:
    """Create a valid tiny wheel and its lock, using only test-owned files."""
    runtime, stack, project = (tmp_path / name for name in ("runtime", "stack", "project"))
    directory = runtime / "document-wheels"
    for path in (directory, stack, project):
        path.mkdir(parents=True)
    filename = "clio_offline_fixture-1.0-py3-none-any.whl"
    wheel = directory / filename
    info = "clio_offline_fixture-1.0.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("clio_offline_fixture.py", "VALUE = 'installed offline'\n")
        archive.writestr(
            f"{info}/METADATA", "Metadata-Version: 2.1\nName: clio-offline-fixture\nVersion: 1.0\n"
        )
        archive.writestr(
            f"{info}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        )
        archive.writestr(f"{info}/RECORD", "")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    (stack / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "clio-offline-fixture"\nversion = "1.0"\n'
        'source = { registry = "https://example.invalid/simple" }\n'
        f'wheels = [{{hash = "sha256:{digest}"}}]\n',
        encoding="utf-8",
    )
    manifest = {
        "schema": 1,
        "lock_sha256": hashlib.sha256((stack / "uv.lock").read_bytes()).hexdigest(),
        "cache_tag": sys.implementation.cache_tag,
        "platform": sysconfig.get_platform(),
        "packages": [
            {
                "name": "clio-offline-fixture",
                "version": "1.0",
                "filename": filename,
                "sha256": digest,
                "source_hash": f"sha256:{digest}",
            }
        ],
    }
    write_manifest(runtime, manifest)
    return runtime, stack, project, manifest


def write_manifest(runtime: Path, manifest: dict[str, Any]) -> None:
    """Write a fixture bundle's manifest after a deliberate test mutation."""
    (runtime / "document-wheels/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize(
    "damage",
    [
        "lock",
        "interpreter",
        "platform",
        "schema",
        "missing",
        "duplicate",
        "version",
        "source",
        "null-source",
        "path",
        "checksum",
    ],
)
def test_rejects_changed_incomplete_or_foreign_bundles(tmp_path: Path, damage: str) -> None:
    runtime, stack, project, manifest = wheel_bundle(tmp_path)
    entry = manifest["packages"][0]
    if damage == "lock":
        (stack / "uv.lock").write_text("version = 99\n")
    elif damage in {"interpreter", "platform", "schema"}:
        manifest[{"interpreter": "cache_tag"}.get(damage, damage)] = "different"
    elif damage == "missing":
        manifest["packages"] = []
    elif damage == "duplicate":
        manifest["packages"].append(entry.copy())
    elif damage == "version":
        entry["version"] = "2.0"
    elif damage in {"source", "null-source"}:
        entry["source_hash"] = None if damage == "null-source" else "sha256:" + "0" * 64
    elif damage == "path":
        entry["filename"] = "../escape.whl"
    else:
        (runtime / "document-wheels" / entry["filename"]).write_bytes(b"damaged")
    write_manifest(runtime, manifest)
    with pytest.raises(ValueError, match="Bundled document wheel"):
        bundled_wheel_requirements(runtime, stack, project)
    assert not (project / "bundled-requirements.txt").exists()


def test_unbundled_development_environment_keeps_frozen_sync(tmp_path: Path) -> None:
    assert bundled_wheel_requirements(None, tmp_path, tmp_path) is None
    assert bundled_wheel_requirements(tmp_path, tmp_path, tmp_path) is None


def test_verified_bundle_installs_offline_in_real_isolated_interpreter(tmp_path: Path) -> None:
    """Exercise uv's actual hash verification, wheel install, and module import."""
    runtime, stack, project, _ = wheel_bundle(tmp_path)
    requirements = bundled_wheel_requirements(runtime, stack, project)
    uv = shutil.which("uv")
    assert uv is not None
    environment = tmp_path / "environment"
    env = {**os.environ, "UV_CACHE_DIR": str(tmp_path / "uv-cache"), "UV_OFFLINE": "1"}
    subprocess.run(
        [uv, "venv", "--no-managed-python", "--python", sys.executable, str(environment)],
        env=env,
        check=True,
        capture_output=True,
        timeout=30,
    )
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [
            uv,
            "pip",
            "sync",
            "--python",
            str(python),
            str(requirements),
            "--no-index",
            "--find-links",
            str(runtime / "document-wheels"),
            "--require-hashes",
            "--only-binary=:all:",
        ],
        env=env,
        check=True,
        capture_output=True,
        timeout=30,
    )
    result = subprocess.check_output(
        [str(python), "-I", "-c", "import clio_offline_fixture; print(clio_offline_fixture.VALUE)"],
        env=env,
        text=True,
        timeout=10,
    )
    assert result.strip() == "installed offline"
