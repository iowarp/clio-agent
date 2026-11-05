"""Attaching to the person's own Chrome, and running an installed Chrome (no browser needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.gact.browser import chrome_discovery, settings
from clio_agent.gact.browser.settings import BrowserError


def test_the_endpoint_chrome_allows_is_read_from_its_user_data_folder(tmp_path: Path) -> None:
    (tmp_path / "DevToolsActivePort").write_text("9333\n/devtools/browser/abc-123\n")
    endpoint = chrome_discovery.devtools_endpoint("chrome", folder=tmp_path)
    assert endpoint == "ws://127.0.0.1:9333/devtools/browser/abc-123"


def test_chrome_without_remote_debugging_says_how_to_allow_it(tmp_path: Path) -> None:
    with pytest.raises(BrowserError) as refused:
        chrome_discovery.devtools_endpoint("chrome", folder=tmp_path)
    assert refused.value.reason == "browser_cdp_not_enabled"
    assert "chrome://inspect/#remote-debugging" in str(refused.value)
    with pytest.raises(BrowserError) as edge:
        chrome_discovery.devtools_endpoint("msedge", folder=tmp_path)
    assert "edge://inspect/#remote-debugging" in str(edge.value)


@pytest.mark.parametrize(
    ("product", "platform", "tail"),
    [
        ("chrome", "linux", ".config/google-chrome"),
        ("chrome", "darwin", "Library/Application Support/Google/Chrome"),
        ("msedge", "darwin", "Library/Application Support/Microsoft Edge"),
        ("chrome", "win32", "Google/Chrome/User Data"),
    ],
)
def test_each_system_has_its_default_user_data_folder(
    product: str, platform: str, tail: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    folder = chrome_discovery.user_data_dir(product, platform=platform, home=tmp_path)
    assert folder.as_posix().endswith(tail)


def test_the_channel_names_chromium_chrome_or_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLIO_BROWSER_CHANNEL", raising=False)
    assert settings.channel() == "chromium"
    monkeypatch.setenv("CLIO_BROWSER_CHANNEL", "Chrome")
    assert settings.channel() == "chrome"
    monkeypatch.setenv("CLIO_BROWSER_CHANNEL", "firefox")
    with pytest.raises(ValueError, match="browser.channel"):
        settings.channel()
