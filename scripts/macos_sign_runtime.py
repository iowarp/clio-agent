"""Developer ID-sign every Mach-O in the bundled macOS runtime, inside out.

Tauri signs the app, its main binary and the sidecar launcher, but copies
``Contents/Resources/gact-runtime`` as opaque resources. The notary service
rejects any Mach-O in the app that lacks a Developer ID signature, the hardened
runtime or a secure timestamp, including members of zip archives such as the
offline document wheels. ``sign`` signs all of them before ``tauri build``
copies the runtime into the bundle; ``verify`` proves each one carries the
release team, the hardened runtime, a timestamp and exactly the entitlements
its role needs (``install/macos/*.entitlements``).

Signing a wheel member changes the wheel's bytes, so ``sign`` rewrites the
wheel's ``RECORD`` and the ``document-wheels/manifest.json`` digest, and keeps
the locked download's digest as ``codesigned_from`` for provenance.

    python scripts/macos_sign_runtime.py sign --runtime DIR --identity "$APPLE_SIGNING_IDENTITY"
    python scripts/macos_sign_runtime.py verify --runtime DIR --team-id "$APPLE_TEAM_ID"

``--identity -`` signs ad hoc (no timestamp) for local pipeline checks.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, TypeVar

REPO = Path(__file__).resolve().parents[1]
ENTITLEMENTS_DIR = REPO / "install" / "macos"
BUNDLE_SUFFIXES = (".app", ".framework", ".bundle", ".xpc", ".plugin")
ARCHIVE_SUFFIXES = (".whl", ".zip")
_THIN_BIG = {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf"}
_THIN_LITTLE = {b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"}
_FAT = {b"\xca\xfe\xba\xbe": 20, b"\xca\xfe\xba\xbf": 32}
_MH_OBJECT, _MH_EXECUTE, _MH_DSYM = 1, 2, 10

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]
Item = TypeVar("Item")
_RUNTIME_FLAG = re.compile(r"flags=0x[0-9a-f]+\([^)]*\bruntime\b")


def macho_kind(stream: IO[bytes]) -> str | None:
    """Classify a stream as a signable ``executable``/``library`` Mach-O, or None."""
    header = stream.read(32)
    magic = header[:4]
    if magic in _FAT:
        count = int.from_bytes(header[4:8], "big")
        # Java class files share 0xCAFEBABE; their "count" is the class version (>= 45).
        if not 0 < count < 20:
            return None
        offset = (
            int.from_bytes(header[16:20], "big")
            if magic == b"\xca\xfe\xba\xbe"
            else int.from_bytes(header[16:24], "big")
        )
        stream.seek(offset)
        header = stream.read(16)
        magic = header[:4]
    if magic in _THIN_BIG:
        filetype = int.from_bytes(header[12:16], "big")
    elif magic in _THIN_LITTLE:
        filetype = int.from_bytes(header[12:16], "little")
    else:
        return None
    if filetype in {_MH_OBJECT, _MH_DSYM}:
        return None
    return "executable" if filetype == _MH_EXECUTE else "library"


def entitlements_for(relative: PurePosixPath) -> str | None:
    """Name the entitlements file a runtime executable needs, or None for none."""
    if relative.parent == PurePosixPath("python/bin") and relative.name.startswith("python"):
        # Loads extension modules from uv-managed MCP environments, document
        # environments and the provider overlay, none signed by our team.
        return "gact-runtime.entitlements"
    if relative.parent.name == "bin" and relative.parent.parent.name == "iowarp_core":
        # Started with DYLD_LIBRARY_PATH (arc/runtime_spawn.py).
        return "clio-run.entitlements"
    return None


@dataclass(frozen=True)
class Target:
    """One loose Mach-O file and how to sign it."""

    path: Path
    executable: bool
    entitlements: str | None


@dataclass(frozen=True)
class Inventory:
    """Everything in a runtime that carries code."""

    files: list[Target]
    archives: list[Path]
    bundles: list[Path]


def _file_kind(path: Path) -> str | None:
    with path.open("rb") as stream:
        return macho_kind(stream)


def _archive_members(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str]]:
    found = []
    for info in archive.infolist():
        if info.is_dir() or info.file_size < 32:
            continue
        with archive.open(info) as stream:
            kind = macho_kind(stream)
        if kind:
            found.append((info, kind))
    return found


def inventory(runtime: Path) -> Inventory:
    """Find every Mach-O, code bundle and Mach-O-bearing archive by content."""
    files: list[Target] = []
    archives: list[Path] = []
    bundles: list[Path] = []
    for root, dirs, names in os.walk(runtime):
        base = Path(root)
        bundles.extend(base / name for name in dirs if name.endswith(BUNDLE_SUFFIXES))
        for name in names:
            path = base / name
            if path.is_symlink() or not path.is_file():
                continue
            kind = _file_kind(path)
            if kind:
                relative = PurePosixPath(path.relative_to(runtime).as_posix())
                executable = kind == "executable"
                entitlements = entitlements_for(relative) if executable else None
                files.append(Target(path, executable, entitlements))
            elif name.endswith(ARCHIVE_SUFFIXES) and zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as archive:
                    if _archive_members(archive):
                        archives.append(path)
    bundles.sort(key=lambda path: len(path.parts), reverse=True)
    return Inventory(sorted(files, key=lambda t: t.path), sorted(archives), bundles)


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(command)} failed: {result.stderr.strip()}")
    return result


@dataclass(frozen=True)
class Signer:
    """``codesign`` with the release identity, hardened runtime and a timestamp."""

    identity: str
    keychain: str | None = None
    entitlements_dir: Path = ENTITLEMENTS_DIR
    run: Runner = _run

    def command(self, path: Path, entitlements: str | None) -> list[str]:
        """The exact codesign invocation for one path."""
        command = ["codesign", "--force", "--sign", self.identity, "--options", "runtime"]
        command.append("--timestamp=none" if self.identity == "-" else "--timestamp")
        if self.keychain:
            command += ["--keychain", self.keychain]
        if entitlements:
            command += ["--entitlements", str(self.entitlements_dir / entitlements)]
        return [*command, str(path)]

    def sign(self, path: Path, entitlements: str | None = None) -> None:
        """Sign one file or bundle directory."""
        self.run(self.command(path, entitlements))


def _record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + digest.decode("ascii")


def _rewrite_record(record: bytes, replaced: dict[str, bytes]) -> bytes:
    rows = list(csv.reader(io.StringIO(record.decode("utf-8"))))
    for row in rows:
        if row and row[0] in replaced:
            data = replaced[row[0]]
            row[1:3] = [_record_hash(data), str(len(data))]
    output = io.StringIO()
    csv.writer(output, lineterminator="\n").writerows(rows)
    return output.getvalue().encode("utf-8")


def resign_archive(path: Path, signer: Signer) -> None:
    """Sign an archive's Mach-O members in place and keep a wheel's RECORD valid."""
    replaced: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as archive, tempfile.TemporaryDirectory() as temporary:
        members = _archive_members(archive)
        for index, (info, kind) in enumerate(members):
            target = Path(temporary) / str(index) / PurePosixPath(info.filename).name
            target.parent.mkdir()
            target.write_bytes(archive.read(info))
            target.chmod(0o755 if kind == "executable" else 0o644)
            signer.sign(target)
            replaced[info.filename] = target.read_bytes()
        rewritten = path.with_name(path.name + ".signing")
        with zipfile.ZipFile(rewritten, "w") as output:
            for info in archive.infolist():
                data = replaced.get(info.filename)
                if data is None:
                    data = archive.read(info)
                    if info.filename.endswith(".dist-info/RECORD"):
                        data = _rewrite_record(data, replaced)
                clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                clone.compress_type = info.compress_type
                clone.external_attr = info.external_attr
                clone.create_system = info.create_system
                output.writestr(clone, data)
    os.replace(rewritten, path)


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def update_wheel_manifest(runtime: Path, resigned: Iterable[Path]) -> None:
    """Point the document-wheel manifest at the signed wheels' digests."""
    manifest_path = runtime / "document-wheels" / "manifest.json"
    names = {path.name: path for path in resigned if path.parent == manifest_path.parent}
    if not names:
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["packages"]:
        wheel = names.pop(entry["filename"], None)
        if wheel is not None:
            entry.setdefault("codesigned_from", entry["sha256"])
            entry["sha256"] = _sha256(wheel)
    if names:
        raise ValueError(f"signed wheels missing from the manifest: {sorted(names)}")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _parallel(jobs: int, work: Callable[[Item], None], items: Iterable[Item]) -> None:
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        list(executor.map(work, items))


