from __future__ import annotations

import io
import json
import logging
import re
import tarfile
from pathlib import Path
from typing import Any

import pytest
import zstandard

from clio_agent.runtime.document_stack import office, office_compat

#: Each host machine name, the lock's package set for it, its triplet and its loader.
HOSTS = {
    "x86_64": ("amd64", "x86_64-linux-gnu", "ld-linux-x86-64.so.2"),
    "aarch64": ("arm64", "aarch64-linux-gnu", "ld-linux-aarch64.so.1"),
}
#: Where each set is pinned: an immutable snapshot (amd64) or Launchpad's librarian (arm64).
SOURCES = {
    "amd64": r"https://snapshot\.ubuntu\.com/ubuntu/\d{8}T\d{6}Z/pool/.+_(amd64|all)\.deb",
    "arm64": r"https://launchpadlibrarian\.net/\d+/[^/]+_(arm64|all)\.deb",
}


def _lock() -> dict[str, Any]:
    return json.loads(office_compat.LOCK_FILE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("machine", ["x86_64", "amd64", "aarch64", "arm64"])
@pytest.mark.parametrize("glibc", ["2.28", "2.31", "2.35", "2.39"])
def test_office_always_runs_on_its_private_userland_on_linux(
    glibc: str, machine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever the host's glibc or libraries, Office loads none of them (no ABI mixing)."""
    monkeypatch.setattr(office_compat.platform, "system", lambda: "Linux")
    monkeypatch.setattr(office_compat.platform, "machine", lambda: machine)
    monkeypatch.setattr(office_compat.platform, "libc_ver", lambda: ("glibc", glibc))
    assert office_compat.supported() is True


def test_hosts_without_a_pinned_userland_report_a_typed_reason(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(office_compat.platform, "system", lambda: "Linux")
    monkeypatch.setattr(office_compat.platform, "machine", lambda: "riscv64")
    monkeypatch.setattr(office_compat, "_REPORTED", set())
    with caplog.at_level(logging.WARNING, logger=office_compat.__name__):
        assert office_compat.supported() is False
        assert office_compat.supported() is False
    reported = [r.getMessage() for r in caplog.records]
    assert len(reported) == 1
    assert f"reason={office_compat.UNSUPPORTED} machine=riscv64" in reported[0]
    with pytest.raises(office_compat.UnsupportedArchitecture) as raised:
        office_compat.packages()
    assert raised.value.reason == office_compat.UNSUPPORTED
    assert raised.value.machine == "riscv64"
    with pytest.raises(office_compat.UnsupportedArchitecture):
        office_compat.prepare(
            Path("unused"), download=lambda *args: None, deb_data=lambda stream: b""
        )
    # Other systems run TDF's self-contained builds; no Linux userland applies there.
    monkeypatch.setattr(office_compat.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(office_compat.platform, "machine", lambda: "x86_64")
    assert office_compat.supported() is False


def test_an_unreadable_lock_is_a_typed_reason(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(office_compat.platform, "system", lambda: "Linux")
    monkeypatch.setattr(office_compat.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(office_compat, "_REPORTED", set())
    monkeypatch.setattr(office_compat, "_lock", lambda: {"snapshot": "x"})
    with caplog.at_level(logging.WARNING, logger=office_compat.__name__):
        assert office_compat.supported() is False
    assert f"reason={office_compat.LOCK_UNREADABLE}" in caplog.records[0].getMessage()


def test_a_pinned_machine_missing_from_the_lock_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = _lock()
    del lock["architectures"]["arm64"]
    monkeypatch.setattr(office_compat, "_lock", lambda: lock)
    monkeypatch.setattr(office_compat.platform, "machine", lambda: "aarch64")
    with pytest.raises(office_compat.UnsupportedArchitecture, match="machine=aarch64"):
        office_compat.packages()


def test_the_lock_holds_one_verified_package_set_per_architecture() -> None:
    lock = _lock()
    assert sorted(lock["architectures"]) == sorted({key for key, _, _ in HOSTS.values()})
    for key, triplet, _ in HOSTS.values():
        entry = lock["architectures"][key]
        assert entry["triplet"] == triplet
        names = [item["package"] for item in entry["packages"]]
        assert names == sorted(set(names))
        for item in entry["packages"]:
            assert set(item) == {"package", "version", "path", "sha256", "size"}
            assert re.fullmatch(SOURCES[key], f"{entry['base']}/{item['path']}")
            assert Path(item["path"]).name.startswith(f"{item['package']}_")
            assert re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) and item["size"] > 0


def test_arm64_pins_the_amd64_versions() -> None:
    """Both sets come from one point in jammy: every shared package has one version."""
    architectures = _lock()["architectures"]
    amd64 = {item["package"]: item["version"] for item in architectures["amd64"]["packages"]}
    arm64 = {item["package"]: item["version"] for item in architectures["arm64"]["packages"]}
    assert {name: (amd64[name], arm64[name]) for name in amd64.keys() & arm64.keys()} == {
        name: (amd64[name], amd64[name]) for name in amd64.keys() & arm64.keys()
    }


@pytest.mark.parametrize("machine", sorted(HOSTS))
def test_the_lock_pins_the_c_runtime_and_the_desktop_libraries(
    machine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(office_compat.platform, "machine", lambda: machine)
    key = HOSTS[machine][0]
    pinned = office_compat.packages()
    names = {url.rsplit("/", 1)[1].split("_", 1)[0] for url, _ in pinned}
    # The C runtime and the libraries a bare host lacks (found on a minimal Ubuntu).
    assert {"libc6", "libstdc++6", "libx11-6", "libxinerama1", "libcairo2", "libcups2"} <= names
    assert {"libfontconfig1", "libnss3", "libglib2.0-0"} <= names
    for url, checksum in pinned:
        assert re.fullmatch(SOURCES[key], url)
        assert len(checksum) == 64


def test_each_host_gets_its_own_userland_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    directories = set()
    for machine in HOSTS:
        monkeypatch.setattr(office_compat.platform, "machine", lambda machine=machine: machine)
        directories.add(office_compat.runtime_directory())
    assert len(directories) == len(HOSTS)


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


@pytest.mark.parametrize("machine", sorted(HOSTS))
def test_wrapper_published_last_and_remains_relocatable(
    machine: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(office_compat.platform, "machine", lambda: machine)
    key, triplet, loader = HOSTS[machine]
    root = tmp_path / "office with spaces"
    root.mkdir()
    (root / "compat-0123456789ab").mkdir()  # the previous, C-runtime-only directory
    calls: list[str] = []
    payload = _tar([(f"lib/{triplet}/{loader}", b"loader")])

    def download(url: str, target: Path, checksum: str) -> None:
        assert not (root / office_compat.WRAPPER).exists()
        assert len(checksum) == 64 and re.fullmatch(SOURCES[key], url)
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
    assert f'"$userland/lib/{triplet}/{loader}"' in text
    assert f'--library-path "$userland/lib/{triplet}:$userland/usr/lib/{triplet}:' in text
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
