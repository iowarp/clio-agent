from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Any

import pytest
import zstandard

from clio_agent.runtime.document_stack import office, office_compat


@pytest.mark.parametrize("glibc", ["2.28", "2.31", "2.35", "2.39"])
def test_office_always_runs_on_its_private_userland_on_linux(
    glibc: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever the host's glibc or libraries, Office loads none of them (no ABI mixing)."""
    monkeypatch.setattr(office_compat.platform, "system", lambda: "Linux")
    monkeypatch.setattr(office_compat.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(office_compat.platform, "libc_ver", lambda: ("glibc", glibc))
    assert office_compat.supported() is True


def test_hosts_without_a_pinned_userland_are_not_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(office_compat.platform, "system", lambda: "Linux")
    monkeypatch.setattr(office_compat.platform, "machine", lambda: "riscv64")
    assert office_compat.supported() is False
    monkeypatch.setattr(office_compat.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(office_compat.platform, "machine", lambda: "x86_64")
    assert office_compat.supported() is False


def test_the_lock_pins_the_c_runtime_and_the_desktop_libraries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(office_compat.platform, "machine", lambda: "x86_64")
    pinned = office_compat.packages()
    names = {url.rsplit("/", 1)[1].split("_", 1)[0] for url, _ in pinned}
    # The C runtime and the libraries a bare host lacks (found on a minimal Ubuntu).
    assert {"libc6", "libstdc++6", "libx11-6", "libxinerama1", "libcairo2", "libcups2"} <= names
    assert {"libfontconfig1", "libnss3", "libglib2.0-0"} <= names
    for url, checksum in pinned:
        assert url.startswith("https://snapshot.ubuntu.com/ubuntu/") and url.endswith(".deb")
        assert len(checksum) == 64


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


def test_wrapper_published_last_and_remains_relocatable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(office_compat.platform, "machine", lambda: "x86_64")
    root = tmp_path / "office with spaces"
    root.mkdir()
    (root / "compat-0123456789ab").mkdir()  # the previous, C-runtime-only directory
    calls: list[str] = []
    payload = _tar([("lib/x86_64-linux-gnu/ld-linux-x86-64.so.2", b"loader")])

    def download(url: str, target: Path, checksum: str) -> None:
        assert not (root / office_compat.WRAPPER).exists()
        assert len(checksum) == 64 and url.startswith("https://snapshot.ubuntu.com/")
        calls.append(url)
        target.write_bytes(payload)

    office_compat.prepare(root, download=download, deb_data=lambda stream: stream.read())
    wrapper = root / office_compat.WRAPPER
    text = wrapper.read_text()
    assert sorted(calls) == sorted(url for url, _ in office_compat.packages())
    assert str(root) not in text
    assert "--library-path" in text and "LD_LIBRARY_PATH=" not in text
    assert "SAL_USE_VCLPLUGIN=svp" in text and "FONTCONFIG_FILE=" in text
    assert "$@" in text
    userland = root / office_compat.runtime_directory()
    assert (
        'prefix="relative">../opt/libreoffice26.2/share/fonts'
        in (userland / "fonts.conf").read_text()
    )
    assert not (root / "compat-0123456789ab").exists()


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
