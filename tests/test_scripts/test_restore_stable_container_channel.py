"""Stable-channel recovery preserves immutable published image identities."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from scripts.restore_stable_container_channel import IMAGES, Manifest, Registry, restore_latest


def _manifest(name: str) -> Manifest:
    body = (
        '{"mediaType":"application/vnd.oci.image.manifest.v1+json","name":"' + name + '"}'
    ).encode()
    return Manifest(
        body,
        f"sha256:{hashlib.sha256(body).hexdigest()}",
        "application/vnd.oci.image.manifest.v1+json",
    )


class FakeRegistry(Registry):
    """In-memory registry used only by the recovery tests."""

    def __init__(self) -> None:
        super().__init__("test", "test-token")
        self.release: dict[str, Any] = {
            "tag_name": "v0.9.4.24",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-10-01T00:00:00Z",
        }
        self.entries: dict[tuple[str, str], Manifest] = {}
        self.writes: list[tuple[str, Manifest]] = []
        for image in IMAGES:
            self.entries[image, "0.9.4.24"] = _manifest(f"{image}-stable")
            self.entries[image, "0.9.5-beta.5"] = _manifest(f"{image}-beta")
            self.entries[image, "latest"] = self.entries[image, "0.9.5-beta.5"]

    def stable_release(self) -> dict[str, Any]:
        return self.release

    def manifest(self, image: str, tag: str) -> Manifest:
        return self.entries[image, tag]

    def set_latest(self, image: str, manifest: Manifest) -> None:
        self.writes.append((image, manifest))
        self.entries[image, "latest"] = manifest


def test_restore_preserves_every_version_tag_and_exact_manifest_bytes() -> None:
    registry = FakeRegistry()
    originals = {key: value for key, value in registry.entries.items() if key[1] != "latest"}
    results = restore_latest(registry, "0.9.4.24", "0.9.5-beta.5")
    assert len(results) == 3
    assert len(registry.writes) == 3
    for image in IMAGES:
        assert registry.entries[image, "latest"] is originals[image, "0.9.4.24"]
    assert all(registry.entries[key] is value for key, value in originals.items())


def test_prerelease_cannot_be_selected_as_the_stable_channel() -> None:
    registry = FakeRegistry()
    with pytest.raises(ValueError, match="stable version"):
        restore_latest(registry, "0.9.5-beta.5", "0.9.5-beta.5")
    assert registry.writes == []


def test_requested_version_must_equal_the_published_github_stable() -> None:
    registry = FakeRegistry()
    with pytest.raises(RuntimeError, match="current stable release"):
        restore_latest(registry, "0.9.4.23", "0.9.5-beta.5")
    assert registry.writes == []


def test_all_images_are_validated_before_any_pointer_is_written() -> None:
    registry = FakeRegistry()
    registry.entries["clio-tui", "latest"] = _manifest("unrelated-concurrent-release")
    with pytest.raises(RuntimeError, match="unrelated image"):
        restore_latest(registry, "0.9.4.24", "0.9.5-beta.5")
    assert registry.writes == []


def test_completed_or_partial_recovery_is_idempotent() -> None:
    registry = FakeRegistry()
    registry.entries["clio-api", "latest"] = registry.entries["clio-api", "0.9.4.24"]
    restore_latest(registry, "0.9.4.24", "0.9.5-beta.5")
    assert [image for image, _ in registry.writes] == ["clio-web", "clio-tui"]
    registry.writes.clear()
    restore_latest(registry, "0.9.4.24", "0.9.5-beta.5")
    assert registry.writes == []


def test_registry_rejects_a_content_digest_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = Registry("test", "test-token")
    manifest = _manifest("stable")
    monkeypatch.setattr(registry, "_headers", lambda image: {})
    monkeypatch.setattr(
        registry,
        "_request",
        lambda url, headers: (manifest.body, {"docker-content-digest": "wrong"}),
    )
    with pytest.raises(RuntimeError, match="content digest mismatch"):
        registry.manifest("clio-api", "0.9.4.24")


def test_registry_writes_only_latest_with_the_existing_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = Registry("test", "test-token")
    manifest = _manifest("stable")
    calls: list[tuple[str, dict[str, str], bytes | None]] = []

    def request(
        url: str, headers: dict[str, str], data: bytes | None = None
    ) -> tuple[bytes, dict[str, str]]:
        calls.append((url, headers, data))
        return b"", {"docker-content-digest": manifest.digest}

    monkeypatch.setattr(registry, "_headers", lambda image: {})
    monkeypatch.setattr(registry, "_request", request)
    registry.set_latest("clio-api", manifest)
    assert calls == [
        (
            "https://ghcr.io/v2/iowarp/clio-api/manifests/latest",
            {"Content-Type": manifest.media_type},
            manifest.body,
        )
    ]


def test_pointer_change_after_preparation_is_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = FakeRegistry()
    original_read = registry.manifest
    latest_reads = 0

    def read(image: str, tag: str) -> Manifest:
        nonlocal latest_reads
        if image == "clio-api" and tag == "latest":
            latest_reads += 1
            if latest_reads == 2:
                return _manifest("concurrent-release")
        return original_read(image, tag)

    monkeypatch.setattr(registry, "manifest", read)
    with pytest.raises(RuntimeError, match="changed after preparation"):
        restore_latest(registry, "0.9.4.24", "0.9.5-beta.5")
    assert registry.writes == []
