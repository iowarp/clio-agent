"""Startup precompilation confines writes and retains source-validating bytecode."""

from __future__ import annotations

import importlib.util
import marshal
import struct
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.mark.parametrize("editable", [False, True])
def test_startup_graph_compiles_only_selected_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, editable: bool
) -> None:
    script = Path(__file__).resolve().parents[2] / "install/precompile_runtime.py"
    spec = importlib.util.spec_from_file_location("_precompile_qualification", script)
    assert spec and spec.loader
    compiler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(compiler)
    python_root = tmp_path / "runtime"
    source_root = tmp_path / "source"
    files = [python_root / "dependency.py", source_root / "agent.py", tmp_path / "outside.py"]
    for index, file in enumerate(files):
        file.parent.mkdir(exist_ok=True)
        file.write_text("value = 42\n", encoding="utf-8")
        module = ModuleType(f"_precompile_input_{index}")
        module.__file__ = str(file)
        monkeypatch.setitem(sys.modules, module.__name__, module)
    built: list[bool] = []
    application = ModuleType("clio_agent.gact.app")
    monkeypatch.setattr(application, "build_app", lambda: built.append(True), raising=False)
    monkeypatch.setitem(sys.modules, application.__name__, application)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    assert compiler.precompile_startup_modules(python_root, source_root if editable else None) == (
        2 if editable else 1
    )
    assert built == [True]
    for file in files[: 2 if editable else 1]:
        bytecode = Path(importlib.util.cache_from_source(str(file))).read_bytes()
        assert struct.unpack_from("<I", bytecode, 4)[0] == 3  # CHECKED_HASH
        code = marshal.loads(bytecode[16:])
        assert code.co_filename == (
            file.as_posix() if editable else "gact-runtime/python/dependency.py"
        )
    for file in files[2 if editable else 1 :]:
        assert not Path(importlib.util.cache_from_source(str(file))).exists()
    files[0].write_text("value = 43\n", encoding="utf-8")
    changed_spec = importlib.util.spec_from_file_location("_updated_dependency", files[0])
    assert changed_spec and changed_spec.loader
    changed = importlib.util.module_from_spec(changed_spec)
    changed_spec.loader.exec_module(changed)
    assert changed.value == 43


def test_named_packages_are_compiled_whole_inside_the_runtime_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lazily imported first-party modules ship bytecode instead of compiling every launch."""
    script = Path(__file__).resolve().parents[2] / "install/precompile_runtime.py"
    spec = importlib.util.spec_from_file_location("_precompile_packages", script)
    assert spec and spec.loader
    compiler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(compiler)
    python_root = tmp_path / "runtime"
    package = python_root / "site-packages" / "_clio_lazy_pkg"
    lazy = package / "tools" / "lazy.py"
    lazy.parent.mkdir(parents=True)
    for file in (package / "__init__.py", package / "tools" / "__init__.py", lazy):
        file.write_text("value = 1\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere" / "_clio_outside_pkg"
    elsewhere.mkdir(parents=True)
    (elsewhere / "__init__.py").write_text("value = 2\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(python_root / "site-packages"))
    monkeypatch.syspath_prepend(str(elsewhere.parent))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)

    assert compiler.precompile_packages(python_root, ["_clio_lazy_pkg", "_clio_outside_pkg"]) == 3

    bytecode = Path(importlib.util.cache_from_source(str(lazy))).read_bytes()
    assert struct.unpack_from("<I", bytecode, 4)[0] == 3  # CHECKED_HASH
    assert marshal.loads(bytecode[16:]).co_filename == (
        "gact-runtime/python/site-packages/_clio_lazy_pkg/tools/lazy.py"
    )
    assert not Path(importlib.util.cache_from_source(str(elsewhere / "__init__.py"))).exists()
    with pytest.raises(RuntimeError, match="not an importable package"):
        compiler.precompile_packages(python_root, ["_clio_no_such_pkg"])
