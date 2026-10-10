"""Target-side: how many bytes a pinned image pull still has to download.

Run on the target as ``python3 -c <this file> IMAGE CACHE_DIR`` before an
Apptainer pull. It reads the image's registry manifest (anonymous token flow,
the host's own proxy settings), picks this host's platform from an index, and
compares the layer digests with the blobs already in the layer cache. It
prints one structured line the operation turns into a determinate byte total::

    CLIO_PROGRESS {"unit": "bytes", "current": 0, "total": <bytes still needed>, ...}

Any failure (no network, a private registry, an unknown layout) prints
nothing: the pull's progress then stays honestly indeterminate. Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
ARCHITECTURES = {"x86_64": "amd64", "aarch64": "arm64", "ppc64le": "ppc64le"}


def split(image: str) -> tuple[str, str, str]:
    """(registry host, repository, reference) of a docker-style image reference."""

    name, _, digest = image.removeprefix("docker://").partition("@")
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, repository = first, rest
    else:
        registry, repository = "docker.io", name
    tag = ""
    if ":" in repository.rsplit("/", 1)[-1]:
        repository, tag = repository.rsplit(":", 1)
    if registry == "docker.io":
        registry = "registry-1.docker.io"
        if "/" not in repository:
            repository = "library/" + repository
    return registry, repository, digest or tag or "latest"


def fetch(url: str, token: str = "") -> tuple[dict, str]:
    """GET a manifest, answering one anonymous Bearer challenge."""

    headers = {"Accept": ACCEPT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(url, headers=headers), timeout=15) as response:
            return json.loads(response.read()), token
    except HTTPError as error:
        challenge = error.headers.get("WWW-Authenticate", "") if error.code == 401 else ""
        if token or not challenge.lower().startswith("bearer "):
            raise
    fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = fields.pop("realm", "")
    if not realm.startswith("https://"):
        raise ValueError("unsupported registry challenge")
    with urlopen(f"{realm}?{urlencode(fields)}", timeout=15) as response:
        answer = json.loads(response.read())
    granted = answer.get("token") or answer.get("access_token") or ""
    if not granted:
        raise ValueError("registry granted no token")
    return fetch(url, granted)


def cached_digests(cache: str) -> set[str]:
    """Hex digests of the blobs already in an Apptainer layer cache."""

    found: set[str] = set()
    for directory, folders, files in os.walk(cache):
        if os.path.basename(directory) == "sha256" and "blobs" in directory:
            found.update(name for name in files if re.fullmatch(r"[0-9a-f]{64}", name))
        folders[:] = [name for name in folders if name not in {"rootfs", "oci-tmp"}]
    return found


def plan(image: str, cache: str) -> dict:
    """Bytes and layers still to download for ``image`` on this host."""

    registry, repository, reference = split(image)
    base = f"https://{registry}/v2/{repository}/manifests/"
    manifest, token = fetch(base + reference)
    if "manifests" in manifest:
        architecture = ARCHITECTURES.get(os.uname().machine, os.uname().machine)
        chosen = next(
            (
                row
                for row in manifest["manifests"]
                if (row.get("platform") or {}).get("os") == "linux"
                and (row.get("platform") or {}).get("architecture") == architecture
            ),
            None,
        )
        if chosen is None:
            raise ValueError("no manifest for this platform")
        manifest, _ = fetch(base + chosen["digest"], token)
    blobs = [*manifest.get("layers", []), manifest.get("config") or {}]
    blobs = [row for row in blobs if row.get("digest") and isinstance(row.get("size"), int)]
    have = cached_digests(cache)
    missing = [row for row in blobs if row["digest"].split(":", 1)[-1] not in have]
    return {
        "unit": "bytes",
        "current": 0,
        "total": sum(row["size"] for row in missing),
        "layers": len(manifest.get("layers", [])),
        "cached_layers": len(blobs) - len(missing),
        "cached_bytes": sum(row["size"] for row in blobs if row not in missing),
        "source": "registry_manifest",
    }


def main() -> None:
    """Print the plan line, or nothing at all when the registry cannot be read."""

    try:
        result = plan(sys.argv[1], sys.argv[2])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # Unknown (offline, private registry, odd layout): never a failed pull.
        return
    print("CLIO_PROGRESS " + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
