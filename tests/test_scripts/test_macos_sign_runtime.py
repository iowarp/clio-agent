"""The macOS runtime signer finds every Mach-O and keeps signed wheels installable."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

import pytest

from scripts.macos_sign_runtime import (
    Signer,
    check_signature,
    entitlements_for,
    inventory,
    macho_kind,
    main,
    resign_archive,
    sign_runtime,
    update_wheel_manifest,
)

MH_EXECUTE = (0xFEEDFACF).to_bytes(4, "little") + bytes(8) + (2).to_bytes(4, "little") + bytes(16)
MH_DYLIB = (0xFEEDFACF).to_bytes(4, "little") + bytes(8) + (6).to_bytes(4, "little") + bytes(16)
MH_OBJECT = (0xFEEDFACF).to_bytes(4, "little") + bytes(8) + (1).to_bytes(4, "little") + bytes(16)


def _fat(*slices: bytes) -> bytes:
    offset = 4096
    header = b"\xca\xfe\xba\xbe" + len(slices).to_bytes(4, "big")
    body = b""
    for item in slices:
        header += bytes(8) + offset.to_bytes(4, "big") + len(item).to_bytes(4, "big") + bytes(4)
        body = body.ljust(offset - 4096, b"\0") + item
        offset += 4096
    return header.ljust(4096, b"\0") + body


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (MH_EXECUTE, "executable"),
        (MH_DYLIB, "library"),
        (MH_OBJECT, None),
        (_fat(MH_EXECUTE, MH_EXECUTE), "executable"),
        (_fat(MH_DYLIB), "library"),
        # Java class files share the fat magic; their "count" is a class version.
        (b"\xca\xfe\xba\xbe\x00\x00\x00\x41" + bytes(24), None),
        (b"#!/bin/sh\n" + bytes(30), None),
    ],
)
def test_classifies_signable_code_by_content(data: bytes, kind: str | None) -> None:
    assert macho_kind(io.BytesIO(data)) == kind


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("python/bin/python3.14", "gact-runtime.entitlements"),
        ("python/lib/python3.14/site-packages/iowarp_core/bin/clio_run", "clio-run.entitlements"),
        ("bin/uv", None),
        ("python/lib/python3.14/site-packages/pkg/bin/tool", None),
    ],
)
def test_entitlements_are_scoped_to_their_role(relative: str, expected: str | None) -> None:
    assert entitlements_for(PurePosixPath(relative)) == expected


def _wheel(path: Path, members: dict[str, bytes]) -> None:
    rows = []
    for name, data in members.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        rows.append(f"{name},sha256={digest},{len(data)}")
    rows.append("pkg-1.0.dist-info/RECORD,,")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        archive.writestr("pkg-1.0.dist-info/RECORD", "\n".join(rows) + "\n")


def _runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime"
    for relative, data in {
        "runtime.json": b"{}",
        "python/bin/python3.14": MH_EXECUTE,
        "python/lib/libpython3.14.dylib": MH_DYLIB,
        "python/lib/python3.14/site-packages/iowarp_core/bin/clio_run": MH_EXECUTE,
        "python/lib/python3.14/site-packages/pkg/module.o": MH_OBJECT,
        "Tk.framework/Versions/A/Tk": MH_DYLIB,
        "bin/uv": MH_EXECUTE,
    }.items():
        (runtime / relative).parent.mkdir(parents=True, exist_ok=True)
        (runtime / relative).write_bytes(data)
    (runtime / "python/bin/python3").symlink_to("python3.14")
    wheels = runtime / "document-wheels"
    wheels.mkdir()
    _wheel(wheels / "native-1.0.whl", {"pkg/_ext.so": MH_DYLIB, "pkg/__init__.py": b"x = 1\n"})
    _wheel(wheels / "pure-1.0.whl", {"pure/__init__.py": b"x = 1\n"})
    manifest = {
        "packages": [
            {"filename": name, "sha256": hashlib.sha256((wheels / name).read_bytes()).hexdigest()}
            for name in ("native-1.0.whl", "pure-1.0.whl")
        ]
    }
    (wheels / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return runtime


def test_inventory_finds_code_without_symlinks_objects_or_pure_wheels(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    found = inventory(runtime)
    relative = {target.path.relative_to(runtime).as_posix(): target for target in found.files}
    assert set(relative) == {
        "python/bin/python3.14",
        "python/lib/libpython3.14.dylib",
        "python/lib/python3.14/site-packages/iowarp_core/bin/clio_run",
        "Tk.framework/Versions/A/Tk",
        "bin/uv",
    }
    assert relative["python/bin/python3.14"].entitlements == "gact-runtime.entitlements"
    assert [path.name for path in found.archives] == ["native-1.0.whl"]
    assert [path.name for path in found.bundles] == ["Tk.framework"]


class FakeCodesign:
    """Records codesign calls and 'signs' by appending a marker to the file."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        target = Path(command[-1])
        if command[0] == "codesign" and target.is_file():
            target.write_bytes(target.read_bytes() + b"SIGNED")
        return subprocess.CompletedProcess(command, 0, "", "")


