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
    smoke.require_unchanged(app, before, "the launch")

    cache = module.parent / "__pycache__" / "mod.cpython-314.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"\0" * 16)

    with pytest.raises(RuntimeError, match="the launch wrote into the bundle.*mod.cpython-314.pyc"):
        smoke.require_unchanged(app, before, "the launch")


def test_bytecode_newer_than_the_build_fails_before_any_other_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = tmp_path / "CLIO Desktop.app"
    plist = app / "Contents/Info.plist"
    shipped = app / "Contents/Resources/gact-runtime/python/lib/__pycache__/os.cpython-314.pyc"
    shipped.parent.mkdir(parents=True)
    shipped.write_bytes(b"\0" * 16)
    plist.write_bytes(b"")
    os.utime(shipped, ns=(1_000, 1_000))
    os.utime(plist, ns=(2_000, 2_000))
    verified: list[Path] = []
    monkeypatch.setattr(smoke, "verify", verified.append)
    before = smoke.snapshot(app)

    assert smoke.written_bytecode(app) == []
    smoke.require_sealed(app, before, "--prepare-runtime")
    assert verified == [app]

    compiled = shipped.parent / "plistlib.cpython-314.pyc"
    compiled.write_bytes(b"\0" * 16)
    os.utime(compiled, ns=(3_000, 3_000))

    assert smoke.written_bytecode(app) == [
        "Contents/Resources/gact-runtime/python/lib/__pycache__/plistlib.cpython-314.pyc"
    ]
    with pytest.raises(RuntimeError, match="--prepare-runtime compiled bytecode.*plistlib"):
        smoke.require_sealed(app, before, "--prepare-runtime")
    assert verified == [app]


def test_web_search_check_waits_through_the_starting_503(monkeypatch: pytest.MonkeyPatch) -> None:
    answers: list[object] = [
        smoke.HTTPError("providers", 503, "starting", None, None),  # type: ignore[arg-type]
        {"web": [{"id": "keenable"}, {"id": "exa"}], "literature": []},
    ]
    urls: list[str] = []

    def get_json(url: str, token: str | None = None) -> object:
        urls.append(url)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(smoke, "get_json", get_json)
    monkeypatch.setattr(smoke.time, "sleep", lambda _seconds: None)
    smoke.use_web_search(2, "token")

    assert urls[-1] == "http://127.0.0.1:2/v1/search/providers"
    assert not answers


def test_web_search_check_fails_without_the_built_in_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smoke, "get_json", lambda url, token=None: {"web": []})
    with pytest.raises(RuntimeError, match="sources missing"):
        smoke.use_web_search(2, "token")


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
