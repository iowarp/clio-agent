"""Validate bundled wheels against the canonical, isolated document stack lock."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import sysconfig
import tomllib
from pathlib import Path
from typing import Any


def locked_document_packages(stack: Path) -> list[dict[str, Any]]:
    """Read the exact distributions from the document project's frozen lock."""
    lock = tomllib.loads((stack / "uv.lock").read_text(encoding="utf-8"))
    packages = [package for package in lock["package"] if "virtual" not in package["source"]]
    names = [package["name"] for package in packages]
    if len(names) != len(set(names)):
        raise ValueError("Document wheel bundles require one locked version per distribution")
    return packages


def bundled_wheel_requirements(runtime: Path | None, stack: Path, project: Path) -> Path | None:
    """Verify every bundled wheel and write hash-pinned, offline uv requirements."""
    if runtime is None or not (runtime / "document-wheels/manifest.json").is_file():
        return None
    directory = runtime / "document-wheels"
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    expected_lock = hashlib.sha256((stack / "uv.lock").read_bytes()).hexdigest()
    if (
        manifest["schema"] != 1
        or manifest["lock_sha256"] != expected_lock
        or manifest["cache_tag"] != sys.implementation.cache_tag
        or manifest["platform"] != sysconfig.get_platform()
    ):
        raise ValueError("Bundled document wheels do not match this interpreter and package lock")
    packages = {package["name"]: package for package in locked_document_packages(stack)}
    entries = manifest["packages"]
    if len(entries) != len(packages) or {entry["name"] for entry in entries} != set(packages):
        raise ValueError("Bundled document wheels are incomplete or duplicated")
    requirements = []
    for entry in entries:
        package = packages[entry["name"]]
        sources = [*package.get("wheels", []), package.get("sdist", {})]
        name, version, filename, digest = (
            entry["name"],
            entry["version"],
            entry["filename"],
            entry["sha256"],
        )
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
            or not re.fullmatch(r"[A-Za-z0-9_.+!-]+", version)
            or version != package["version"]
            or not isinstance(entry["source_hash"], str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", entry["source_hash"])
            or entry["source_hash"] not in {source.get("hash") for source in sources}
            or not re.fullmatch(r"[A-Za-z0-9_.+!-]+\.whl", filename)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError(f"Bundled document wheel has invalid locked metadata: {name}")
        wheel = directory / filename
        if wheel.is_symlink():
            raise ValueError(f"Bundled document wheel is a link: {name}")
        with wheel.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest:
            raise ValueError(f"Bundled document wheel checksum mismatch: {name}")
        locked_wheel_hashes = {source.get("hash") for source in package.get("wheels", [])}
        if (
            entry["source_hash"] in locked_wheel_hashes
            and f"sha256:{actual}" != entry["source_hash"]
        ):
            raise ValueError(f"Bundled document wheel differs from the locked download: {name}")
        requirements.append(f"{name}=={version} --hash=sha256:{digest}\n")
    target = project / "bundled-requirements.txt"
    target.write_text("".join(sorted(requirements)), encoding="utf-8")
    return target
