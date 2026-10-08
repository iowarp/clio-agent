"""CLIO's SearXNG lifecycle hook, shipped beside the native supervisor (stdlib only).

``install`` (the supervisor's post-install step, run by the service environment's
Python) fetches SearXNG's source at the pinned commit into the service environment,
refuses an archive that is not that commit, and writes this installation's secret
key to a private 0600 file. SearXNG's source is installed unmodified.

``running`` (the supervisor's identity check, run by the controller's Python) proves
the answering server is this installation: ``/config`` must report the instance name
derived from this installation's secret, and ``/search?format=json`` must answer
JSON once per launch.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import sys
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SOURCE = Path("environment") / "searxng-source"
SECRET = "secret_key"
#: The identity marker's message: the instance name is an HMAC of it under the secret.
IDENTITY_MESSAGE = b"clio-searxng-identity"


def instance_name(secret: str) -> str:
    """The instance name only the holder of this installation's secret can configure."""

    marker = hmac.new(secret.encode(), IDENTITY_MESSAGE, hashlib.sha256).hexdigest()[:16]
    return f"CLIO SearXNG {marker}"


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` readable by this user only (0600), atomically."""

    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.replace(temporary, path)
    if os.name != "nt":
        path.chmod(0o600)


def read_secret(root: Path) -> str:
    """This installation's secret key; installing writes it."""

    path = root / SECRET
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("The SearXNG secret key is missing; reinstall SearXNG")
    secret = path.read_text(encoding="utf-8").strip()
    if len(secret) < 32:
        raise RuntimeError("The SearXNG secret key is invalid; reinstall SearXNG")
    return secret


def _members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """The archive's entries below its single top directory, renamed to drop it."""

    selected = []
    for member in archive.getmembers():
        parts = member.name.split("/", 1)
        if len(parts) != 2 or not parts[1]:
            continue
        member.name = parts[1]
        selected.append(member)
    return selected


def install_source(root: Path, manifest: dict[str, Any]) -> dict[str, str]:
    """Fetch and unpack the pinned commit's source; reuse it when already in place."""

    pinned = manifest["searxng"]
    commit = pinned["commit"]
    target = root / SOURCE
    marker = target / ".clio-source.json"
    if marker.is_file() and json.loads(marker.read_text()).get("commit") == commit:
        print(f"clio: SearXNG source {commit[:12]} is already installed")
        return json.loads(marker.read_text())
    download = root / "tmp" / "searxng-source.tar.gz"
    download.parent.mkdir(exist_ok=True)
    print(f"clio: downloading SearXNG {commit[:12]} from {pinned['source_url']}", flush=True)
    with urllib.request.urlopen(pinned["source_url"], timeout=120) as response:  # noqa: S310
        with download.open("wb") as stream:
            shutil.copyfileobj(response, stream)
    digest = hashlib.sha256(download.read_bytes()).hexdigest()
    staging = root / "tmp" / "searxng-source"
    shutil.rmtree(staging, ignore_errors=True)
    with tarfile.open(download, "r:gz") as archive:
        # git archive records the commit it was made from in the global pax header.
        if archive.pax_headers.get("comment") != commit:
            raise RuntimeError("The downloaded SearXNG archive is not the pinned commit")
        archive.extractall(staging, members=_members(archive), filter="data")
    if not (staging / "searx" / "webapp.py").is_file():
        raise RuntimeError("The downloaded SearXNG archive has no searx/webapp.py")
    record = {"commit": commit, "archive_sha256": digest}
    (staging / ".clio-source.json").write_text(json.dumps(record), encoding="utf-8")
    shutil.rmtree(target, ignore_errors=True)
    staging.replace(target)
    download.unlink(missing_ok=True)
    print(f"clio: SearXNG source installed (archive sha256 {digest})")
    return record


def install(root: Path) -> None:
    """The post-install step: pinned source plus a fresh per-installation secret."""

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    install_source(root, manifest)
    write_private(root / SECRET, secrets.token_hex(32))
    print("clio: wrote this installation's private SearXNG secret key (0600)")


def _get(url: str, timeout: float) -> tuple[int, Any]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            return response.status, json.load(response)
    except (OSError, ValueError):
        return 0, None


def running(root: Path) -> bool:
    """Whether the server on the owned port is this installation (see module docstring)."""

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    base = f"http://127.0.0.1:{int(manifest['port'])}"
    try:
        expected = instance_name(read_secret(root))
    except (OSError, RuntimeError):
        return False
    status, config = _get(base + "/config", 10)
    if status != 200 or not isinstance(config, dict) or config.get("instance_name") != expected:
        return False
    receipt_path = root / "receipt.json"
    receipt = json.loads(receipt_path.read_text()) if receipt_path.is_file() else {}
    launch = {"generation": receipt.get("generation"), "pid": receipt.get("pid")}
    proven = root / "evidence" / "json-api.json"
    if proven.is_file() and json.loads(proven.read_text()) == launch:
        return True
    query = urllib.parse.urlencode({"q": "searxng", "format": "json"})
    status, body = _get(f"{base}/search?{query}", 45)
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("results"), list):
        return False
    proven.parent.mkdir(exist_ok=True)
    proven.write_text(json.dumps(launch), encoding="utf-8")
    return True


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    verb = sys.argv[1] if len(sys.argv) > 1 else ""
    if verb == "install":
        install(here)
    elif verb == "running":
        sys.exit(0 if running(here) else 1)
    else:
        sys.exit(f"unsupported SearXNG hook action {verb!r}")
