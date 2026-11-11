"""``clio update`` drops a release runtime's document wheels once they stop matching.

A plain install from a release's prebuilt runtime keeps ``runtime.json`` and the
release's ``document-wheels`` in the venv. ``clio update`` replaces clio-agent; when
the new one locks other document packages, preparing documents would reject those
wheels, so the launcher removes them and preparation follows the new lock instead.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "install" / "clio"
LOCK = ROOT / "src" / "clio_agent" / "runtime" / "document_stack" / "uv.lock"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _function_source() -> str:
    text = LAUNCHER.read_text(encoding="utf-8")
    match = re.search(
        r"^drop_stale_document_wheels\(\) \{\n.*?^\}\n", text, re.MULTILINE | re.DOTALL
    )
    assert match, "drop_stale_document_wheels() not found in install/clio"
    return match.group(0)


def _venv(tmp_path: Path, *, runtime: bool, lock_sha256: str) -> Path:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python"
    python.write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    if runtime:
        (venv / "runtime.json").write_text("{}")
    wheels = venv / "document-wheels"
    wheels.mkdir()
    (wheels / "manifest.json").write_text(json.dumps({"lock_sha256": lock_sha256}))
    return venv


def _run(venv: Path) -> subprocess.CompletedProcess[str]:
    script = (
        "set -euo pipefail\n"
        'say() { echo "SAY $*"; }\n'
        f'CLIO_VENV="{venv}"\n'
        f"{_function_source()}"
        "drop_stale_document_wheels\n"
        "echo DONE\n"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )


@pytest.mark.parametrize(
    ("runtime", "matching", "kept"),
    [
        pytest.param(True, True, True, id="matching-lock-kept"),
        pytest.param(True, False, False, id="changed-lock-dropped"),
        pytest.param(False, False, True, id="not-a-release-runtime"),
    ],
)
def test_update_keeps_only_document_wheels_built_for_the_installed_lock(
    tmp_path: Path, runtime: bool, matching: bool, kept: bool
) -> None:
    sha = hashlib.sha256(LOCK.read_bytes()).hexdigest() if matching else "0" * 64
    venv = _venv(tmp_path, runtime=runtime, lock_sha256=sha)

    result = _run(venv)

    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip().endswith("DONE")
    assert (venv / "document-wheels").is_dir() is kept
    assert ("SAY " in result.stdout) is not kept