def test_signs_inside_out_with_runtime_timestamp_and_role_entitlements(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    fake = FakeCodesign()
    sign_runtime(runtime, Signer("Developer ID Application: Test (TEAM123456)", "kc", run=fake), 1)
    assert fake.calls[0] == ["xattr", "-cr", str(runtime)]
    signed = [Path(call[-1]) for call in fake.calls[1:]]
    order = {path: index for index, path in enumerate(signed)}
    library = runtime / "python/lib/libpython3.14.dylib"
    python = runtime / "python/bin/python3.14"
    framework = runtime / "Tk.framework"
    assert order[library] < order[python] < order[framework]
    for call in fake.calls[1:]:
        assert call[1:6] == [
            "--force",
            "--sign",
            "Developer ID Application: Test (TEAM123456)",
            "--options",
            "runtime",
        ]
        assert "--timestamp" in call and ["--keychain", "kc"] == call[7:9]
    python_call = next(call for call in fake.calls if Path(call[-1]) == python)
    assert python_call[-2].endswith("install/macos/gact-runtime.entitlements")
    assert not any(
        Path(call[-1]) == runtime / "python/lib/python3.14/site-packages/pkg/module.o"
        for call in fake.calls
    )


def test_resigned_wheel_keeps_order_attributes_and_a_valid_record(tmp_path: Path) -> None:
    wheel = tmp_path / "native-1.0.whl"
    _wheel(wheel, {"pkg/_ext.so": MH_DYLIB, "pkg/__init__.py": b"x = 1\n"})
    with zipfile.ZipFile(wheel) as archive:
        before = [(i.filename, i.compress_type, i.date_time) for i in archive.infolist()]
    resign_archive(wheel, Signer("-", run=FakeCodesign()))
    with zipfile.ZipFile(wheel) as archive:
        assert [(i.filename, i.compress_type, i.date_time) for i in archive.infolist()] == before
        assert archive.read("pkg/_ext.so") == MH_DYLIB + b"SIGNED"
        record = csv.reader(io.StringIO(archive.read("pkg-1.0.dist-info/RECORD").decode()))
        for name, digest, size in (row for row in record if row and row[1]):
            data = archive.read(name)
            expected = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
            assert digest == "sha256=" + expected.decode() and size == str(len(data))


def test_manifest_tracks_signed_digest_and_keeps_the_locked_origin(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    wheels = runtime / "document-wheels"
    manifest_path = wheels / "manifest.json"
    original = json.loads(manifest_path.read_text())["packages"][0]["sha256"]
    for _ in range(2):  # re-signing keeps the first locked digest as provenance
        resign_archive(wheels / "native-1.0.whl", Signer("-", run=FakeCodesign()))
        update_wheel_manifest(runtime, [wheels / "native-1.0.whl"])
    native, pure = json.loads(manifest_path.read_text())["packages"]
    assert native["codesigned_from"] == original
    assert native["sha256"] == hashlib.sha256((wheels / "native-1.0.whl").read_bytes()).hexdigest()
    assert "codesigned_from" not in pure
    stray = wheels / "unlisted-1.0.whl"
    shutil.copy(wheels / "native-1.0.whl", stray)
    with pytest.raises(ValueError, match="missing from the manifest"):
        update_wheel_manifest(runtime, [stray])


def _details(flags: str, team: str | None, timestamp: bool) -> str:
    lines = [f"CodeDirectory v=20500 size=1 flags={flags} hashes=1+1 location=embedded"]
    if team:
        lines.append(f"TeamIdentifier={team}")
    if timestamp:
        lines.append("Timestamp=Jan 1, 2026 at 00:00:00")
    return "\n".join(lines)


@pytest.mark.parametrize(
    ("flags", "team", "timestamp", "problem"),
    [
        ("0x10000(runtime)", "TEAM123456", True, None),
        ("0x0(none)", "TEAM123456", True, "hardened runtime"),
        ("0x10000(runtime)", "OTHER00000", True, "not signed by team"),
        ("0x10000(runtime)", "TEAM123456", False, "no secure timestamp"),
    ],
)
def test_check_signature_demands_release_properties(
    tmp_path: Path, flags: str, team: str, timestamp: bool, problem: str | None
) -> None:
    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        stderr = _details(flags, team, timestamp) if "--verbose=4" in command else ""
        return subprocess.CompletedProcess(command, 0, "", stderr)

    problems = check_signature(tmp_path / "x", "TEAM123456", None, run)
    assert (problems == []) if problem is None else any(problem in p for p in problems)


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/codesign").exists(),
    reason="needs macOS codesign",
)
def test_adhoc_round_trip_with_real_codesign(tmp_path: Path) -> None:
    """Sign a runtime holding a real Mach-O ad hoc, then verify it end to end."""
    runtime = tmp_path / "runtime"
    (runtime / "python/bin").mkdir(parents=True)
    (runtime / "runtime.json").write_text("{}")
    shutil.copy("/usr/bin/true", runtime / "python/bin/python3.14")
    wheels = runtime / "document-wheels"
    wheels.mkdir()
    _wheel(wheels / "native-1.0.whl", {"pkg/tool": Path("/usr/bin/true").read_bytes()})
    (wheels / "manifest.json").write_text(
        json.dumps(
            {
                "packages": [
                    {
                        "filename": "native-1.0.whl",
                        "sha256": hashlib.sha256(
                            (wheels / "native-1.0.whl").read_bytes()
                        ).hexdigest(),
                    }
                ]
            }
        )
    )
    assert main(["sign", "--runtime", str(runtime), "--identity", "-"]) == 0
    assert main(["verify", "--runtime", str(runtime)]) == 0
    subprocess.run(
        ["codesign", "--force", "--sign", "-", str(runtime / "python/bin/python3.14")],
        check=True,
        capture_output=True,
    )
    assert main(["verify", "--runtime", str(runtime)]) == 1
