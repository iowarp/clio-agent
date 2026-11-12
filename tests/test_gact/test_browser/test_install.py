"""The first-use Chromium download outlives one browser action (iowarp/clio-agent#1975)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import stat
import subprocess
import threading
import zipfile
from pathlib import Path

import pytest

from clio_agent.gact.browser import driver, install
from clio_agent.gact.browser.settings import BrowserError
from clio_agent.runtime import package_preparation


@pytest.fixture(autouse=True)
def _no_download() -> None:
    assert install._download is None


def test_a_slow_first_download_answers_preparing_and_keeps_going(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    calls: list[int] = []
    executable = tmp_path / "chrome"

    def download() -> Path:
        calls.append(1)
        release.wait(10)
        return executable

    monkeypatch.setattr(install, "ensure_chromium", download)

    try:
        with pytest.raises(BrowserError) as first:
            install.chromium_for_action(0.05)
        assert first.value.reason == install.REASON_PREPARING
        assert first.value.detail["status"] == "preparing"
        assert "first use only" in first.value.message

        # A second action while it downloads joins the same download.
        with pytest.raises(BrowserError):
            install.chromium_for_action(0.05)
        assert calls == [1]
    finally:
        release.set()
    assert install.chromium_for_action(5) == executable
    assert calls == [1]


def test_a_fast_download_is_used_by_the_same_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "chrome"
    monkeypatch.setattr(install, "ensure_chromium", lambda: executable)
    assert install.chromium_for_action(5) == executable


def test_a_failed_download_is_reported_and_retried_by_the_next_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail() -> Path:
        raise BrowserError(install.REASON_INSTALL_FAILED, "downloading Chromium failed")

    monkeypatch.setattr(install, "ensure_chromium", fail)
    with pytest.raises(BrowserError) as failed:
        install.chromium_for_action(5)
    assert failed.value.reason == install.REASON_INSTALL_FAILED

    executable = tmp_path / "chrome"
    monkeypatch.setattr(install, "ensure_chromium", lambda: executable)
    assert install.chromium_for_action(5) == executable


def test_another_process_downloading_is_a_typed_preparing_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install, "browsers_root", lambda: tmp_path)
    monkeypatch.setattr(install, "chromium_executable", lambda root=None: None)
    monkeypatch.setattr(install, "preparing_answer", install._preparing)
    with pytest.raises(BrowserError) as raised:
        install.ensure_chromium()
    assert raised.value.reason == install.REASON_PREPARING
    assert raised.value.detail == {"status": "preparing"}


_PLAYWRIGHT_TLS_STDOUT = (
    "Downloading Chrome for Testing 156.0.8078.4 (playwright chromium v1248) from "
    "https://cdn.playwright.dev/builds/cft/156.0.8078.4/linux64/chrome-linux64.zip\n"
    "Failed to install browsers\n"
    "Error: Failed to download Chrome for Testing 156.0.8078.4 (playwright chromium v1248), "
    "caused by\nError: Download failure, code=1\n"
)
_NODE_TLS_ERROR = (
    "Error: self-signed certificate in certificate chain\n"
    "    at TLSSocket.onConnectSecure (node:internal/tls/wrap:1788:34)\n"
    "    at TLSSocket.emit (node:events:514:28) {\n"
    "  code: 'SELF_SIGNED_CERT_IN_CHAIN'\n}\n"
)


def test_install_failure_names_the_cause_without_stack_frames() -> None:
    """Playwright's retried Node.js errors become one cause and its fix, never a stack."""
    message = install.install_failure(1, _PLAYWRIGHT_TLS_STDOUT, _NODE_TLS_ERROR * 5)

    assert "cdn.playwright.dev" in message
    assert "NODE_EXTRA_CA_CERTS" in message
    assert message.count("SELF_SIGNED_CERT_IN_CHAIN") == 1
    assert " at TLSSocket" not in message and "caused by" not in message


def test_install_failure_without_a_known_code_keeps_playwrights_error() -> None:
    message = install.install_failure(1, "", "Error: something else broke\n")

    assert message == (
        "Downloading Chromium failed: the download from the Playwright download host did not "
        "complete. Playwright reported: Error: something else broke."
    )


def _chromium_zip(path: Path, *, link: bool = False) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        executable = zipfile.ZipInfo("chrome-win64/chrome.exe")
        executable.external_attr = (stat.S_IFREG | 0o755) << 16  # as the real archive marks it
        archive.writestr(executable, b"MZ" * 1000)
        archive.writestr("chrome-win64/locales/en-US.pak", b"pak")
        for index in range(20):
            archive.writestr(f"chrome-win64/res/{index}.bin", bytes(index * 100))
        if link:
            entry = zipfile.ZipInfo("Chromium.app/Contents/Frameworks/Current")
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, "A")
    return path


def test_the_installer_archive_unpacks_whole_and_marked(tmp_path: Path) -> None:
    root = tmp_path / "browsers"
    (root / "chromium-7").mkdir(parents=True)  # an unfinished earlier attempt
    (root / "chromium-7" / "stale").write_text("x", encoding="utf-8")

    assert install._unpack_chromium(_chromium_zip(tmp_path / "c.zip"), root, "7")

    folder = root / "chromium-7"
    assert (folder / "INSTALLATION_COMPLETE").exists()
    assert (folder / "chrome-win64" / "chrome.exe").read_bytes() == b"MZ" * 1000
    assert len(list((folder / "chrome-win64" / "res").iterdir())) == 20
    assert not (folder / "stale").exists()
    # Playwright's cleanup ignores the working name; nothing is left under it.
    assert sorted(p.name for p in root.iterdir()) == ["chromium-7"]


