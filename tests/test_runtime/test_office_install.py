"""Renderer provisioning rejects tampering and uses private extraction, not system installs."""

from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace
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


@pytest.mark.parametrize("machine", [m for system, m in office.ASSETS if system == "Linux"])
def test_every_linux_build_has_a_pinned_userland(
    machine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each Linux LibreOffice build CLIO downloads runs on its own private userland."""
    monkeypatch.setattr(office.office_compat.platform, "system", lambda: "Linux")
    monkeypatch.setattr(office.office_compat.platform, "machine", lambda: machine)
    assert office.office_compat.supported() is True
    assert office.office_compat.packages()


def test_bad_download_checksum_never_extracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(office.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"corrupt"))
    monkeypatch.setattr(office.time, "sleep", lambda seconds: None)
    with pytest.raises(DocumentError, match="pinned SHA-256"):
        office._download("https://example.invalid/official", tmp_path / "archive", "0" * 64)


def test_windows_renderer_is_extracted_to_private_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(office.platform, "system", lambda: "Windows")
    from clio_agent.runtime.document_stack import windows_msi

    seen: list[tuple[Path, Path]] = []

    monkeypatch.setattr(
        windows_msi, "extract_application", lambda archive, root: seen.append((archive, root))
    )
    archive = tmp_path / "CLIO Desktop" / "renderer.msi"
    private = tmp_path / "CLIO Desktop" / "private image"
    office._extract(archive, private)
    assert seen == [(archive, private)]


def test_explicit_windows_command_line_reaches_createprocess_without_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve MSI-specific quoting through the bounded subprocess owner."""
    from clio_agent.runtime.document_stack import process

    observed: dict[str, Any] = {}

    class Child:
        returncode = 0

        def communicate(self, *, timeout: float) -> tuple[str, str]:
            assert timeout == 300
            return "extracted", ""

    def start(args: str | list[str], **kwargs: Any) -> Child:
        observed.update(args=args, **kwargs)
        return Child()

    monkeypatch.setattr(process, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(process.subprocess, "Popen", start)
    command_line = 'msiexec.exe /a "renderer with spaces.msi" /qn TARGETDIR="private image"'
    assert (
        process.run(
            ["msiexec.exe", "/a", "renderer with spaces.msi", "/qn", "TARGETDIR=private image"],
            cwd=tmp_path,
            timeout=300,
            windows_command_line=command_line,
        )
        == "extracted"
    )
    assert observed["args"] == command_line
    assert observed["executable"] == "msiexec.exe"
    assert observed.get("shell", False) is False
    assert observed["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def test_private_renderer_is_probed_before_warm_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = tmp_path / "program" / "soffice.com"
    program.parent.mkdir()
    program.touch()
    seen: list[list[str]] = []
    # A Windows-layout tree: no private Linux userland applies.
    monkeypatch.setattr(office.office_compat, "supported", lambda: False)
    monkeypatch.setattr(office, "run", lambda command, **kwargs: seen.append(command) or "26.2.6")
    assert office.ensure_office(tmp_path) == str(program)
    assert len(seen) == 1
    assert seen[0][0] == str(program)
    assert seen[0][-1] == "--version"
    assert "--headless" in seen[0]
    assert seen[0][1].startswith("-env:UserInstallation=file:")


def test_renderer_probe_uses_and_cleans_an_exclusive_profile_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed startup cannot pollute the user's Office profile or leave its own."""
    from urllib.parse import unquote, urlparse

    profiles: list[Path] = []

    def fail(command: list[str], **kwargs: Any) -> str:
        profile_uri = command[1].split("=", 1)[1]
        path = unquote(urlparse(profile_uri).path)
        if path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        profile = Path(path)
        assert profile.parent.is_dir()
        assert "--headless" in command and "--norestore" in command
        assert kwargs["timeout"] == 30
        profiles.append(profile)
        raise DocumentError("version probe failed")

    monkeypatch.setattr(office, "run", fail)
    for _ in range(2):
        with pytest.raises(DocumentError, match="version probe failed"):
            office._verify_office(tmp_path / "soffice.com", cwd=tmp_path)
    assert profiles[0] != profiles[1]
    assert all(not profile.parent.exists() for profile in profiles)


@pytest.mark.parametrize("system,expected_attempts", [("Windows", 2), ("Linux", 1), ("Darwin", 1)])
def test_renderer_probe_timeout_has_one_windows_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system: str, expected_attempts: int
) -> None:
    """Only a native Windows timeout can retry, after the first profile is retired."""
    from urllib.parse import unquote, urlparse

    profiles: list[Path] = []

    def probe(command: list[str], **kwargs: Any) -> str:
        path = unquote(urlparse(command[1].split("=", 1)[1]).path)
        if path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        profile = Path(path)
        assert profile.parent.is_dir()
        assert all(not previous.parent.exists() for previous in profiles)
        assert kwargs["timeout"] == 30
        profiles.append(profile)
        if len(profiles) == 1:
            raise DocumentError("soffice.com exceeded 30 seconds") from subprocess.TimeoutExpired(
                command, 30
            )
        return "LibreOffice 26.2.6"

    monkeypatch.setattr(office.platform, "system", lambda: system)
    monkeypatch.setattr(office, "run", probe)
    if system == "Windows":
        office._verify_office(tmp_path / "soffice.com", cwd=tmp_path)
    else:
        with pytest.raises(DocumentError, match="exceeded 30 seconds"):
            office._verify_office(tmp_path / "soffice.com", cwd=tmp_path)
    assert len(profiles) == expected_attempts
    assert len(set(profiles)) == expected_attempts
    assert all(not profile.parent.exists() for profile in profiles)


def test_renderer_probe_persistent_windows_timeout_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A second timeout never becomes a successful installation or an endless retry."""
    calls: list[list[str]] = []

    def timeout(command: list[str], **kwargs: Any) -> str:
        assert kwargs["timeout"] == 30
        calls.append(command)
        raise DocumentError("soffice.com exceeded 30 seconds") from subprocess.TimeoutExpired(
            command, 30
        )

    monkeypatch.setattr(office.platform, "system", lambda: "Windows")
    monkeypatch.setattr(office, "run", timeout)
    with pytest.raises(DocumentError, match="exceeded 30 seconds") as caught:
        office._verify_office(tmp_path / "soffice.com", cwd=tmp_path)
    assert isinstance(caught.value.__cause__, subprocess.TimeoutExpired)
    assert len(calls) == 2
    assert calls[0][1] != calls[1][1]
    assert caplog.text.count("reason=office_probe_timeout_retry") == 1


def test_renderer_probe_windows_non_timeout_failure_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery must not hide an executable failure or treat it as a cold-start timeout."""
    calls: list[list[str]] = []

    def fail(command: list[str], **kwargs: Any) -> str:
        calls.append(command)
        raise DocumentError("soffice.com exited 1: invalid installation")

    monkeypatch.setattr(office.platform, "system", lambda: "Windows")
    monkeypatch.setattr(office, "run", fail)
    with pytest.raises(DocumentError, match="invalid installation"):
        office._verify_office(tmp_path / "soffice.com", cwd=tmp_path)
    assert len(calls) == 1


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


@pytest.mark.parametrize("winerror", [5, 32])
def test_publish_image_retries_only_transient_windows_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, winerror: int
) -> None:
    """A verified image can be renamed once a briefly retained native handle closes."""
    image, root = tmp_path / "image", tmp_path / "installed"
    image.mkdir()
    (image / "verified").write_text("preserved")
    original = Path.rename
    calls = 0

    def rename(path: Path, target: Path) -> Path:
        nonlocal calls
        calls += 1
        if calls < 3:
            error = OSError("handle still open")
            error.winerror = winerror
            raise error
        return original(path, target)

    monkeypatch.setattr(office.platform, "system", lambda: "Windows")
    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(office.time, "sleep", lambda _: None)
    office._publish_image(image, root)
    assert calls == 3
    assert (root / "verified").read_text() == "preserved"


@pytest.mark.parametrize(
    "system,winerror,attempts", [("Windows", 5, 20), ("Windows", 87, 1), ("Linux", 5, 1)]
)
def test_publish_image_persistent_or_unrelated_error_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system: str, winerror: int, attempts: int
) -> None:
    """No permission bypass, unbounded retry or cross-platform masking is allowed."""
    calls = 0

    def rename(path: Path, target: Path) -> Path:
        nonlocal calls
        calls += 1
        error = OSError("cannot move verified image")
        error.winerror = winerror
        raise error

    monkeypatch.setattr(office.platform, "system", lambda: system)
    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(office.time, "sleep", lambda _: None)
    with pytest.raises(OSError, match="cannot move"):
        office._publish_image(tmp_path / "image", tmp_path / "installed")
    assert calls == attempts


def test_publish_image_never_overwrites_an_existing_cache(tmp_path: Path) -> None:
    """A competing/incomplete cache needs an explicit repair, never replacement."""
    image, root = tmp_path / "image", tmp_path / "installed"
    image.mkdir()
    root.mkdir()
    (root / "keep").write_text("existing")
    with pytest.raises(DocumentError, match="Incomplete renderer cache"):
        office._publish_image(image, root)
    assert (root / "keep").read_text() == "existing"
    assert image.exists()


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
