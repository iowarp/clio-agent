"""The macOS bundle smoke finds every signable binary and proves the seal stays intact."""

from __future__ import annotations

import importlib.util
import os
import struct
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_smoke_macos_bundle", ROOT / "scripts" / "smoke_macos_bundle.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _load()
THIN = b"\xcf\xfa\xed\xfe" + b"\0" * 12
FAT = b"\xca\xfe\xba\xbe" + struct.pack(">I", 2) + b"\0" * 8
JAVA_CLASS = b"\xca\xfe\xba\xbe" + struct.pack(">HH", 0, 65) + b"\0" * 8


def test_macho_detection_uses_magic_not_names() -> None:
    assert smoke.is_macho(THIN)
    assert smoke.is_macho(FAT)
    assert not smoke.is_macho(JAVA_CLASS)
    assert not smoke.is_macho(b"#!/bin/sh\n")


def test_inventory_lists_runtime_binaries_and_archive_members(tmp_path: Path) -> None:
    runtime = tmp_path / "gact-runtime"
    (runtime / "python/bin").mkdir(parents=True)
    (runtime / "python/bin/python3.14").write_bytes(THIN)
    (runtime / "python/bin/python3").symlink_to("python3.14")
    (runtime / "python/lib").mkdir()
    (runtime / "python/lib/_ssl.cpython-314-darwin.so").write_bytes(FAT)
    (runtime / "python/lib/module.py").write_text("x = 1\n", encoding="utf-8")
    (runtime / "codex").write_bytes(THIN)
    wheels = runtime / "document-wheels"
    wheels.mkdir()
    with zipfile.ZipFile(wheels / "pkg-1-cp314-macosx_11_0_arm64.whl", "w") as archive:
        archive.writestr("pkg/_native.so", THIN)
        archive.writestr("pkg/__init__.py", "")

    inventory = smoke.macho_inventory(runtime)

    assert inventory == {
        "files": ["codex", "python/bin/python3.14", "python/lib/_ssl.cpython-314-darwin.so"],
        "archived": ["document-wheels/pkg-1-cp314-macosx_11_0_arm64.whl!pkg/_native.so"],
    }


def test_any_write_into_the_bundle_fails_the_smoke(tmp_path: Path) -> None:
    app = tmp_path / "CLIO Desktop.app"
    module = app / "Contents/Resources/gact-runtime/python/lib/mod.py"
    module.parent.mkdir(parents=True)
    module.write_text("x = 1\n", encoding="utf-8")
    before = smoke.snapshot(app)
    smoke.require_unchanged(app, before)

    cache = module.parent / "__pycache__" / "mod.cpython-314.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"\0" * 16)

    with pytest.raises(RuntimeError, match="wrote into its bundle.*mod.cpython-314.pyc"):
        smoke.require_unchanged(app, before)


@pytest.mark.skipif(sys.platform != "darwin", reason="quarantine is a macOS xattr")
def test_quarantine_marks_files_and_links_without_following_them(tmp_path: Path) -> None:
    app = tmp_path / "CLIO Desktop.app"
    binary = app / "Contents/MacOS/clio-desktop"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(THIN)
    outside = tmp_path / "outside"
    outside.write_text("", encoding="utf-8")
    (app / "Contents/link").symlink_to(outside)

    smoke.quarantine(app)

    import ctypes

    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    buffer = ctypes.create_string_buffer(128)

    def marked(path: Path, flags: int = 0) -> bool:
        size = libc.getxattr(
            os.fsencode(path), b"com.apple.quarantine", buffer, len(buffer), 0, flags
        )
        return size > 0 and buffer.raw[:size].endswith(b";Safari;")

    assert marked(binary) and marked(app)
    assert marked(app / "Contents/link", 0x0001)
    assert not marked(outside)
