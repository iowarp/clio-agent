"""Prepare hash-verified document wheels at build time instead of on user machines."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen

from packaging.tags import sys_tags
from packaging.utils import parse_wheel_filename

from clio_agent.runtime.document_wheels import locked_document_packages


def select_wheel(package: dict[str, Any]) -> dict[str, Any] | None:
    """Choose the best locked wheel for the interpreter producing this runtime."""
    ranks = {tag: rank for rank, tag in enumerate(sys_tags())}
    candidates = []
    for wheel in package.get("wheels", []):
        filename = unquote(urlsplit(wheel["url"]).path.rsplit("/", 1)[-1])
        tags = parse_wheel_filename(filename)[3]
        compatible = tags & ranks.keys()
        if compatible:
            candidates.append((min(ranks[tag] for tag in compatible), wheel))
    return min(candidates, key=lambda pair: pair[0])[1] if candidates else None


def download_locked(source: dict[str, Any], destination: Path) -> None:
    """Download one lockfile artifact and reject changed or truncated bytes."""
    digest = hashlib.sha256()
    with urlopen(source["url"], timeout=120) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
    if "sha256:" + digest.hexdigest() != source["hash"]:
        raise ValueError(f"Locked document package checksum mismatch: {destination.name}")


def prepare_package(package: dict[str, Any], output: Path, uv: str) -> dict[str, str]:
    """Download a compatible wheel, or build from the verified locked source archive."""
    source = select_wheel(package)
    if source is not None:
        filename = unquote(urlsplit(source["url"]).path.rsplit("/", 1)[-1])
        wheel = output / filename
        download_locked(source, wheel)
    else:
        source = package["sdist"]
        with tempfile.TemporaryDirectory(prefix="clio-wheel-build-") as temporary:
            staging = Path(temporary)
            archive = staging / unquote(urlsplit(source["url"]).path.rsplit("/", 1)[-1])
            download_locked(source, archive)
            wheels = staging / "wheels"
            subprocess.run(
                [
                    uv,
                    "build",
                    "--wheel",
                    "--python",
                    sys.executable,
                    "--out-dir",
                    str(wheels),
                    str(archive),
                ],
                check=True,
                timeout=180,
            )
            built = list(wheels.glob("*.whl"))
            if len(built) != 1:
                raise ValueError(f"Expected one built wheel for {package['name']}")
            wheel = output / built[0].name
            shutil.copyfile(built[0], wheel)
    with wheel.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    print(f"Prepared locked document wheel: {package['name']} {package['version']}", flush=True)
    return {
        "name": package["name"],
        "version": package["version"],
        "filename": wheel.name,
        "sha256": digest,
        "source_hash": source["hash"],
    }


def main() -> None:
    """Build a complete wheel bundle for the same target interpreter as the app."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--uv", required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    packages = locked_document_packages(args.stack)
    with ThreadPoolExecutor(max_workers=4) as executor:
        entries = list(
            executor.map(lambda package: prepare_package(package, args.out, args.uv), packages)
        )
    manifest = {
        "schema": 1,
        "cache_tag": sys.implementation.cache_tag,
        "platform": sysconfig.get_platform(),
        "lock_sha256": hashlib.sha256((args.stack / "uv.lock").read_bytes()).hexdigest(),
        "packages": entries,
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