def sign_runtime(runtime: Path, signer: Signer, jobs: int = 8) -> Inventory:
    """Sign archives, libraries, executables, then bundles: always inside out."""
    signer.run(["xattr", "-cr", str(runtime)])
    found = inventory(runtime)
    _parallel(jobs, lambda path: resign_archive(path, signer), found.archives)
    update_wheel_manifest(runtime, found.archives)
    libraries = [target for target in found.files if not target.executable]
    executables = [target for target in found.files if target.executable]
    for group in (libraries, executables):
        _parallel(jobs, lambda t: signer.sign(t.path, t.entitlements), group)
    for bundle in found.bundles:
        signer.sign(bundle)
    return found


def _expected_entitlements(name: str | None, directory: Path) -> dict[str, object]:
    if name is None:
        return {}
    return plistlib.loads((directory / name).read_bytes())


def check_signature(
    path: Path,
    team_id: str | None,
    entitlements: str | None,
    run: Runner = _run,
    entitlements_dir: Path = ENTITLEMENTS_DIR,
) -> list[str]:
    """Return what is wrong with one signed file (empty when release-ready)."""
    problems = []
    try:
        run(["codesign", "--verify", "--strict", str(path)])
    except RuntimeError as error:
        return [f"{path}: {error}"]
    details = run(["codesign", "-d", "--verbose=4", str(path)]).stderr
    if not _RUNTIME_FLAG.search(details):
        problems.append(f"{path}: hardened runtime flag missing")
    if team_id is not None:
        if f"TeamIdentifier={team_id}" not in details:
            problems.append(f"{path}: not signed by team {team_id}")
        if "Timestamp=" not in details:
            problems.append(f"{path}: no secure timestamp")
    raw = run(["codesign", "-d", "--entitlements", "-", "--xml", str(path)]).stdout.strip()
    actual = plistlib.loads(raw.encode()) if raw else {}
    expected = _expected_entitlements(entitlements, entitlements_dir)
    if actual != expected:
        problems.append(f"{path}: entitlements {sorted(actual)} != expected {sorted(expected)}")
    return problems


