"""The first-use Chromium download outlives one browser action (iowarp/clio-agent#1975)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from clio_agent.gact.browser import install
from clio_agent.gact.browser.settings import BrowserError


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
