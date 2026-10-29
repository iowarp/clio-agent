"""Private Office extraction validates MSI layout and CAB contents before publication."""

from __future__ import annotations

import ctypes
import struct
from pathlib import Path
from typing import Any

import pytest

from clio_agent.runtime.document_stack import windows_msi as msi


def _layout(root: Path, filename: str = "SOFFIC~1.EXE|soffice.exe") -> dict[str, tuple[Path, int]]:
    return msi._application_files(
        [
            ("TARGETDIR", "", "SourceDir"),
            ("INSTALLLOCATION", "TARGETDIR", "OFFICE~1|Office:."),
            ("program", "INSTALLLOCATION", "program"),
            ("font", "TARGETDIR", "Fonts"),
            ("system", "TARGETDIR", "System64"),
        ],
        [("app", "program"), ("fonts", "font"), ("system_dll", "system")],
        [
            ("soffice", "app", filename, "3"),
            ("font_file", "fonts", "font.ttf", "2"),
            ("dll", "system_dll", "system.dll", "1"),
        ],
        root,
    )


def _cabinet(path: Path, entries: list[tuple[bytes, int, int]]) -> None:
    header = bytearray(36)
    header[:4] = b"MSCF"
    struct.pack_into("<I", header, 16, 36)
    struct.pack_into("<H", header, 28, len(entries))
    data = bytearray(header)
    for name, size, attributes in entries:
        data.extend(struct.pack("<IIHHHH", size, 0, 0, 0, 0, attributes))
        data.extend(name + b"\0")
    path.write_bytes(data)


def test_application_tree_retains_fonts_without_system_integration(tmp_path: Path) -> None:
    root = tmp_path / "Office"
    assert _layout(root) == {
        "soffice": (root / "program/soffice.exe", 3),
        "font_file": (root / "Fonts/font.ttf", 2),
    }


@pytest.mark.parametrize(
    "filename", ["..", ".", "../escape", "..\\escape", "C:\\escape", "C:escape", "bad\0name"]
)
def test_rejects_escaping_application_names(tmp_path: Path, filename: str) -> None:
    with pytest.raises(msi.DocumentError):
        _layout(tmp_path, filename)


@pytest.mark.parametrize(
    ("directories", "components", "files"),
    [
        ([("loop", "loop", "loop")], [("c", "loop")], [("f", "c", "f", "1")]),
        ([], [], [("f", "missing", "f", "1")]),
        ([], [("c", "INSTALLLOCATION")], [("f", "c", "f", "-1")]),
        ([], [("c", "INSTALLLOCATION")], [("a", "c", "F", "1"), ("b", "c", "f", "1")]),
        ([], [("c", "INSTALLLOCATION")], [("a", "c", "f", "1"), ("a", "c", "g", "1")]),
    ],
)
def test_rejects_inconsistent_application_inventory(
    tmp_path: Path,
    directories: list[tuple[str, ...]],
    components: list[tuple[str, ...]],
    files: list[tuple[str, ...]],
) -> None:
    with pytest.raises(msi.DocumentError):
        msi._application_files(directories, components, files, tmp_path)


def test_cabinet_inventory_accepts_exact_literal_names_and_sizes(tmp_path: Path) -> None:
    cabinet = tmp_path / "office.cab"
    _cabinet(cabinet, [(b"soffice", 3, 0), (b"font_file", 2, 0)])
    msi._validate_cabinet(cabinet, {"soffice": 3, "font_file": 2})


@pytest.mark.parametrize(
    "entries",
    [
        [(b"../escape", 3, 0)],
        [(b"C:escape", 3, 0)],
        [(b".", 3, 0)],
        [(b"short|soffice", 3, 0)],
        [(b"soffice", 4, 0)],
        [(b"unknown", 3, 0)],
        [(b"soffice", 3, 0), (b"soffice", 3, 0)],
        [(b"\xff", 3, 128)],
    ],
)
def test_cabinet_rejects_unsafe_or_mismatched_entries(
    tmp_path: Path, entries: list[tuple[bytes, int, int]]
) -> None:
    cabinet = tmp_path / "office.cab"
    _cabinet(cabinet, entries)
    with pytest.raises(msi.DocumentError):
        msi._validate_cabinet(cabinet, {"soffice": 3})


