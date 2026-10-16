from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Any

import pytest
import zstandard

from clio_agent.runtime.document_stack import office, office_compat


@pytest.mark.parametrize(
    "glibc,expected", [("2.28", True), ("2.31", True), ("2.34", False), ("2.39", False)]
)
def test_compatibility_loader_only_for_older_glibc(
    glibc: str, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(office_compat.platform, "system", lambda: "Linux")
    monkeypatch.setattr(office_compat.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(office_compat.platform, "libc_ver", lambda: ("glibc", glibc))
    assert office_compat.supported() is expected


def _tar(members: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, data in members:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


def test_zstd_packages_are_extracted_with_notices(tmp_path: Path) -> None:
    payload = _tar(
        [
            ("lib/x86_64-linux-gnu/libc.so.6", b"library"),
            ("usr/share/doc/libc6/copyright", b"license"),
        ]
    )
    office_compat._extract(zstandard.ZstdCompressor().compress(payload), tmp_path)
    assert (tmp_path / "lib/x86_64-linux-gnu/libc.so.6").read_bytes() == b"library"
    assert (tmp_path / "usr/share/doc/libc6/copyright").read_bytes() == b"license"


def test_compatibility_payload_cannot_escape_root(tmp_path: Path) -> None:
    with pytest.raises(tarfile.TarError):
        office_compat._extract(_tar([("../outside", b"bad")]), tmp_path / "inside")
    assert not (tmp_path / "outside").exists()


def test_wrapper_published_last_and_remains_relocatable(tmp_path: Path) -> None:
    root = tmp_path / "office with spaces"
    root.mkdir()
    calls: list[str] = []
    payload = _tar([(str(office_compat.LOADER).replace("\\", "/"), b"loader")])

    def download(url: str, target: Path, checksum: str) -> None:
        assert not (root / office_compat.WRAPPER).exists()
        assert len(checksum) == 64 and url.startswith("https://launchpadlibrarian.net/")
        calls.append(url)
        target.write_bytes(payload)

    office_compat.prepare(root, download=download, deb_data=lambda stream: stream.read())
    wrapper = root / office_compat.WRAPPER
    assert len(calls) == len(office_compat.PACKAGES)
    assert str(root) not in wrapper.read_text()
    assert "--library-path" in wrapper.read_text()
    assert "LD_LIBRARY_PATH=" not in wrapper.read_text()
    assert "$@" in wrapper.read_text()


def test_old_managed_image_gets_loader_before_version_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = tmp_path / "opt/libreoffice26.2/program/soffice"
    program.parent.mkdir(parents=True)
    program.touch()
    monkeypatch.setattr(office_compat, "supported", lambda: True)
    order: list[str] = []

    def prepare(root: Path, **kwargs: Any) -> None:
        order.append("compatibility")
        (root / office_compat.WRAPPER).touch()

    def probe(command: list[str], **kwargs: Any) -> str:
        assert Path(command[0]).name == office_compat.WRAPPER
        order.append("version")
        return "LibreOffice 26.2.6"

    monkeypatch.setattr(office_compat, "prepare", prepare)
    monkeypatch.setattr(office, "run", probe)
    assert Path(office.ensure_office(tmp_path)).name == office_compat.WRAPPER
    assert order == ["compatibility", "version"]
