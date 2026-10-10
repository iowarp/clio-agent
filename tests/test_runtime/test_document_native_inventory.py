"""Independent native probes overlap without unbounded process fan-out."""

from __future__ import annotations

import importlib
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from clio_agent.runtime.document_runtime import STACK_ROOT


@pytest.fixture
def inventory_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the inventory exactly as its standalone interpreter does."""
    monkeypatch.syspath_prepend(str(STACK_ROOT))
    monkeypatch.delitem(sys.modules, "inventory", raising=False)
    module = importlib.import_module("inventory")
    monkeypatch.delitem(sys.modules, "inventory")
    return module


def test_converter_probes_overlap_with_two_workers(
    inventory_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reusable barrier detects accidental serial execution without speed assertions."""
    barrier = threading.Barrier(2, timeout=5)
    lock = threading.Lock()
    active = 0
    peak = 0
    flags: dict[str, str] = {}

    def run(command: list[str], *, cwd: Path, timeout: float) -> str:
        nonlocal active, peak
        assert cwd == tmp_path
        assert timeout == 10
        with lock:
            active += 1
            peak = max(peak, active)
            flags[command[0]] = command[1]
        barrier.wait()
        barrier.wait()
        with lock:
            active -= 1
        return f"{command[0]} version\n"

    monkeypatch.setattr(inventory_module, "find_native", lambda name: name)
    monkeypatch.setattr(inventory_module, "run", run)
    result = inventory_module.native_inventory(tmp_path)
    assert peak == 2
    assert list(result) == list(inventory_module.NATIVE_TOOLS)
    assert flags["pdftoppm"] == "-v"
    assert all(flag == "--version" for name, flag in flags.items() if name != "pdftoppm")
    assert result["soffice"] == {
        "status": "available",
        "path": "soffice",
        "version": "soffice version",
    }


def test_probe_failure_and_missing_tool_do_not_discard_other_results(
    inventory_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Timeouts remain per converter; missing tools never launch a process."""

    def find(name: str) -> str | None:
        if name == "pandoc":
            return None
        if name == "tesseract":
            raise inventory_module.DocumentError("configured executable missing")
        return name

    def run(command: list[str], **kwargs: Any) -> str:
        if command[0] == "soffice":
            raise inventory_module.DocumentError("soffice exceeded 10 seconds")
        assert command[0] == "pdftoppm"
        return "poppler version"

    monkeypatch.setattr(inventory_module, "find_native", find)
    monkeypatch.setattr(inventory_module, "run", run)
    result = inventory_module.native_inventory(tmp_path)
    assert result["soffice"]["status"] == "failed"
    assert "10 seconds" in result["soffice"]["error"]
    assert result["pandoc"] == {"status": "missing", "path": None}
    assert result["tesseract"]["status"] == "failed"
    assert result["pdftoppm"]["version"] == "poppler version"


def test_inventory_launches_real_version_processes(
    inventory_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise portable process launch and output collection with the current Python."""
    monkeypatch.setattr(inventory_module, "NATIVE_TOOLS", ("first", "second"))
    monkeypatch.setattr(inventory_module, "find_native", lambda name: sys.executable)
    result = inventory_module.native_inventory(tmp_path)
    assert all(row["status"] == "available" for row in result.values())
    assert all(row["version"].startswith("Python ") for row in result.values())


def test_execution_inventory_reads_fresh_metadata_without_converter_or_font_work(
    inventory_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An updated distribution is visible on the next call, without importing it."""
    node_module = ModuleType("nodejs_wheel")
    node_module.__file__ = str(tmp_path / "__init__.py")
    monkeypatch.setitem(sys.modules, "nodejs_wheel", node_module)
    (tmp_path / "node.exe").touch()
    monkeypatch.setattr(inventory_module, "PACKAGES", {"example": "example"})
    monkeypatch.setattr(inventory_module.importlib.util, "find_spec", lambda name: object())
    revision = "1.0"
    monkeypatch.setattr(inventory_module.importlib.metadata, "version", lambda name: revision)
    monkeypatch.setattr(inventory_module, "run", lambda *args, **kwargs: "v26.0.0")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "Execution discovery must not import packages, scan fonts or probe converters"
        )

    monkeypatch.setattr(inventory_module.importlib, "import_module", forbidden)
    monkeypatch.setattr(inventory_module, "native_inventory", forbidden)
    monkeypatch.setattr(Path, "rglob", forbidden)
    first = inventory_module.inventory(execution_only=True)
    revision = "2.0"
    second = inventory_module.inventory(execution_only=True)
    assert first["packages"]["example"]["version"] == "1.0"
    assert second["packages"]["example"] == {
        "version": "2.0",
        "import": "example",
        "verification": "module_discovered",
    }
    assert second["inventory_scope"] == "execution"
    assert second["font_files"] == []
    assert all(tool["status"] == "not_checked" for tool in second["native_tools"].values())