@pytest.mark.parametrize("payload", [b"", b"MSCF", b"MSCF" + b"\0" * 24 + b"\1" + b"\0" * 7])
def test_rejects_truncated_cabinet(tmp_path: Path, payload: bytes) -> None:
    cabinet = tmp_path / "office.cab"
    cabinet.write_bytes(payload)
    with pytest.raises(msi.DocumentError):
        msi._validate_cabinet(cabinet, {"soffice": 3})


@pytest.mark.parametrize("damage", ["none", "missing", "wrong_size"])
def test_extraction_checks_published_payload_and_closes_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    closed: list[bool] = []

    class Database:
        def __init__(self, archive: Path) -> None:
            assert archive == tmp_path / "office.msi"

        def rows(self, sql: str, columns: int) -> list[tuple[str, ...]]:
            if "FROM `File`" in sql:
                return [("soffice", "app", "soffice.exe", "3")]
            if "FROM `Directory`" in sql:
                return [("INSTALLLOCATION", "", ".")]
            if "FROM `Component`" in sql:
                return [("app", "INSTALLLOCATION")]
            assert "FROM `Media`" in sql
            return [("#office.cab",)]

        def stream(self, name: str, output: Path) -> None:
            _cabinet(output, [(b"soffice", 3, 0)])

        def close(self) -> None:
            closed.append(True)

    def expand(command: list[str], **kwargs: Any) -> str:
        assert command[2] == "-F:*"
        if damage != "missing":
            (Path(command[3]) / "soffice").write_bytes(
                b"bad_size" if damage == "wrong_size" else b"exe"
            )
        return ""

    monkeypatch.setattr(msi, "_Database", Database)
    monkeypatch.setattr(msi, "_direct_paths_supported", lambda *args: False)
    monkeypatch.setattr(msi, "run", expand)
    monkeypatch.setattr(msi, "inherit_parent_access", lambda path: None)
    root = tmp_path / "Office"
    root.mkdir()
    if damage == "none":
        msi.extract_application(tmp_path / "office.msi", root)
        assert (root / "soffice.exe").read_bytes() == b"exe"
    else:
        with pytest.raises(msi.DocumentError, match="omitted|damaged"):
            msi.extract_application(tmp_path / "office.msi", root)
    assert closed == [True]
    assert not list(tmp_path.glob("msi-*"))


@pytest.mark.parametrize(
    "damage", ["none", "size", "duplicate", "write", "unknown_result", "spanned"]
)
def test_direct_cabinet_notifications_preserve_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    target = tmp_path / "program/soffice.com"

    def iterate(cabinet: Path, notify: Any) -> None:
        skipped = msi._CabinetFile(NameInCabinet="system_dll", FileSize=9)
        assert notify(0x11, ctypes.addressof(skipped)) == 2
        if damage == "spanned":
            assert notify(0x12, 0) != 0
            return
        info = msi._CabinetFile(NameInCabinet="soffice", FileSize=4 if damage == "size" else 3)
        decision = notify(0x11, ctypes.addressof(info))
        if damage == "size":
            assert decision == 0
            return
        assert decision == 1 and info.FullTargetName == str(target)
        if damage == "duplicate":
            assert notify(0x11, ctypes.addressof(info)) == 0
            return
        extracted = msi._ExtractedFile(
            Target="unexpected" if damage == "unknown_result" else str(target),
            Win32Error=5 if damage == "write" else 0,
        )
        assert notify(0x13, ctypes.addressof(extracted)) == (0 if damage == "none" else 13)

    monkeypatch.setattr(msi, "_iterate_cabinet", iterate)
    if damage == "none":
        assert msi._expand_direct(tmp_path / "office.cab", {"soffice": (target, 3)}) == {"soffice"}
    else:
        with pytest.raises(msi.DocumentError):
            msi._expand_direct(tmp_path / "office.cab", {"soffice": (target, 3)})


def test_direct_cabinet_declines_long_paths_without_truncating(tmp_path: Path) -> None:
    assert msi._direct_paths_supported(tmp_path / "office.cab", {"a": (tmp_path / "a", 1)})
    assert not msi._direct_paths_supported(tmp_path / "office.cab", {"a": (Path("a" * 260), 1)})
    assert not msi._direct_paths_supported(tmp_path / "office.cab", {"a": (Path("😀" * 130), 1)})
