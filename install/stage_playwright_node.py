"""Replace Playwright's bundled Node.js with a record of where to fetch it on first use.

Run by install/build-gact-runtime.sh and .ps1 with the built runtime's own Python:

    python -I -B install/stage_playwright_node.py <site-packages>

The ``playwright`` wheel ships the official Node.js binary its driver runs on
(``playwright/driver/node`` or ``node.exe``): 122 MB unpacked, 28 MB of the macOS download,
used only once the agent first opens a browser. This step finds that same binary,
byte for byte, inside the nodejs.org release archive, and writes
``playwright/driver/clio-node.json`` naming the archive, its SHA-256 (from nodejs.org's
SHASUMS256.txt, checked against the download) and the binary's SHA-256 (the hash of the file
the hash-locked wheel installed). Then it deletes the binary and its RECORD line. At runtime
``clio_agent.gact.browser.driver`` fetches the archive on first browser use and accepts only
that binary.

The build fails if the archive's binary differs from the wheel's, so the runtime never runs a
Node.js other than the one this Playwright release was built and tested with.
"""

from __future__ import annotations

import hashlib
import io
import json
import platform
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

DIST = "https://nodejs.org/dist"
MANIFEST = "clio-node.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "clio-agent-build"})
    with urllib.request.urlopen(request, timeout=120) as answer:  # noqa: S310 - nodejs.org
        return answer.read()


def _target(version: str) -> tuple[str, str]:
    """The release archive and the binary's member path for this build machine."""
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    if arch is None:
        raise SystemExit(f"stage_playwright_node: no Node.js build for machine {machine!r}")
    if sys.platform == "darwin":
        stem = f"node-v{version}-darwin-{arch}"
        return f"{stem}.tar.xz", f"{stem}/bin/node"
    if sys.platform.startswith("linux"):
        stem = f"node-v{version}-linux-{arch}"
        return f"{stem}.tar.xz", f"{stem}/bin/node"
    if sys.platform == "win32":
        stem = f"node-v{version}-win-{arch}"
        return f"{stem}.zip", f"{stem}/node.exe"
    raise SystemExit(f"stage_playwright_node: unsupported platform {sys.platform}")


def _member(archive: bytes, name: str, member: str) -> bytes:
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            return bundle.read(member)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as bundle:
        source = bundle.extractfile(member)
        if source is None:
            raise SystemExit(f"stage_playwright_node: {member} is not a file in {name}")
        return source.read()


def _drop_record_line(site: Path, relative: str) -> None:
    records = sorted(site.glob("playwright-*.dist-info/RECORD"))
    if not records:
        raise SystemExit("stage_playwright_node: playwright's dist-info RECORD is missing")
    for record in records:
        lines = record.read_text(encoding="utf-8").splitlines()
        kept = [line for line in lines if not line.startswith(f"{relative},")]
        if len(kept) == len(lines):
            raise SystemExit(f"stage_playwright_node: {record} does not list {relative}")
        kept.append(f"playwright/driver/{MANIFEST},,")
        record.write_text("\n".join(kept) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit("usage: stage_playwright_node.py <site-packages>")
    site = Path(argv[1])
    driver = site / "playwright" / "driver"
    if not driver.is_dir():
        print("stage_playwright_node: playwright is not installed; nothing to stage")
        return 0
    executable = "node.exe" if sys.platform == "win32" else "node"
    node = driver / executable
    if not node.is_file():
        raise SystemExit(f"stage_playwright_node: {node} is missing")
    done = subprocess.run(  # noqa: S603 - the wheel's own pinned binary
        [str(node), "--version"], capture_output=True, text=True, timeout=60, check=True
    )
    version = done.stdout.strip().removeprefix("v")
    wheel_node = node.read_bytes()
    node_sha = _sha256(wheel_node)

    name, member = _target(version)
    url = f"{DIST}/v{version}/{name}"
    sums = _fetch(f"{DIST}/v{version}/SHASUMS256.txt").decode("ascii")
    listed = {line.split()[1]: line.split()[0] for line in sums.splitlines() if line.strip()}
    if name not in listed:
        raise SystemExit(f"stage_playwright_node: SHASUMS256.txt does not list {name}")
    archive = _fetch(url)
    if _sha256(archive) != listed[name]:
        raise SystemExit(f"stage_playwright_node: {url} does not match SHASUMS256.txt")
    if _sha256(_member(archive, name, member)) != node_sha:
        raise SystemExit(
            f"stage_playwright_node: {member} in {name} is not the binary the playwright wheel "
            "ships; keep the bundled one or pin a matching Node.js"
        )

    record = {
        "schema": 1,
        "node_version": version,
        "url": url,
        "sha256": listed[name],
        "size": len(archive),
        "member": member,
        "node_sha256": node_sha,
        "node_size": len(wheel_node),
        "executable": executable,
    }
    (driver / MANIFEST).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    node.unlink()
    _drop_record_line(site, f"playwright/driver/{executable}")
    print(
        f"stage_playwright_node: node v{version} ({len(wheel_node) / 1e6:.0f} MB) is fetched "
        f"on first browser use from {url} ({len(archive) / 1e6:.0f} MB)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
