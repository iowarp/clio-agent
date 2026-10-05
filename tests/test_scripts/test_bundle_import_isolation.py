"""Bundled startup uses its own packages despite inherited Python import paths."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


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
