#!/usr/bin/env python3
"""Pin the private Linux userland the bundled LibreOffice runs on.

TDF's Linux build links against about forty desktop system libraries (X11,
cairo, cups, fontconfig, NSS, glib, ...) and needs glibc 2.34. A headless
server, an HPC node or a minimal container has few of them and often an older
glibc, so Office must bring its own. This script resolves exactly the shared
libraries the pinned LibreOffice program needs (and what those need, to a fixed
point) to Ubuntu 22.04 packages as of one snapshot timestamp, and writes one
package set per architecture, with each file's URL and SHA-256 value, to
``src/clio_agent/runtime/document_stack/office_userland.lock.json``.

    python scripts/office_userland_lock.py --arch amd64 \\
        --program <extracted x86_64>/opt/libreoffice26.2/program \\
        --work /tmp/office-lock-amd64 --snapshot 20261020T000000Z
    python scripts/office_userland_lock.py --arch arm64 \\
        --program <extracted aarch64>/opt/libreoffice26.2/program \\
        --work /tmp/office-lock-arm64 --snapshot 20261020T000000Z

amd64 packages come from snapshot.ubuntu.com at the timestamp. snapshot.ubuntu.com
does not serve the ubuntu-ports archive that holds arm64, so arm64 packages are
pinned to their Launchpad librarian files, which never change once published:
each one is the version the amd64 set pins when Launchpad published it for arm64,
otherwise the newest jammy/jammy-updates/jammy-security version published by the
timestamp. Run it again when LibreOffice's pin or the snapshot changes; the
runtime reads only the lock (``office_compat``).
"""

from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import io
import json
import lzma
import re
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import cmp_to_key
from pathlib import Path
from typing import Any

import zstandard

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "src/clio_agent/runtime/document_stack/office_userland.lock.json"
SERIES = "jammy"
SUITES = (SERIES, f"{SERIES}-updates", f"{SERIES}-security")
POCKETS = ("Release", "Updates", "Security")
COMPONENTS = ("main", "universe")
TRIPLET = {"amd64": "x86_64-linux-gnu", "arm64": "aarch64-linux-gnu"}
#: Where each architecture's index files (Packages, Contents) are published.
ARCHIVE = {"amd64": "https://snapshot.ubuntu.com/ubuntu/{snapshot}", "arm64": None}
PORTS = "https://ports.ubuntu.com/ubuntu-ports"
LAUNCHPAD = "https://api.launchpad.net/devel"
LIBRARIAN = "https://launchpadlibrarian.net"
#: Desktop toolkits a headless renderer never loads (it uses the svp backend).
GUI_PLUGIN = re.compile(r"(gtk|kf5|kf6|qt5|qt6|avmediagst|vclplug_(?!svp))", re.IGNORECASE)
#: Always private: the C/C++ runtime itself (the program's loader comes from here).
BASE_PACKAGES = ("libc6", "libgcc-s1", "libstdc++6", "gcc-12-base")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def read(url: str) -> bytes:
    """GET a URL, retrying the transient failures Launchpad's services return."""
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - https
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt == 4:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 4:
                raise
        print(f"retrying {url} (attempt {attempt + 2} of 5)")
        time.sleep(2**attempt)
    raise AssertionError("unreachable")


def fetch(url: str, target: Path) -> Path:
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.with_suffix(".part").write_bytes(read(url))
        target.with_suffix(".part").replace(target)
    return target


def get_json(url: str) -> Any:
    return json.loads(read(url))


def compare(a: str, b: str) -> int:
    if a == b:
        return 0
    lower = subprocess.run(["dpkg", "--compare-versions", a, "lt", b], check=False)
    return -1 if lower.returncode == 0 else 1


def newer(a: str, b: str) -> bool:
    return compare(a, b) > 0


class Snapshot:
    """Packages at one snapshot.ubuntu.com timestamp (the archive holds amd64)."""

    def __init__(self, arch: str, snapshot: str, work: Path) -> None:
        self.base = ARCHIVE[arch].format(snapshot=snapshot)  # type: ignore[union-attr]
        self.index: dict[str, dict[str, str]] = {}
        for suite in SUITES:
            for component in COMPONENTS:
                url = f"{self.base}/dists/{suite}/{component}/binary-{arch}/Packages.xz"
                text = lzma.decompress(
                    fetch(url, work / suite / component / "Packages.xz").read_bytes()
                )
                for stanza in text.decode("utf-8", "replace").split("\n\n"):
                    fields = dict(re.findall(r"^([A-Za-z0-9-]+): (.*)$", stanza, re.MULTILINE))
                    name = fields.get("Package")
                    if name and (
                        name not in self.index
                        or newer(fields["Version"], self.index[name]["Version"])
                    ):
                        self.index[name] = fields

    def resolve(self, name: str) -> dict[str, Any] | None:
        fields = self.index.get(name)
        if fields is None:
            return None
        return {
            "package": name,
            "version": fields["Version"],
            "path": fields["Filename"],
            "sha256": fields["SHA256"],
            "size": int(fields["Size"]),
        }