def verify_runtime(
    runtime: Path, team_id: str | None, run: Runner = _run, jobs: int = 8
) -> list[str]:
    """Check every loose Mach-O, archive member and bundle in the runtime."""
    found = inventory(runtime)
    problems: list[str] = []

    def loose(target: Target) -> list[str]:
        return check_signature(target.path, team_id, target.entitlements, run)

    with ThreadPoolExecutor(max_workers=jobs) as executor:
        for result in executor.map(loose, found.files):
            problems.extend(result)
    for archive_path in found.archives:
        with zipfile.ZipFile(archive_path) as archive, tempfile.TemporaryDirectory() as temporary:
            for index, (info, _kind) in enumerate(_archive_members(archive)):
                target = Path(temporary) / str(index) / PurePosixPath(info.filename).name
                target.parent.mkdir()
                target.write_bytes(archive.read(info))
                for problem in check_signature(target, team_id, None, run):
                    problems.append(f"{archive_path.name}!{info.filename}: {problem}")
    for bundle in found.bundles:
        problems.extend(check_signature(bundle, team_id, None, run))
    manifest = runtime / "document-wheels" / "manifest.json"
    if manifest.is_file():
        for entry in json.loads(manifest.read_text(encoding="utf-8"))["packages"]:
            wheel = manifest.parent / entry["filename"]
            if _sha256(wheel) != entry["sha256"]:
                problems.append(f"{wheel.name}: manifest digest does not match the signed wheel")
    print(
        f"checked {len(found.files)} Mach-O files, {len(found.archives)} archives, "
        f"{len(found.bundles)} bundles",
        flush=True,
    )
    return problems


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="action", required=True)
    sign = sub.add_parser("sign", help="sign every Mach-O in the runtime")
    sign.add_argument("--identity", required=True)
    sign.add_argument("--keychain")
    verify = sub.add_parser("verify", help="prove every Mach-O is release-signed")
    verify.add_argument("--team-id", help="required team; omit only for ad hoc checks")
    for command in (sign, verify):
        command.add_argument("--runtime", required=True, type=Path)
        command.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args(argv)
    if not (args.runtime / "runtime.json").is_file():
        parser.error(f"{args.runtime} is not a built runtime (runtime.json missing)")
    if args.action == "sign":
        found = sign_runtime(args.runtime, Signer(args.identity, args.keychain), args.jobs)
        executables = [t for t in found.files if t.executable]
        print(
            f"signed {len(found.files)} Mach-O files ({len(executables)} executables), "
            f"{len(found.archives)} archives, {len(found.bundles)} bundles",
            flush=True,
        )
        for target in executables:
            if target.entitlements:
                print(f"  {target.path.relative_to(args.runtime)}: {target.entitlements}")
        return 0
    problems = verify_runtime(args.runtime, args.team_id, jobs=args.jobs)
    for problem in problems:
        print(f"::error::{problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
