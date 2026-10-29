"""Managed-tool bootstrap can locate CLIO's uv without agent/service dependencies."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("name", "module"),
    [
        ("InfrastructureRuntime", "runtime"),
        ("InfrastructureStore", "store"),
        ("InfrastructureTransportRegistry", "transport"),
    ],
)
def test_infrastructure_public_imports_keep_their_identity(name: str, module: str) -> None:
    import importlib

    package = importlib.import_module("clio_agent.gact.infrastructure")
    implementation = importlib.import_module(f"{package.__name__}.{module}")
    assert getattr(package, name) is getattr(implementation, name)
    with pytest.raises(AttributeError):
        _ = package.unknown_infrastructure_type


def test_uv_lookup_does_not_import_service_stack(tmp_path: Path) -> None:
    binary = tmp_path / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    binary.parent.mkdir()
    binary.touch()
    source = Path(__file__).resolve().parents[2] / "src"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import sys, importlib.abc; "
            "sys.path.insert(0, sys.argv[1]); "
            "exec('class Reject(importlib.abc.MetaPathFinder):\\n'"
            "' def find_spec(self, fullname, path, target=None):\\n'"
            '\'  if fullname.startswith(("clio_schemas", "fastapi", "clio_agent.gact.infrastructure.runtime")): raise AssertionError(fullname)\'); '
            "sys.meta_path.insert(0, Reject()); "
            "from clio_agent.providers.dependencies import _uv_executable; "
            "assert _uv_executable(sys.argv[2]) == sys.argv[3]",
            str(source),
            str(tmp_path / "runtime/python"),
            str(binary),
        ],
        env={**os.environ, "CLIO_BIN_DIR": str(binary.parent), "GACT_BUNDLED_RUNTIME_DIR": ""},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
