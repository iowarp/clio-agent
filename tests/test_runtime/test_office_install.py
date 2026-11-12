"""Renderer provisioning rejects tampering and uses private extraction, not system installs."""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
from pathlib import Path, PurePath
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.runtime.document_stack import office, office_fetch
from clio_agent.runtime.document_stack.process import DocumentError


@pytest.mark.parametrize("system,machine", office.ASSETS)
def test_official_assets_are_pinned(
    system: str, machine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(office.platform, "system", lambda: system)
    monkeypatch.setattr(office.platform, "machine", lambda: machine)
    url, filename, checksum = office.asset()
    assert url.startswith("https://github.com/iowarp/clio-agent/releases/download/office-")
    assert url.endswith(f"/office-{office.BUILD}/{filename}")
    assert filename.startswith(f"LibreOffice_{office.BUILD}_")
    fallback = office.asset_fallback()
    # TDF's archive names the full build in both the directory and the file name.
    assert fallback.startswith(
        f"https://downloadarchive.documentfoundation.org/libreoffice/old/{office.BUILD}/"
    )
    assert fallback.endswith(f"/{filename}")
    assert office.BUILD.startswith(office.VERSION + ".")
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
    monkeypatch.setattr(
        office_fetch.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"corrupt")
    )
    monkeypatch.setattr(office_fetch.time, "sleep", lambda seconds: None)
    with pytest.raises(DocumentError, match="pinned SHA-256"):
        office_fetch.download("https://example.invalid/official", tmp_path / "archive", "0" * 64)


def test_windows_renderer_is_extracted_to_private_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(office.platform, "system", lambda: "Windows")
    from clio_agent.runtime.document_stack import windows_msi

    seen: list[tuple[Path, Path, Any]] = []

    monkeypatch.setattr(
        windows_msi,
        "extract_application",
        lambda archive, root, skip=None: seen.append((archive, root, skip)),
    )
    archive = tmp_path / "CLIO Desktop" / "renderer.msi"
    private = tmp_path / "CLIO Desktop" / "private image"
    office._extract(archive, private)
    assert seen == [(archive, private, office.unread)]


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

    monkeypatch.setattr(process, "os", SimpleNamespace(name="nt", environ={}))
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
        assert kwargs.get("timeout") is None
        profiles.append(profile)
        raise DocumentError("version probe failed")

    monkeypatch.setattr(office, "run", fail)
    for _ in range(2):
        with pytest.raises(DocumentError, match="version probe failed"):
            office._verify_office(tmp_path / "soffice.com", cwd=tmp_path)
    assert profiles[0] != profiles[1]
    assert all(not profile.parent.exists() for profile in profiles)


@pytest.mark.parametrize("system", ["Windows", "Linux", "Darwin"])
def test_renderer_probe_waits_for_a_slow_first_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, system: str
) -> None:
    """A slow first start is waited for, never turned into a missing or broken Office.

    The probe runs once with no clock (``timeout`` unset); Office answering late is a
    working Office. SABOTAGE: restore a fixed probe timeout -> ``timeout`` is passed -> red.
    """
    import time

    calls: list[list[str]] = []

    def slow(command: list[str], **kwargs: Any) -> str:
        assert kwargs.get("timeout") is None
        calls.append(command)
        time.sleep(0.3)
        return "LibreOffice 26.2.6"

    monkeypatch.setattr(office.platform, "system", lambda: system)
    monkeypatch.setattr(office, "run", slow)
    office._verify_office(tmp_path / "soffice.com", cwd=tmp_path)
    assert len(calls) == 1
    assert "office_probe_timeout_retry" not in caplog.text


