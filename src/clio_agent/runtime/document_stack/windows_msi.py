"""Extract a checksum-verified Office application's embedded CAB without MSI actions.

The MSI is a read-only database. Application files and bundled fonts are published;
Windows system-integration components are not part of the private application.
Windows' CAB API decompresses members after their names and sizes match the MSI
File table, with expand.exe for longer paths. No installer service or registration runs.
"""

from __future__ import annotations

import ctypes
import os
import stat
import struct
import tempfile
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING or __package__:
    from .process import DocumentError, inherit_parent_access, run
else:
    from process import DocumentError, inherit_parent_access, run


def _check(code: int) -> None:
    if code:
        raise DocumentError(f"Could not read the verified Office MSI (Windows error {code})")


class _Database:
    """Own all native handles opened against a read-only MSI database."""

    def __init__(self, archive: Path) -> None:
        self.api: Any = cast(Any, ctypes).WinDLL("msi")
        handle = ctypes.c_uint
        self.api.MsiOpenDatabaseW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.POINTER(handle),
        ]
        self.api.MsiDatabaseOpenViewW.argtypes = [handle, wintypes.LPCWSTR, ctypes.POINTER(handle)]
        self.api.MsiViewExecute.argtypes = [handle, handle]
        self.api.MsiViewFetch.argtypes = [handle, ctypes.POINTER(handle)]
        self.api.MsiRecordGetStringW.argtypes = [
            handle,
            ctypes.c_uint,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.api.MsiRecordReadStream.argtypes = [
            handle,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.api.MsiCloseHandle.argtypes = [handle]
        self.handle = handle()
        _check(self.api.MsiOpenDatabaseW(str(archive), None, ctypes.byref(self.handle)))

    def close(self) -> None:
        """Release the database after all views have settled."""
        _check(self.api.MsiCloseHandle(self.handle))

    @contextmanager
    def records(self, sql: str) -> Iterator[Iterator[ctypes.c_uint]]:
        """Yield fetched records while bounding both view and record lifetimes."""
        view = ctypes.c_uint()
        _check(self.api.MsiDatabaseOpenViewW(self.handle, sql, ctypes.byref(view)))

        def iterate() -> Generator[ctypes.c_uint, None, None]:
            while True:
                record = ctypes.c_uint()
                code = self.api.MsiViewFetch(view, ctypes.byref(record))
                if code == 259:  # ERROR_NO_MORE_ITEMS
                    return
                _check(code)
                try:
                    yield record
                finally:
                    _check(self.api.MsiCloseHandle(record))

        try:
            _check(self.api.MsiViewExecute(view, 0))
            iterator = iterate()
            try:
                yield iterator
            finally:
                iterator.close()
        finally:
            _check(self.api.MsiCloseHandle(view))

    def rows(self, sql: str, columns: int) -> list[tuple[str, ...]]:
        """Read text/integer columns without executing installation sequences."""
        rows = []
        with self.records(sql) as records:
            for record in records:
                values = []
                for field in range(1, columns + 1):
                    size = wintypes.DWORD()
                    code = self.api.MsiRecordGetStringW(record, field, None, ctypes.byref(size))
                    if code not in (0, 234):  # ERROR_MORE_DATA reports the required size.
                        _check(code)
                    buffer = ctypes.create_unicode_buffer(size.value + 1)
                    size.value += 1
                    _check(self.api.MsiRecordGetStringW(record, field, buffer, ctypes.byref(size)))
                    values.append(buffer.value)
                rows.append(tuple(values))
        return rows

    def stream(self, name: str, output: Path) -> None:
        """Copy one embedded CAB stream in bounded chunks."""
        sql = "SELECT `Data` FROM `_Streams` WHERE `Name`='" + name.replace("'", "''") + "'"
        with self.records(sql) as records:
            record = next(records, None)
            if record is None:
                raise DocumentError(f"Office MSI is missing embedded cabinet {name}")
            buffer = ctypes.create_string_buffer(1024 * 1024)
            with output.open("xb") as target:
                while True:
                    size = wintypes.DWORD(len(buffer))
                    _check(self.api.MsiRecordReadStream(record, 1, buffer, ctypes.byref(size)))
                    if not size.value:
                        break
                    target.write(buffer.raw[: size.value])


def _name(value: str) -> str:
    if any(char in value for char in "/\\\0") or PureWindowsPath(value).drive:
        raise DocumentError(f"Unsafe path in Office MSI: {value!r}")
    name = value.split(":", 1)[0].split("|")[-1]
    if name == ".":
        return name
    if not name or name == ".." or any(char in name for char in "/\\:"):
        raise DocumentError(f"Unsafe path in Office MSI: {value!r}")
    if PureWindowsPath(name).is_absolute() or PureWindowsPath(name).drive:
        raise DocumentError(f"Absolute path in Office MSI: {value!r}")
    return name


def _application_files(
    directories: list[tuple[str, ...]],
    components: list[tuple[str, ...]],
    files: list[tuple[str, ...]],
    root: Path,
) -> dict[str, tuple[Path, int]]:
    root = root.resolve()
    parents = {key: (parent, name) for key, parent, name in directories}
    component_dirs: dict[str, str] = {}
    for key, directory_key in components:
        component_dirs[key] = directory_key
    resolved: dict[str, Path | None] = {"INSTALLLOCATION": root}
    # Administrative images also contain the renderer's font payload at the
    # root. Retain it, including every language's fonts, without installing it
    # into the operating system's Fonts directory.
    for key, parent, name in directories:
        if parent == "TARGETDIR" and _name(name).casefold() == "fonts":
            resolved[key] = root / "Fonts"

    def directory(key: str, seen: frozenset[str] = frozenset()) -> Path | None:
        if key in resolved:
            return resolved[key]
        if key in seen or key not in parents:
            raise DocumentError("Invalid directory ancestry in Office MSI")
        parent, name = parents[key]
        base = directory(parent, seen | {key}) if parent else None
        result = base / _name(name) if base is not None else None
        resolved[key] = result
        return result

    targets: dict[str, tuple[Path, int]] = {}
    occupied: set[str] = set()
    for identifier, component, filename, size in files:
        if _name(identifier) != identifier or identifier == ".":
            raise DocumentError("Invalid file identifier in Office MSI")
        if component not in component_dirs:
            raise DocumentError("Missing component directory in Office MSI")
        base = directory(component_dirs[component])
        if base is None:
            continue
        leaf = _name(filename)
        if leaf == ".":
            raise DocumentError("Invalid application filename in Office MSI")
        target = base / leaf
        key = str(target).casefold()
        if identifier in targets or key in occupied or not target.is_relative_to(root):
            raise DocumentError("Overlapping or escaping application files in Office MSI")
        occupied.add(key)
        if int(size) < 0:
            raise DocumentError("Invalid application file size in Office MSI")
        targets[identifier] = (target, int(size))
    if not targets:
        raise DocumentError("Office MSI has no private application payload")
    return targets


def _validate_cabinet(cabinet: Path, sizes: Mapping[str, int]) -> None:
    """Reject unexpected names, paths and sizes before Windows expands any member."""
    with cabinet.open("rb") as source:
        header = source.read(36)
        if len(header) != 36 or header[:4] != b"MSCF":
            raise DocumentError("Invalid Office cabinet header")
        source.seek(struct.unpack_from("<I", header, 16)[0])
        seen = set()
        for _ in range(struct.unpack_from("<H", header, 28)[0]):
            entry = source.read(16)
            if len(entry) != 16:
                raise DocumentError("Truncated Office cabinet entry")
            name_bytes = bytearray()
            while (char := source.read(1)) != b"\0":
                if not char or len(name_bytes) > 255:
                    raise DocumentError("Invalid Office cabinet member name")
                name_bytes.extend(char)
            try:
                name = name_bytes.decode(
                    "utf-8" if struct.unpack_from("<H", entry, 14)[0] & 128 else "cp1252"
                )
            except UnicodeDecodeError as exc:
                raise DocumentError("Invalid Office cabinet member encoding") from exc
            if _name(name) != name or name == ".":
                raise DocumentError("Invalid Office cabinet member identifier")
            if (
                name in seen
                or name not in sizes
                or sizes[name] != struct.unpack_from("<I", entry)[0]
            ):
                raise DocumentError("Office cabinet differs from its MSI file table")
            seen.add(name)


class _CabinetFile(ctypes.Structure):
    _fields_ = [
        ("NameInCabinet", wintypes.LPCWSTR),
        ("FileSize", wintypes.DWORD),
        ("Win32Error", wintypes.DWORD),
        ("DosDate", wintypes.WORD),
        ("DosTime", wintypes.WORD),
        ("DosAttribs", wintypes.WORD),
        ("FullTargetName", wintypes.WCHAR * 260),
    ]


class _ExtractedFile(ctypes.Structure):
    _fields_ = [
        ("Target", wintypes.LPCWSTR),
        ("Source", wintypes.LPCWSTR),
        ("Win32Error", wintypes.UINT),
        ("Flags", wintypes.DWORD),
    ]


def _iterate_cabinet(cabinet: Path, notify: Callable[[int, int], int]) -> None:
    """Use Windows' CAB decompressor without any device/application install actions."""
    callback_type = cast(Any, ctypes).WINFUNCTYPE(
        wintypes.UINT, ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, ctypes.c_size_t
    )
    callback = callback_type(lambda context, message, first, second: notify(message, first))
    api: Any = cast(Any, ctypes).WinDLL("setupapi", use_last_error=True)
    api.SetupIterateCabinetW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        callback_type,
        ctypes.c_void_p,
    ]
    api.SetupIterateCabinetW.restype = wintypes.BOOL
    if not api.SetupIterateCabinetW(str(cabinet), 0, callback, None):
        error = cast(Any, ctypes).WinError()
        raise DocumentError(f"Could not expand verified Office cabinet: {error}")


def _direct_paths_supported(cabinet: Path, targets: Mapping[str, tuple[Path, int]]) -> bool:
    # SetupAPI's FILE_IN_CABINET_INFO_W has a fixed MAX_PATH target buffer.
    # Keep the expand.exe path for longer profiles; never truncate a destination.
    return all(
        len(str(path).encode("utf-16-le")) // 2 < 260
        for path in (cabinet, *(target for target, _ in targets.values()))
    )


def _expand_direct(cabinet: Path, targets: Mapping[str, tuple[Path, int]]) -> set[str]:
    """Decompress straight into validated application paths, avoiding a second file move."""
    requested: dict[str, str] = {}
    published: set[str] = set()
    created: set[Path] = set()
    failures: list[Exception] = []

    def notify(message: int, parameter: int) -> int:
        try:
            if message == 0x11:  # SPFILENOTIFY_FILEINCABINET
                info = ctypes.cast(parameter, ctypes.POINTER(_CabinetFile)).contents
                if info.NameInCabinet not in targets:
                    return 2  # FILEOP_SKIP: system integration is outside the private app.
                target, size = targets[info.NameInCabinet]
                name = str(target)
                if info.FileSize != size or name in requested:
                    raise DocumentError("Duplicate or damaged Office cabinet application file")
                if target.parent not in created:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    created.add(target.parent)
                requested[name] = info.NameInCabinet
                info.FullTargetName = name
                return 1  # FILEOP_DOIT
            if message == 0x13:  # SPFILENOTIFY_FILEEXTRACTED
                extracted = ctypes.cast(parameter, ctypes.POINTER(_ExtractedFile)).contents
                if extracted.Win32Error:
                    raise DocumentError(f"Office cabinet write failed: {extracted.Win32Error}")
                identifier = requested.get(extracted.Target)
                if identifier is None or identifier in published:
                    raise DocumentError("Unexpected Office cabinet extraction result")
                published.add(identifier)
            if message == 0x12:  # SPFILENOTIFY_NEEDNEWCABINET
                raise DocumentError("Spanned Office cabinets are unsupported")
            return 0
        except (DocumentError, OSError, ValueError) as exc:
            # Never let Python exceptions cross the native callback boundary.
            failures.append(exc)
            return 0 if message == 0x11 else 13  # FILEOP_ABORT / ERROR_INVALID_DATA

    try:
        _iterate_cabinet(cabinet, notify)
    except DocumentError:
        if not failures:
            raise
    if failures:
        raise DocumentError(f"Office cabinet extraction failed: {failures[0]}") from failures[0]
    return published


def extract_application(archive: Path, root: Path) -> None:
    """Expand the pinned MSI's complete application tree into a fresh private root."""
    if any(root.iterdir()):
        raise DocumentError("Office extraction requires an empty private directory")
    database = _Database(archive)
    try:
        files = database.rows("SELECT `File`, `Component_`, `FileName`, `FileSize` FROM `File`", 4)
        targets = _application_files(
            database.rows(
                "SELECT `Directory`, `Directory_Parent`, `DefaultDir` FROM `Directory`", 3
            ),
            database.rows("SELECT `Component`, `Directory_` FROM `Component`", 2),
            files,
            root,
        )
        sizes = {row[0]: int(row[3]) for row in files}
        published: set[str] = set()
        created: set[Path] = set()
        with tempfile.TemporaryDirectory(prefix="msi-", dir=root.parent) as temporary:
            staging = Path(temporary)
            inherit_parent_access(staging)
            for (cabinet,) in database.rows("SELECT `Cabinet` FROM `Media`", 1):
                if not cabinet.startswith("#"):
                    raise DocumentError("Office MSI requires an unsupported external cabinet")
                name = _name(cabinet[1:])
                if name != cabinet[1:] or name == ".":
                    raise DocumentError("Invalid embedded Office cabinet name")
                cab_path = staging / name
                database.stream(cabinet[1:], cab_path)
                _validate_cabinet(cab_path, sizes)
                if _direct_paths_supported(cab_path, targets):
                    extracted = _expand_direct(cab_path, targets)
                    if published.intersection(extracted):
                        raise DocumentError("Duplicate extracted Office application file")
                    published.update(extracted)
                    continue
                loose = staging / (cab_path.stem + "-files")
                loose.mkdir()
                expand = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/expand.exe"
                run([str(expand), str(cab_path), "-F:*", str(loose)], cwd=staging, timeout=180)
                with os.scandir(loose) as entries:
                    for source in entries:
                        if source.name not in targets:
                            continue
                        target, size = targets[source.name]
                        metadata = source.stat(follow_symlinks=False)
                        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != size:
                            raise DocumentError(
                                "Office cabinet extraction returned damaged content"
                            )
                        if source.name in published:
                            raise DocumentError("Duplicate extracted Office application file")
                        if target.parent not in created:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            created.add(target.parent)
                        Path(source.path).rename(target)
                        published.add(source.name)
        if published != targets.keys():
            raise DocumentError("Office cabinet extraction omitted application files")
    finally:
        database.close()
