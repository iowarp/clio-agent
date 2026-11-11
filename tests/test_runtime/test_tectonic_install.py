"""The pinned Tectonic: verified extraction, the receipt, and how a build finds the binary."""

from __future__ import annotations

import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from clio_agent.runtime import tectonic


def _tar(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as package:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            package.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        for name, data in members.items():
            package.writestr(name, data)
    return buffer.getvalue()


def test_only_the_one_executable_at_the_archive_root_is_read() -> None:
    archive = _tar({"tectonic": b"binary"})
    assert tectonic._binary(archive, "x86_64-unknown-linux-musl.tar.gz") == b"binary"
    assert tectonic._binary(_zip({"tectonic.exe": b"exe"}), "x.zip") == b"exe"
    # A nested path or a second copy is not the release's layout.
    with pytest.raises(ValueError, match="no unique"):
        tectonic._binary(_tar({"x/tectonic": b"1"}), "x.tar.gz")
    with pytest.raises(ValueError, match="no unique"):
        tectonic._binary(_zip({"bin/tectonic.exe": b"1"}), "x.zip")


def test_every_pinned_asset_has_a_sha256_and_the_bundle_is_versioned() -> None:
    assert all(len(digest) == 64 for _, digest in tectonic.ASSETS.values())
    assert tectonic.BUNDLE_URL.endswith("_v33.tar")
    assert {"relay.fullyjustified.net", "data1b.fullyjustified.net"} == set(tectonic.BUNDLE_HOSTS)


def test_a_verified_install_is_reused_and_a_changed_binary_is_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, _, executable = tectonic._installation(tmp_path)
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"binary")
    receipt = executable.parent / "executable.sha256"
    receipt.write_text(hashlib.sha256(b"binary").hexdigest() + "\n")
    assert tectonic.installed_tectonic(cache_root=tmp_path) == executable
    executable.write_bytes(b"tampered")
    assert tectonic.installed_tectonic(cache_root=tmp_path) is None


def test_a_download_with_the_wrong_digest_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self) -> list[bytes]:
            return [_tar({"tectonic": b"not the release"})]

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def stream(self, method: str, url: str) -> _Response:
            assert method == "GET" and f"tectonic%40{tectonic.VERSION}/" in url
            return _Response()

    monkeypatch.setattr(tectonic.httpx, "Client", _Client)
    with pytest.raises(ValueError, match="pinned SHA-256"):
        tectonic.ensure_tectonic(cache_root=tmp_path)
    _, _, executable = tectonic._installation(tmp_path)
    assert not executable.exists()


def test_a_build_uses_the_override_then_the_install_then_a_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "override-tectonic"
    override.write_bytes(b"x")
    monkeypatch.setenv("CLIO_DOCUMENT_TECTONIC", str(override))
    assert tectonic.tectonic_executable() == override.resolve()
    monkeypatch.delenv("CLIO_DOCUMENT_TECTONIC")
    managed = tmp_path / "managed" / "tectonic"
    monkeypatch.setattr(tectonic, "installed_tectonic", lambda **_: managed)
    assert tectonic.tectonic_executable() == managed
    monkeypatch.setattr(tectonic, "installed_tectonic", lambda **_: None)

    def offline(**_: object) -> Path:
        raise tectonic.httpx.ConnectError("offline")

    monkeypatch.setattr(tectonic, "ensure_tectonic", offline)
    with pytest.raises(tectonic.TectonicMissing, match="could not be installed") as caught:
        tectonic.tectonic_executable()
    assert caught.value.reason == "tectonic_missing"
