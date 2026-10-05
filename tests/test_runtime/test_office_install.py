"""Renderer provisioning rejects tampering and uses private extraction, not system installs."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Any

import pytest

from clio_agent.runtime.document_stack import office
from clio_agent.runtime.document_stack.process import DocumentError


@pytest.mark.parametrize("system,machine", office.ASSETS)
def test_official_assets_are_pinned(
    system: str, machine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(office.platform, "system", lambda: system)
    monkeypatch.setattr(office.platform, "machine", lambda: machine)
    url, filename, checksum = office.asset()
    assert url.startswith("https://download.documentfoundation.org/")
    assert filename in url
    assert len(checksum) == 64


def test_bad_download_checksum_never_extracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(office.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"corrupt"))
    with pytest.raises(DocumentError, match="pinned SHA-256"):
        office._download("https://example.invalid/official", tmp_path / "archive", "0" * 64)


def test_windows_renderer_is_extracted_to_private_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(office.platform, "system", lambda: "Windows")
    seen: list[str] = []

    def run(command: list[str], **kwargs: Any) -> str:
        seen.extend(command)
        return ""

    monkeypatch.setattr(office, "run", run)
    office._extract(tmp_path / "renderer.msi", tmp_path / "private")
    assert seen[1:4] == ["/a", str(tmp_path / "renderer.msi"), "/qn"]
    assert seen[-1] == f"TARGETDIR={tmp_path / 'private'}"
    assert "/i" not in seen


def test_private_renderer_is_probed_before_warm_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = tmp_path / "program" / "soffice.com"
    program.parent.mkdir()
    program.touch()
    seen: list[list[str]] = []
    monkeypatch.setattr(office, "run", lambda command, **kwargs: seen.append(command) or "26.2.6")
    assert office.ensure_office(tmp_path) == str(program)
    assert seen == [[str(program), "--version"]]


def test_debian_archive_parser_reads_real_data_member() -> None:
    payload = b"real tar payload"
    header = (
        b"data.tar.xz/".ljust(16)
        + b"0".ljust(12)
        + b"0".ljust(6) * 2
        + b"100644".ljust(8)
        + str(len(payload)).encode().ljust(10)
        + b"`\n"
    )
    assert office._deb_data(io.BytesIO(b"!<arch>\n" + header + payload)) == payload
    with pytest.raises(DocumentError, match="Invalid"):
        office._deb_data(io.BytesIO(b"bad header"))


def test_linux_archive_cannot_escape_private_root(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo("../outside")
        member.size = 1
        output.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(tarfile.TarError):
        office._extract_linux(archive, tmp_path / "private")
    assert not (tmp_path / "outside").exists()


def test_profile_cleanup_retries_transient_windows_handles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from clio_agent.runtime.document_stack import process

    class Profile:
        attempts = 0

        def cleanup(self) -> None:
            self.attempts += 1
            if self.attempts < 3:
                error = OSError("directory is not empty")
                error.winerror = 145
                raise error

    profile = Profile()
    profile.name = str(tmp_path / "private-profile")
    monkeypatch.setattr(process.time, "sleep", lambda duration: None)
    process._cleanup_profile(profile)  # type: ignore[arg-type]
    assert profile.attempts == 3


def test_conversion_profile_uses_allowed_temp_instead_of_nested_artifact_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.runtime.document_stack import process

    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(process.tempfile, "tempdir", str(temp))
    source = tmp_path / "source.docx"
    source.write_bytes(b"source preserved")
    output = tmp_path / "deep workspace" / "artifacts" / "revision" / "rendition"
    observed: list[Path] = []

    def convert(command: list[str], cwd: Path) -> None:
        profiles = list(temp.glob("*/profile"))
        assert len(profiles) == 1
        profile = profiles[0]
        observed.append(profile)
        assert profile.is_relative_to(temp.resolve())
        assert not profile.is_relative_to(output)
        assert f"-env:UserInstallation={profile.as_uri()}" in command
        assert "<value>3</value>" in (profile / "registrymodifications.xcu").read_text()
        assert cwd == output
        (output / "source.pdf").write_bytes(b"converted output")

    result = process.office_convert(
        source, output, "pdf", execute=convert, executable_path="soffice"
    )
    assert result == output / "source.pdf"
    assert source.read_bytes() == b"source preserved"
    assert observed and not observed[0].exists()
