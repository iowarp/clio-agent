#!/usr/bin/env python3
"""Pin the private Linux userland the bundled LibreOffice runs on.

TDF's Linux build links against about forty desktop system libraries (X11,
cairo, cups, fontconfig, NSS, glib, ...) and needs glibc 2.34. A headless
server, an HPC node or a minimal container has few of them and often an older
glibc, so Office must bring its own. This script resolves exactly the shared
libraries the pinned LibreOffice program needs (and what those need, to a fixed
point) to Ubuntu 22.04 packages at one snapshot.ubuntu.com timestamp, and
writes their pinned paths and SHA-256 values to
``src/clio_agent/runtime/document_stack/office_userland.lock.json``.

    python scripts/office_userland_lock.py --program <extracted>/opt/libreoffice26.2/program \\
        --work /tmp/office-lock --snapshot 20261001T000000Z

Run it again when LibreOffice's pin or the snapshot changes; the runtime reads
only the lock (``office_compat``).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import lzma
import re
import subprocess
import tarfile
import urllib.request
from pathlib import Path

import zstandard

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "src/clio_agent/runtime/document_stack/office_userland.lock.json"
SUITES = ("jammy", "jammy-updates", "jammy-security")
COMPONENTS = ("main", "universe")
TRIPLET = {"amd64": "x86_64-linux-gnu"}
#: Desktop toolkits a headless renderer never loads (it uses the svp backend).
GUI_PLUGIN = re.compile(r"(gtk|kf5|kf6|qt5|qt6|avmediagst|vclplug_(?!svp))", re.IGNORECASE)
#: Always private: the C/C++ runtime itself (the program's loader comes from here).
BASE_PACKAGES = ("libc6", "libgcc-s1", "libstdc++6", "gcc-12-base")


def fetch(url: str, target: Path) -> Path:
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - pinned https
            target.write_bytes(response.read())
    return target


def newer(a: str, b: str) -> bool:
    return subprocess.run(["dpkg", "--compare-versions", a, "gt", b], check=False).returncode == 0


def packages_index(base: str, arch: str, work: Path) -> dict[str, dict[str, str]]:
    """Every package at the snapshot, newest version per name."""
    best: dict[str, dict[str, str]] = {}
    for suite in SUITES:
        for component in COMPONENTS:
            url = f"{base}/dists/{suite}/{component}/binary-{arch}/Packages.xz"
            text = lzma.decompress(fetch(url, work / suite / component / "Packages.xz").read_bytes())
            for stanza in text.decode("utf-8", "replace").split("\n\n"):
                fields = dict(re.findall(r"^([A-Za-z0-9-]+): (.*)$", stanza, re.MULTILINE))
                name = fields.get("Package")
                if name and (name not in best or newer(fields["Version"], best[name]["Version"])):
                    best[name] = fields
    return best


def contents_index(base: str, arch: str, work: Path) -> dict[str, str]:
    """Library file path -> package, for the arch's library directories."""
    owners: dict[str, str] = {}
    triplet = TRIPLET[arch]
    for suite in SUITES:
        path = fetch(f"{base}/dists/{suite}/Contents-{arch}.gz", work / suite / f"Contents-{arch}.gz")
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as lines:
            for line in lines:
                if f"lib/{triplet}/" not in line:
                    continue
                file, _, location = line.rstrip("\n").rpartition(" ")
                owners.setdefault(file.strip(), location.strip().split(",")[0].split("/")[-1])
    return owners


def needed(path: Path) -> set[str]:
    output = subprocess.run(["objdump", "-p", str(path)], capture_output=True, text=True).stdout
    return set(re.findall(r"^\s+NEEDED\s+(\S+)$", output, re.MULTILINE))


def is_elf(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(4) == b"\x7fELF"
    except OSError:
        return False


def deb_payload(path: Path) -> bytes:
    data = path.read_bytes()
    position = 8
    while position < len(data):
        header = data[position : position + 60]
        size = int(header[48:58])
        name = header[:16].decode().strip().rstrip("/")
        payload = data[position + 60 : position + 60 + size]
        if name.startswith("data.tar"):
            if name.endswith(".zst"):
                return zstandard.ZstdDecompressor().stream_reader(payload).read()
            return lzma.decompress(payload) if name.endswith(".xz") else gzip.decompress(payload)
        position += 60 + size + (size % 2)
    raise ValueError(f"{path} has no data member")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--program", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--arch", default="amd64", choices=sorted(TRIPLET))
    args = parser.parse_args()
    base = f"https://snapshot.ubuntu.com/ubuntu/{args.snapshot}"
    triplet = TRIPLET[args.arch]
    index = packages_index(base, args.arch, args.work / "index")
    owners = contents_index(base, args.arch, args.work / "index")

    shipped = {p.name for p in args.program.iterdir()}
    wanted: set[str] = set()
    for path in args.program.iterdir():
        if path.is_file() and is_elf(path) and not GUI_PLUGIN.search(path.name):
            wanted |= needed(path)
    provided: set[str] = set(shipped)
    chosen: dict[str, dict[str, str]] = {}
    queue = [*BASE_PACKAGES]
    unresolved: set[str] = set()
    while True:
        for name in queue:
            if name in chosen:
                continue
            fields = index[name]
            chosen[name] = fields
            deb = fetch(f"{base}/{fields['Filename']}", args.work / "debs" / Path(fields["Filename"]).name)
            if hashlib.sha256(deb.read_bytes()).hexdigest() != fields["SHA256"]:
                raise SystemExit(f"{name}: SHA-256 mismatch")
            image = args.work / "image"
            with tarfile.open(fileobj=io.BytesIO(deb_payload(deb))) as archive:
                kept = [m for m in archive if not (m.issym() and m.linkname.startswith("/"))]
                archive.extractall(image, members=kept, filter="data")
                members = [m.name for m in kept]
            for member in members:
                path = image / member
                if f"lib/{triplet}/" in member and path.is_file() and is_elf(path):
                    provided.add(path.name)
                    wanted |= needed(path)
                elif f"lib/{triplet}/" in member and path.is_symlink():
                    provided.add(path.name)
        missing = sorted(wanted - provided - unresolved)
        queue = []
        for soname in missing:
            owner = owners.get(f"usr/lib/{triplet}/{soname}") or owners.get(f"lib/{triplet}/{soname}")
            if owner is None or owner not in index:
                unresolved.add(soname)
            elif owner not in chosen:
                queue.append(owner)
            else:
                unresolved.add(soname)  # owned by a chosen package yet not shipped there
        if not queue:
            break
    lock = json.loads(LOCK.read_text()) if LOCK.is_file() else {}
    lock.update(
        {
            "comment": "Generated by scripts/office_userland_lock.py; do not edit by hand.",
            "snapshot": args.snapshot,
            "base": "https://snapshot.ubuntu.com/ubuntu",
        }
    )
    lock.setdefault("architectures", {})[args.arch] = {
        "triplet": triplet,
        "packages": [
            {
                "package": name,
                "version": chosen[name]["Version"],
                "path": chosen[name]["Filename"],
                "sha256": chosen[name]["SHA256"],
                "size": int(chosen[name]["Size"]),
            }
            for name in sorted(chosen)
        ],
        "unresolved": sorted(unresolved),
    }
    LOCK.write_text(json.dumps(lock, indent=1) + "\n")
    total = sum(int(f["Size"]) for f in chosen.values())
    print(f"{len(chosen)} packages, {total / 1e6:.1f} MB; unresolved: {sorted(unresolved)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