def test_an_archive_with_links_is_left_to_playwright(tmp_path: Path) -> None:
    root = tmp_path / "browsers"
    root.mkdir()
    assert not install._unpack_chromium(_chromium_zip(tmp_path / "c.zip", link=True), root, "7")
    assert list(root.iterdir()) == []


def _fake_install(monkeypatch: pytest.MonkeyPatch, root: Path, calls: list[tuple]) -> None:
    monkeypatch.setattr(install, "browsers_root", lambda: root)
    monkeypatch.setattr(install, "preparing_answer", lambda: None)
    monkeypatch.setattr(install, "_driver", lambda: ("node", "cli.js"))
    monkeypatch.setattr(install, "chromium_revision", lambda: "7")
    monkeypatch.setattr(install, "playwright_version", lambda: "1.0")
    monkeypatch.setattr(package_preparation, "preparing", lambda _root: contextlib.nullcontext())

    def run(node: str, cli: str, where: Path, names: tuple[str, ...] = ("chromium",)):
        calls.append(names)
        if names == ("chromium",) and not (where / "chromium-7").exists():
            folder = where / "chromium-7" / "chrome-win64"
            folder.mkdir(parents=True)
            (folder / "chrome.exe").write_bytes(b"downloaded")
            (folder / "chrome.exe").chmod(0o755)  # chromium_executable needs X_OK on POSIX
            (where / "chromium-7" / "INSTALLATION_COMPLETE").write_text("")
        return subprocess.CompletedProcess([node], 0, "", "")

    monkeypatch.setattr(install, "_run_install", run)


_PIN = {"revision": "7", "url": "https://x/chrome-win64.zip", "sha256": "ab" * 32}


def test_ensure_chromium_unpacks_the_pinned_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, calls = tmp_path / "browsers", []
    _fake_install(monkeypatch, root, calls)
    archive = _chromium_zip(tmp_path / "chrome-win64.zip")
    monkeypatch.setattr(install, "_chromium_pin", lambda: _PIN)
    monkeypatch.setattr(install, "_fetch_pinned", lambda pin, where: archive)

    found = install.ensure_chromium()

    assert found.read_bytes() == b"MZ" * 1000
    helpers = ("ffmpeg", "winldd") if install.os.name == "nt" else ("ffmpeg",)
    assert calls == [helpers, ("chromium",)]
    assert not archive.exists()  # the download is spent


def test_a_failed_pinned_download_falls_back_to_playwright(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, calls = tmp_path / "browsers", []
    _fake_install(monkeypatch, root, calls)
    monkeypatch.setattr(install, "_chromium_pin", lambda: _PIN)

    def refuse(pin: dict, where: Path) -> Path:
        raise ValueError("the Chromium archive does not match its recorded SHA-256")

    monkeypatch.setattr(install, "_fetch_pinned", refuse)
    assert install.ensure_chromium().read_bytes() == b"downloaded"
    assert calls[-1] == ("chromium",)


def test_a_broken_pinned_archive_falls_back_to_playwright(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, calls = tmp_path / "browsers", []
    _fake_install(monkeypatch, root, calls)
    broken = tmp_path / "chrome-win64.zip"
    broken.write_bytes(b"not a zip")
    monkeypatch.setattr(install, "_chromium_pin", lambda: _PIN)
    monkeypatch.setattr(install, "_fetch_pinned", lambda pin, where: broken)

    assert install.ensure_chromium().read_bytes() == b"downloaded"
    assert calls[-1] == ("chromium",)
    assert not broken.exists()


def test_without_a_pin_playwright_installs_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, calls = tmp_path / "browsers", []
    _fake_install(monkeypatch, root, calls)
    monkeypatch.setattr(install, "_chromium_pin", lambda: None)
    assert install.ensure_chromium().read_bytes() == b"downloaded"
    assert calls == [("chromium",)]


def test_the_pin_is_used_only_for_this_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver, "manifest", lambda: {"chromium": dict(_PIN)})
    monkeypatch.setattr(install, "chromium_revision", lambda: "8")
    assert install._chromium_pin() is None
    monkeypatch.setattr(install, "chromium_revision", lambda: "7")
    assert install._chromium_pin() == _PIN
    monkeypatch.setattr(driver, "manifest", lambda: {"chromium": {**_PIN, "url": "http://x"}})
    assert install._chromium_pin() is None


class _Answer(io.BytesIO):
    def __enter__(self) -> _Answer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def test_the_pinned_download_keeps_only_matching_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"chromium bytes" * 1000
    monkeypatch.setattr(install.urllib.request, "urlopen", lambda *a, **k: _Answer(body))
    good = {**_PIN, "sha256": hashlib.sha256(body).hexdigest()}
    root = tmp_path / "browsers"
    archive = install._fetch_pinned(good, root)
    assert archive.read_bytes() == body
    archive.unlink()

    with pytest.raises(ValueError, match="SHA-256"):
        install._fetch_pinned(_PIN, root)
    assert list(root.iterdir()) == []
