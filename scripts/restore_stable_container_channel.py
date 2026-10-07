"""Restore GHCR latest pointers from existing stable manifests without rebuilding."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

IMAGES = ("clio-api", "clio-web", "clio-tui")
MANIFEST_TYPES = (
    "application/vnd.oci.image.manifest.v1+json,"
    "application/vnd.docker.distribution.manifest.v2+json,"
    "application/vnd.oci.image.index.v1+json,"
    "application/vnd.docker.distribution.manifest.list.v2+json"
)
STABLE_VERSION = re.compile(r"\d+\.\d+\.\d+(?:\.\d+)?")
IMAGE_VERSION = re.compile(r"\d+\.\d+\.\d+(?:\.\d+)?(?:-(?:beta|rc)\.\d+)?")


@dataclass(frozen=True)
class Manifest:
    """Exact registry bytes and their content identity."""

    body: bytes
    digest: str
    media_type: str


class Registry:
    """Access only CLIO image manifests and the public stable-release metadata."""

    def __init__(self, actor: str, token: str) -> None:
        self.actor = actor
        self.token = token
        self.registry_tokens: dict[str, str] = {}

    def _request(
        self, url: str, headers: dict[str, str], data: bytes | None = None
    ) -> tuple[bytes, dict[str, str]]:
        request = Request(url, data=data, headers=headers, method="GET" if data is None else "PUT")
        try:
            with urlopen(request, timeout=30) as response:
                return response.read(), {
                    key.lower(): value for key, value in response.headers.items()
                }
        except HTTPError as exc:
            raise RuntimeError(f"{request.method} {url} returned HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"{request.method} {url} failed: {exc.reason}") from exc

    def stable_release(self) -> dict[str, Any]:
        """Read the repository's currently published stable release."""

        body, _ = self._request(
            "https://api.github.com/repos/iowarp/clio-agent/releases/latest",
            {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json"},
        )
        return json.loads(body)

    def _headers(self, image: str) -> dict[str, str]:
        if image not in self.registry_tokens:
            credentials = base64.b64encode(f"{self.actor}:{self.token}".encode()).decode()
            query = urlencode(
                {"service": "ghcr.io", "scope": f"repository:iowarp/{image}:pull,push"}
            )
            body, _ = self._request(
                f"https://ghcr.io/token?{query}", {"Authorization": f"Basic {credentials}"}
            )
            access = json.loads(body)
            self.registry_tokens[image] = access.get("token") or access["access_token"]
        return {"Authorization": f"Bearer {self.registry_tokens[image]}", "Accept": MANIFEST_TYPES}

    def manifest(self, image: str, tag: str) -> Manifest:
        """Read and verify the exact bytes of an existing manifest."""

        body, headers = self._request(
            f"https://ghcr.io/v2/iowarp/{image}/manifests/{tag}", self._headers(image)
        )
        digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
        if headers.get("docker-content-digest") != digest:
            raise RuntimeError(f"Registry content digest mismatch for {image}:{tag}")
        return Manifest(body, digest, json.loads(body)["mediaType"])

    def set_latest(self, image: str, manifest: Manifest) -> None:
        """Copy an existing manifest to latest; no version tag is written."""

        headers = self._headers(image) | {"Content-Type": manifest.media_type}
        _, response_headers = self._request(
            f"https://ghcr.io/v2/iowarp/{image}/manifests/latest", headers, manifest.body
        )
        if response_headers.get("docker-content-digest") != manifest.digest:
            raise RuntimeError(f"Registry did not acknowledge the stable digest for {image}:latest")


def restore_latest(
    registry: Registry, stable_version: str, expected_latest: str
) -> list[dict[str, str]]:
    """Validate every image first, then restore only the authorized latest pointers."""

    if not STABLE_VERSION.fullmatch(stable_version):
        raise ValueError("Restoration requires a stable version, not a prerelease")
    if not IMAGE_VERSION.fullmatch(expected_latest):
        raise ValueError("Expected latest must name an existing CLIO version")
    release = registry.stable_release()
    if release.get("draft") or release.get("prerelease") or not release.get("published_at"):
        raise RuntimeError("GitHub has no published stable release")
    if release.get("tag_name") != f"v{stable_version}":
        raise RuntimeError("Requested version does not equal GitHub's current stable release")
    prepared: dict[str, tuple[Manifest, Manifest]] = {}
    for image in IMAGES:
        stable = registry.manifest(image, stable_version)
        expected = registry.manifest(image, expected_latest)
        current = registry.manifest(image, "latest")
        if current.digest not in (stable.digest, expected.digest):
            raise RuntimeError(
                f"{image}:latest changed to an unrelated image; refusing restoration"
            )
        prepared[image] = stable, expected
    results: list[dict[str, str]] = []
    for image, (stable, expected) in prepared.items():
        current = registry.manifest(image, "latest")
        if current.digest not in (stable.digest, expected.digest):
            raise RuntimeError(f"{image}:latest changed after preparation; refusing restoration")
        if current.digest != stable.digest:
            registry.set_latest(image, stable)
        if registry.manifest(image, "latest").digest != stable.digest:
            raise RuntimeError(f"Stable latest verification failed for {image}")
        if registry.manifest(image, stable_version).digest != stable.digest:
            raise RuntimeError(f"Stable version tag changed for {image}")
        if registry.manifest(image, expected_latest).digest != expected.digest:
            raise RuntimeError(f"Expected version tag changed for {image}")
        results.append({"image": image, "stable_version": stable_version, "digest": stable.digest})
    return results


def main() -> None:
    """Run an explicit GitHub-token-backed repair of the three stable channels."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--expected-latest", required=True)
    args = parser.parse_args()
    registry = Registry(os.environ["GITHUB_ACTOR"], os.environ["GITHUB_TOKEN"])
    print(json.dumps(restore_latest(registry, args.version, args.expected_latest), indent=2))


if __name__ == "__main__":
    main()
