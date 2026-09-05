"""Installation and qualification contracts for the shared CLIO Kit runtime."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _qualification_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "qualify_shared_mcp_runtime", ROOT / "scripts/qualify_shared_mcp_runtime.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installers_select_one_persistent_science_union() -> None:
    """Both installers select science once and avoid per-server uvx commands."""
    for relative in ("install/install.sh", "install/install.ps1"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "CLIO_KIT_PACKAGE" in text
        assert "[science]" in text
        assert "uv tool install" in text
        assert "uvx clio-kit" not in text


def test_candidate_wheel_gets_science_extra() -> None:
    """A local candidate wheel uses the same science-union install boundary."""
    module = _qualification_module()
    assert module.science_package_spec("D:/build/clio_kit-3.0.0-py3-none-any.whl") == (
        "D:/build/clio_kit-3.0.0-py3-none-any.whl[science]"
    )
    assert module.science_package_spec("clio-kit==2.10.6") == "clio-kit[science]==2.10.6"


def test_package_spec_rejects_ambiguous_extras() -> None:
    """Callers cannot accidentally create a different dependency selection."""
    module = _qualification_module()
    with pytest.raises(ValueError, match="base package"):
        module.science_package_spec("clio-kit[pandas]")


def test_candidate_install_is_scoped_to_owned_root(tmp_path: Path, monkeypatch) -> None:
    """Candidate installation cannot replace an ambient uv tool runtime."""
    module = _qualification_module()
    calls = []
    monkeypatch.setattr(
        module.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    launcher, environment = module.install_candidate("candidate.whl", tmp_path / "runtime", uv="uv")
    assert Path(environment["UV_TOOL_DIR"]).is_relative_to(tmp_path)
    assert Path(environment["UV_TOOL_BIN_DIR"]).is_relative_to(tmp_path)
    assert launcher.is_relative_to(tmp_path)


def test_candidate_install_rejects_existing_root(tmp_path: Path) -> None:
    """A candidate cannot mutate a possibly live earlier runtime."""
    module = _qualification_module()
    root = tmp_path / "existing"
    root.mkdir()
    with pytest.raises(RuntimeError, match="live runtime"):
        module.install_candidate("candidate.whl", root, uv="uv")