def test_renderer_probe_cancelled_is_not_a_missing_office(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stopping the turn mid-probe surfaces as cancelled, and the profile is still retired."""
    from urllib.parse import unquote, urlparse

    from clio_agent.runtime.document_stack.process import DocumentCancelled

    profiles: list[Path] = []

    def cancelled(command: list[str], **kwargs: Any) -> str:
        path = unquote(urlparse(command[1].split("=", 1)[1]).path)
        if path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        profiles.append(Path(path))
        raise DocumentCancelled("soffice.com was stopped")

    monkeypatch.setattr(office, "run", cancelled)
    with pytest.raises(DocumentCancelled):
        office._verify_office(tmp_path / "soffice.com", cwd=tmp_path)
    assert len(profiles) == 1 and not profiles[0].parent.exists()


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
        assert "<value>3</value>" in (profile / "user" / "registrymodifications.xcu").read_text()
        assert cwd == output
        (output / "source.pdf").write_bytes(b"converted output")

    result = process.office_convert(
        source, output, "pdf", execute=convert, executable_path="soffice"
    )
    assert result == output / "source.pdf"
    assert source.read_bytes() == b"source preserved"
    assert observed and not observed[0].exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="ditto is part of base macOS")
def test_macos_app_copy_keeps_bundle_symlinks_and_contents(tmp_path: Path) -> None:
    """The app leaves its disk image through ditto, which keeps signature xattrs."""
    source = tmp_path / "image" / "LibreOffice.app"
    binary = source / "Contents" / "MacOS" / "soffice"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    (source / "Contents" / "Frameworks").symlink_to("MacOS")
    target = tmp_path / "cache" / "LibreOffice.app"
    target.parent.mkdir()

    office._copy_app_bundle(source, target)

    assert (target / "Contents" / "Frameworks").is_symlink()
    assert (target / "Contents" / "MacOS" / "soffice").stat().st_mode & 0o111


def test_app_copy_uses_ditto_and_falls_back_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(office, "run", lambda command, **_: calls.append(command) or "")
    source = tmp_path / "LibreOffice.app"
    (source / "Contents").mkdir(parents=True)
    target = tmp_path / "out" / "LibreOffice.app"
    target.parent.mkdir()
    real_is_file = Path.is_file

    monkeypatch.setattr(
        Path, "is_file", lambda self: str(self) == "/usr/bin/ditto" or real_is_file(self)
    )
    office._copy_app_bundle(source, target)
    assert calls == [["/usr/bin/ditto", str(source), str(target)]]

    monkeypatch.setattr(
        Path, "is_file", lambda self: str(self) != "/usr/bin/ditto" and real_is_file(self)
    )
    office._copy_app_bundle(source, target)
    assert (target / "Contents").is_dir() and len(calls) == 1


@pytest.mark.parametrize("state", ["valid", "tampered", "symlink", "path", "unset"])
def test_installer_download_counts_only_the_pinned_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    import hashlib

    folder = tmp_path / "downloads"
    folder.mkdir()
    real = folder / "real.dmg"
    real.write_bytes(b"office")
    checksum = hashlib.sha256(b"office").hexdigest()
    name = "real.dmg"
    if state == "tampered":
        real.write_bytes(b"other")
    elif state == "symlink":
        (folder / "link.dmg").symlink_to(real)
        name = "link.dmg"
    elif state == "path":
        name = "../downloads/real.dmg"
    if state != "unset":
        monkeypatch.setenv(office.INSTALLER_DOWNLOADS, str(folder))
    else:
        monkeypatch.delenv(office.INSTALLER_DOWNLOADS, raising=False)
    expected = real if state == "valid" else None
    assert office.installer_download(name, checksum) == expected


@pytest.mark.parametrize("outcome", ["lands", "abandoned", "stalls"])
def test_installer_download_waits_for_a_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """The installer may still be writing NAME.part when the app prepares."""
    import hashlib
    import threading

    folder = tmp_path / "downloads"
    folder.mkdir()
    partial = folder / "office.dmg.part"
    partial.write_bytes(b"off")
    checksum = hashlib.sha256(b"office").hexdigest()
    monkeypatch.setenv(office.INSTALLER_DOWNLOADS, str(folder))
    monkeypatch.setattr(office, "_INSTALLER_STALL_S", 0.5)

    def finish() -> None:
        partial.write_bytes(b"office")
        if outcome == "lands":
            partial.rename(folder / "office.dmg")
        else:
            partial.unlink()

    timer = threading.Timer(0.3, finish)
    if outcome != "stalls":
        timer.start()
    try:
        found = office.installer_download("office.dmg", checksum)
    finally:
        timer.cancel()
    assert found == (folder / "office.dmg" if outcome == "lands" else None)


def test_installer_download_replaces_the_network_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    folder = tmp_path / "installer"
    folder.mkdir()
    prefetched = folder / "LibreOffice.dmg"
    prefetched.write_bytes(b"office image")
    checksum = hashlib.sha256(b"office image").hexdigest()
    monkeypatch.setenv(office.INSTALLER_DOWNLOADS, str(folder))
    monkeypatch.setattr(
        office, "asset", lambda: ("https://example.invalid/o", prefetched.name, checksum)
    )
    monkeypatch.setattr(office, "_download", lambda *a: pytest.fail("fetched from network"))
    monkeypatch.setattr(office.office_compat, "supported", lambda: False)
    monkeypatch.setattr(office, "_verify_office", lambda *a, **kw: None)
    extracted: list[Path] = []

    def extract(archive: Path, image: Path) -> None:
        extracted.append(archive)
        program = image / "LibreOffice.app/Contents/MacOS/soffice"
        program.parent.mkdir(parents=True)
        program.write_text("")
        program.chmod(0o755)

    monkeypatch.setattr(office, "_extract", extract)
    monkeypatch.setattr(office, "executable", lambda root: next(root.rglob("soffice"), None))
    root = tmp_path / "runtime" / "office"
    assert office.ensure_office(root).endswith("soffice")
    assert extracted == [prefetched]
    assert prefetched.read_bytes() == b"office image"


def test_macos_image_attach_skips_the_redundant_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(office.platform, "system", lambda: "Darwin")
    commands: list[list[str]] = []
    monkeypatch.setattr(office, "run", lambda command, **_: commands.append(command) or "")
    monkeypatch.setattr(office, "_copy_app_bundle", lambda source, target: None)
    root = tmp_path / "image"
    root.mkdir()
    office._extract(tmp_path / "office.dmg", root)
    assert commands[0][:2] == ["/usr/bin/hdiutil", "attach"]
    assert "-noverify" in commands[0]


def test_prune_unread_keeps_hyphenation_and_english_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spelling, thesaurus and non-English configuration go; what changes layout stays."""
    monkeypatch.setattr(office.platform, "system", lambda: "Windows")
    share = tmp_path / "LibreOffice" / "share"
    names = {
        "extensions/dict-de/de_DE_frami.dic": False,
        "extensions/dict-de/de_DE_frami.aff": False,
        "extensions/dict-de/th_de_DE_v2.dat": False,
        "extensions/dict-de/th_de_DE_v2.idx": False,
        "extensions/dict-de/hyph_de_DE.dic": True,
        "extensions/dict-de/dictionaries.xcu": True,
        "extensions/dict-th/th_TH.dic": False,
        "registry/res/fcfg_langpack_de.xcd": False,
        "registry/res/registry_pt-BR.xcd": False,
        "registry/res/fcfg_langpack_en-US.xcd": True,
        "registry/res/registry_en-GB.xcd": True,
        "registry/main.xcd": True,
    }
    for name in names:
        (share / name).parent.mkdir(parents=True, exist_ok=True)
        (share / name).write_bytes(b"x")
    (tmp_path / "LibreOffice" / "program").mkdir()

    assert office.prune_unread(tmp_path / "LibreOffice") == 7
    for name, kept in names.items():
        assert (share / name).exists() is kept, name


def test_unread_names_exactly_what_prune_unread_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extraction skips the same files pruning would delete, so the two never drift."""
    monkeypatch.setattr(office.platform, "system", lambda: "Windows")
    image = tmp_path / "office"
    names = [
        "program/soffice.com",
        "program/resource/de/LC_MESSAGES/sw.mo",
        "program/resource/en-GB/LC_MESSAGES/sw.mo",
        "help/en-US/text.xhp",
        "share/extensions/nlpsolver/solver.jar",
        "share/extensions/wiki-publisher/wiki.jar",
        "share/extensions/dict-de/de_DE_frami.dic",
        "share/extensions/dict-de/th_de_DE_v2.idx",
        "share/extensions/dict-de/hyph_de_DE.dic",
        "share/extensions/dict-de/dictionaries.xcu",
        "share/registry/res/fcfg_langpack_fr.xcd",
        "share/registry/res/registry_en-GB.xcd",
        "share/registry/main.xcd",
        "share/fonts/truetype/DejaVuSans.ttf",
        "share/Scripts/python/HelloWorld.py",
        "share/wizards/agenda.py",
        "program/wizards/fax/FaxWizard.py",
        "share/template/common/layout/lyt-blue.otp",
        "share/template/shellnew/soffice.odt",
        "share/gallery/arrows.sdg",
        "share/autocorr/acor_en-US.dat",
        "share/autotext/en-US/standard.bau",
        "share/fingerprint/en.lm",
        "share/tipoftheday/tipoftheday.png",
        "share/numbertext/en.sor",
        "share/config/soffice.cfg/modules/swriter/ui/gallery.ui",
        "share/config/images_colibre.zip",
        "share/config/images_colibre_dark.zip",
        "share/config/images_breeze.zip",
        "share/config/images_sifr_dark_svg.zip",
        "share/config/images_helpimg.zip",
        "program/classes/hsqldb.jar",
        "readmes/readme_de.txt",
        "readmes/readme_en-US.txt",
        "program/python-core-3.12.14/lib/pip/__init__.py",
        "program/python-core-3.12.14/lib/setuptools/dist.py",
        "program/python-core-3.12.14/lib/distutils-precedence.pth",
        "program/python-core-3.12.14/lib/encodings/utf_8.py",
        "program/python-core-3.12.14/lib/re/__init__.py",
    ]
    for name in names:
        (image / name).parent.mkdir(parents=True, exist_ok=True)
        (image / name).write_bytes(b"x")

    skipped = {name for name in names if office.unread(PurePath(name))}
    office.prune_unread(image)
    removed = {name for name in names if not (image / name).exists()}
    assert skipped == removed
    assert "share/extensions/dict-de/hyph_de_DE.dic" not in skipped
    assert "program/resource/de/LC_MESSAGES/sw.mo" in skipped
    assert {
        "share/Scripts/python/HelloWorld.py",
        "program/wizards/fax/FaxWizard.py",
        "share/gallery/arrows.sdg",
        "readmes/readme_de.txt",
        "program/python-core-3.12.14/lib/pip/__init__.py",
        "program/python-core-3.12.14/lib/distutils-precedence.pth",
        "share/autotext/en-US/standard.bau",
        "share/config/images_breeze.zip",
        "share/config/images_sifr_dark_svg.zip",
        "share/config/images_helpimg.zip",
        "program/classes/hsqldb.jar",
    } <= skipped
    # What a conversion reads: the startup Python, layout configuration, fonts, the
    # NUMBERTEXT data, new-document templates, the fallback icon themes and the English
    # readme.
    assert not skipped & {
        "program/python-core-3.12.14/lib/encodings/utf_8.py",
        "program/python-core-3.12.14/lib/re/__init__.py",
        "share/config/soffice.cfg/modules/swriter/ui/gallery.ui",
        "share/config/images_colibre.zip",
        "share/config/images_colibre_dark.zip",
        "share/fonts/truetype/DejaVuSans.ttf",
        "share/numbertext/en.sor",
        "share/template/shellnew/soffice.odt",
        "readmes/readme_en-US.txt",
    }


def test_icon_themes_autotext_and_java_are_pruned_only_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows-only prunes leave the Linux layout as it was."""
    names = [
        "share/config/images_breeze.zip",
        "share/config/images_helpimg.zip",
        "share/autotext/en-US/standard.bau",
        "program/classes/hsqldb.jar",
    ]
    monkeypatch.setattr(office.platform, "system", lambda: "Linux")
    image = tmp_path / "office"
    for name in names:
        (image / name).parent.mkdir(parents=True, exist_ok=True)
        (image / name).write_bytes(b"x")
    assert not any(office.unread(PurePath(name)) for name in names)
    assert office.prune_unread(image) == 0
    monkeypatch.setattr(office.platform, "system", lambda: "Windows")
    assert all(office.unread(PurePath(name)) for name in names)
    assert office.prune_unread(image) == len(names)