class Launchpad:
    """Packages Launchpad published for one ports architecture, as of the snapshot time.

    Pins are librarian files: ``https://launchpadlibrarian.net/<id>/<file>`` is the
    target of the archive's ``+files`` redirect and is immutable once published,
    unlike ports.ubuntu.com ``pool/`` paths, which disappear on supersession.
    """

    def __init__(self, arch: str, snapshot: str, preferred: dict[str, str]) -> None:
        self.base = LIBRARIAN
        self.series = f"{LAUNCHPAD}/ubuntu/{SERIES}/{arch}"
        self.cutoff = datetime.datetime.strptime(snapshot, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=datetime.UTC
        )
        self.preferred = preferred
        self.deviations: dict[str, tuple[str, str]] = {}
        self.opener = urllib.request.build_opener(_NoRedirect)

    def _publications(self, name: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {
                "ws.op": "getPublishedBinaries",
                "binary_name": name,
                "exact_match": "true",
                "distro_arch_series": self.series,
                "ws.size": "300",
            }
        )
        url: str | None = f"{LAUNCHPAD}/ubuntu/+archive/primary?{query}"
        entries: list[dict[str, Any]] = []
        while url:
            page = get_json(url)
            entries += page["entries"]
            url = page.get("next_collection_link")
        return [
            entry
            for entry in entries
            if entry["pocket"] in POCKETS
            and entry["status"] != "Deleted"
            and datetime.datetime.fromisoformat(entry["date_published"] or "9999-01-01T00:00Z")
            <= self.cutoff
        ]

    def _librarian(self, url: str) -> str:
        """The immutable librarian file the archive's ``+files`` URL redirects to."""
        for attempt in range(5):
            try:
                self.opener.open(urllib.request.Request(url, method="HEAD"), timeout=120)
            except urllib.error.HTTPError as exc:
                location = exc.headers.get("Location", "")
                if exc.code in {301, 302, 303, 307} and location.startswith(f"{LIBRARIAN}/"):
                    return location
                if exc.code < 500 or attempt == 4:
                    raise
                time.sleep(2**attempt)
                continue
            break
        raise SystemExit(f"{url} did not redirect to the Launchpad librarian")

    def resolve(self, name: str) -> dict[str, Any] | None:
        published = self._publications(name)
        if not published:
            return None
        versions = sorted(
            {entry["binary_package_version"] for entry in published}, key=cmp_to_key(compare)
        )
        version = versions[-1]
        wanted = self.preferred.get(name)
        if wanted in versions:
            version = wanted
        elif wanted is not None:
            self.deviations[name] = (wanted, version)
        entry = next(e for e in published if e["binary_package_version"] == version)
        link = entry["self_link"].replace("/1.0/", "/devel/")
        files = get_json(f"{link}?ws.op=binaryFileUrls&include_meta=true")
        (file,) = [f for f in files if f["url"].endswith(".deb")]
        librarian = self._librarian(file["url"])
        return {
            "package": name,
            "version": version,
            "path": librarian.removeprefix(f"{LIBRARIAN}/"),
            "sha256": file["sha256"],
            "size": int(file["size"]),
        }


def contents_index(base: str, arch: str, work: Path) -> dict[str, str]:
    """Library file path -> package, for the arch's library directories."""
    owners: dict[str, str] = {}
    triplet = TRIPLET[arch]
    for suite in SUITES:
        path = fetch(
            f"{base}/dists/{suite}/Contents-{arch}.gz", work / suite / f"Contents-{arch}.gz"
        )
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
    triplet = TRIPLET[args.arch]
    lock = json.loads(LOCK.read_text()) if LOCK.is_file() else {}
    if lock.get("snapshot", args.snapshot) != args.snapshot:
        raise SystemExit("Every architecture is pinned at one snapshot; regenerate them all")
    source: Snapshot | Launchpad
    if ARCHIVE[args.arch] is not None:
        source = Snapshot(args.arch, args.snapshot, args.work / "index")
        index_base = source.base
    else:
        amd64 = lock.get("architectures", {}).get("amd64", {}).get("packages", [])
        preferred = {item["package"]: item["version"] for item in amd64}
        source = Launchpad(args.arch, args.snapshot, preferred)
        index_base = PORTS
    owners = contents_index(index_base, args.arch, args.work / "index")

    shipped = {p.name for p in args.program.iterdir()}
    wanted: set[str] = set()
    for path in args.program.iterdir():
        if path.is_file() and is_elf(path) and not GUI_PLUGIN.search(path.name):
            wanted |= needed(path)
    provided: set[str] = set(shipped)
    chosen: dict[str, dict[str, Any]] = {}
    queue = [*BASE_PACKAGES]
    unresolved: set[str] = set()
    while True:
        for name in queue:
            if name in chosen:
                continue
            pin = source.resolve(name)
            if pin is None:
                raise SystemExit(f"{name}: not published for {args.arch}")
            chosen[name] = pin
            deb = fetch(f"{source.base}/{pin['path']}", args.work / "debs" / Path(pin["path"]).name)
            if hashlib.sha256(deb.read_bytes()).hexdigest() != pin["sha256"]:
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
            owner = owners.get(f"usr/lib/{triplet}/{soname}") or owners.get(
                f"lib/{triplet}/{soname}"
            )
            if owner is None:
                unresolved.add(soname)
            elif owner not in chosen:
                queue.append(owner)
            else:
                unresolved.add(soname)  # owned by a chosen package yet not shipped there
        if not queue:
            break
    lock.update(
        {
            "comment": "Generated by scripts/office_userland_lock.py; do not edit by hand.",
            "snapshot": args.snapshot,
        }
    )
    lock.setdefault("architectures", {})[args.arch] = {
        "base": source.base,
        "triplet": triplet,
        "packages": [chosen[name] for name in sorted(chosen)],
        "unresolved": sorted(unresolved),
    }
    LOCK.write_text(json.dumps(lock, indent=1) + "\n")
    total = sum(pin["size"] for pin in chosen.values())
    print(f"{len(chosen)} packages, {total / 1e6:.1f} MB; unresolved: {sorted(unresolved)}")
    for name, (wanted_version, version) in sorted(getattr(source, "deviations", {}).items()):
        print(f"{name}: amd64 pins {wanted_version}, {args.arch} has {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
