"""The pinned ripgrep: verified extraction and how the fs tools find a binary."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from clio_agent.runtime import ripgrep


def _archive(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as package:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            package.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_only_the_one_rg_executable_is_read_from_the_archive() -> None:
    archive = _archive({"ripgrep-14.1.1-x/rg": b"binary", "ripgrep-14.1.1-x/doc/rg.1": b"man"})
    assert ripgrep._binary(archive, "x86_64-unknown-linux-musl.tar.gz") == b"binary"
    with pytest.raises(ValueError, match="no unique"):
        ripgrep._binary(_archive({"a/rg": b"1", "b/rg": b"2"}), "x.tar.gz")


def test_every_pinned_asset_has_a_sha256() -> None:
    assert all(len(digest) == 64 for _, digest in ripgrep.ASSETS.values())


def test_the_tools_use_a_verified_install_then_the_host_then_a_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ripgrep, "installed_ripgrep", lambda: tmp_path / "managed" / "rg")
    assert ripgrep.ripgrep_executable() == tmp_path / "managed" / "rg"
    monkeypatch.setattr(ripgrep, "installed_ripgrep", lambda: None)
    monkeypatch.setattr(ripgrep.shutil, "which", lambda name: "/opt/host/rg")
    assert ripgrep.ripgrep_executable() == Path("/opt/host/rg")
    monkeypatch.setattr(ripgrep.shutil, "which", lambda name: None)

    def offline() -> Path:
        raise ripgrep.httpx.ConnectError("offline")

    monkeypatch.setattr(ripgrep, "ensure_ripgrep", offline)
    with pytest.raises(ripgrep.RipgrepMissing, match="ripgrep_missing"):
        ripgrep.ripgrep_executable()
