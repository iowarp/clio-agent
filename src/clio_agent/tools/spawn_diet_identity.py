"""Bound cached legacy launch plans to the actual installed toolkit, not its shim."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname


def launcher_identity(command: str) -> str:
    """Fingerprint bounded launcher/install metadata without spawning or source walks."""
    launcher = Path(command).resolve(strict=True)
    prefixes = {launcher.parent.parent}
    with launcher.open("rb") as stream:
        first_line = stream.readline(4096)
    if launcher.suffix.lower() == ".exe":
        try:
            # uv/pip Windows console shims carry the interpreter in this script.
            with zipfile.ZipFile(launcher) as archive, archive.open("__main__.py") as entry:
                first_line = entry.readline(4096)
        except (zipfile.BadZipFile, KeyError):
            pass
    if first_line.startswith(b"#!"):
        interpreter = Path(first_line[2:].decode("utf-8").strip().strip('"'))
        if interpreter.is_absolute() and interpreter.is_file():
            prefixes.add(interpreter.parent.parent)
    roots = [
        root
        for prefix in sorted(prefixes)
        for root in (prefix / "Lib" / "site-packages", *prefix.glob("lib/python*/site-packages"))
    ]
    files = {launcher}
    for root in roots:
        modules = [root / "clio_kit"]
        for distribution in root.glob("clio_kit-*.dist-info"):
            files.add(distribution / "METADATA")
            direct_url = distribution / "direct_url.json"
            if direct_url.is_file():
                files.add(direct_url)
                with direct_url.open("rb") as stream:
                    try:
                        origin = json.loads(stream.read(65536))
                    except ValueError as exc:
                        raise OSError("invalid toolkit installation metadata") from exc
                if not isinstance(origin, dict) or not isinstance(origin.get("dir_info", {}), dict):
                    raise OSError("invalid toolkit installation metadata")
                if origin.get("dir_info", {}).get("editable"):
                    if not isinstance(origin.get("url"), str):
                        raise OSError("editable toolkit omitted its source URL")
                    try:
                        url = urlsplit(origin["url"])
                    except ValueError as exc:
                        raise OSError("invalid editable toolkit source URL") from exc
                    if url.scheme == "file" and not url.netloc:
                        modules.append(Path(url2pathname(url.path)) / "src" / "clio_kit")
        for module in modules:
            files.update(
                path
                for path in (module / "__init__.py", module / "runtime-catalog.json")
                if path.is_file()
            )
    digest = hashlib.sha256()
    for path in sorted(files):
        status = path.stat()
        digest.update(f"{path}:{status.st_size}:{status.st_mtime_ns}\0".encode())
        with path.open("rb") as stream:
            digest.update(stream.read(1024 * 1024))
    return digest.hexdigest()
