"""Bundled startup uses its own packages despite inherited Python import paths."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_bundle_helpers_use_the_resolved_checkout_in_ref_mode() -> None:
    """Cloned-ref builds retain their helper source until portability checks finish."""
    posix = (ROOT / "install/build-gact-runtime.sh").read_text(encoding="utf-8")
    windows = (ROOT / "install/build-gact-runtime.ps1").read_text(encoding="utf-8")
    assert '"$CHECKOUT/install/precompile_runtime.py"' in posix
    assert "Join-Path $checkout 'install/precompile_runtime.py'" in windows
    assert "Join-Path $checkout 'install/arc_smoke.py'" in windows
    assert windows.index("Remove-Item -LiteralPath $cleanupTarget") > windows.index(
        "relocated cold boot ready"
    )


@pytest.mark.parametrize("builder", ["build-gact-runtime.sh", "build-gact-runtime.ps1"])
def test_bundle_manifest_ignores_host_python_imports(tmp_path: Path, builder: str) -> None:
    """Run the actual manifest's Python flags against a conflicting host module."""
    script = (ROOT / "install" / builder).read_text(encoding="utf-8")
    if builder.endswith(".sh"):
        match = re.search(r'^\s*"exec": (\[[^\n]+\])', script, flags=re.MULTILINE)
        assert match is not None
        arguments = json.loads(match.group(1))[1:]
    else:
        match = re.search(r"exec = @\([^\n]+\)", script)
        assert match is not None
        arguments = re.findall(r"'([^']+)'", match.group(0))
    flags = arguments[: arguments.index("-m")]
    assert flags == ["-I", "-B"]
    (tmp_path / "json.py").write_text("raise RuntimeError('host module replaced bundle')\n")
    environment = {**os.environ, "PYTHONPATH": str(tmp_path)}
    command = [sys.executable, "-c", "import json; print(json.__name__)"]
    poisoned = subprocess.run(
        command, cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=15
    )
    assert poisoned.returncode != 0
    assert "host module replaced bundle" in poisoned.stderr
    isolated = subprocess.run(
        [command[0], *flags, *command[1:]],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert isolated.returncode == 0, isolated.stderr
    assert isolated.stdout.strip() == "json"


def test_bundle_uv_install_arguments_are_accepted_without_network(tmp_path: Path) -> None:
    """Execute bundle install commands in dry-run mode; Python flags cannot reach uv."""
    uv = shutil.which("uv")
    assert uv is not None
    requirement = f"packaging=={version('packaging')}"
    constraints = tmp_path / "constraints.txt"
    constraints.write_text(requirement + "\n")
    script = (ROOT / "install/build-gact-runtime.sh").read_text(encoding="utf-8")
    commands = [line for line in script.splitlines() if line.startswith("uv pip install ")]
    assert len(commands) == 2
    replacements = {
        "$OUT/$PYBIN_REL": sys.executable,
        "$CONSTRAINTS": str(constraints),
        "$BUNDLE_SPEC": requirement,
        "$WEB_MCP_PROJECT": requirement,
    }
    for command in commands:
        arguments = [replacements.get(argument, argument) for argument in shlex.split(command)[1:]]
        result = subprocess.run(
            [uv, *arguments, "--dry-run", "--no-index"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
