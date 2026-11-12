"""install/interleave_runtime.py deals the runtime's files through the archive."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _module():
    spec = importlib.util.spec_from_file_location(
        "interleave_runtime", ROOT / "install" / "interleave_runtime.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree(root: Path) -> Path:
    runtime = root / "gact-runtime"
    for folder, count in (("python/Lib", 70), ("documents/office/program", 70), ("app", 3)):
        for index in range(count):
            path = runtime / folder / f"f{index:03}.bin"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * index)
    (runtime / "runtime.json").write_text("{}")
    return runtime


def test_every_file_once_after_its_directories(tmp_path: Path) -> None:
    runtime = _tree(tmp_path)
    names = _module().members(runtime)
    files = sorted(
        "gact-runtime/" + path.relative_to(runtime).as_posix()
        for path in runtime.rglob("*")
        if path.is_file()
    )
    directories = {
        "gact-runtime/" + path.relative_to(runtime).as_posix()
        for path in runtime.rglob("*")
        if path.is_dir()
    } | {"gact-runtime"}
    assert sorted(name for name in names if name not in directories) == files
    assert set(names[: len(directories)]) == directories
    assert len(names) == len(set(names))


def test_documents_are_spread_through_the_archive_not_left_to_the_end(tmp_path: Path) -> None:
    runtime = _tree(tmp_path)
    names = _module().members(runtime)
    files = [name for name in names if name.endswith(".bin")]
    first_half = files[: len(files) // 2]
    assert any("/documents/" in name for name in first_half)
    assert any("/python/" in name for name in files[len(files) // 2 :])
    assert names == _module().members(runtime)


def test_a_non_ascii_path_is_refused(tmp_path: Path) -> None:
    runtime = _tree(tmp_path)
    (runtime / "app" / "caf\u00e9.txt").write_text("x")
    with pytest.raises(ValueError, match="non-ASCII"):
        _module().members(runtime)
