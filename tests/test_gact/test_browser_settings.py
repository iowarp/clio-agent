"""Choosing the browser in Settings: saved in the user's config, applied at once."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent import conf
from clio_agent.gact.browser import service as browser_service
from clio_agent.gact.browser import settings as browser_settings
from clio_agent.gact.routes.session_defaults import register_session_defaults_routes
from tests._config_layer import read_config


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.chdir(tmp_path)
    for name in ("CLIO_BROWSER_BACKEND", "CLIO_BROWSER_CHANNEL", "CLIO_BROWSER_CDP_URL"):
        monkeypatch.delenv(name, raising=False)
    conf.reload()
    app = FastAPI()
    register_session_defaults_routes(app)
    return TestClient(app)


def _save(client: TestClient, changes: dict) -> object:
    revision = client.get("/v1/settings/runtime").json()["revision"]
    return client.patch("/v1/settings/runtime", json={"revision": revision, "changes": changes})


def test_settings_offer_the_browser_choices(client: TestClient) -> None:
    rows = {
        row["key"]: row
        for row in client.get("/v1/settings/runtime").json()["settings"]
        if row["group"] == "Browser"
    }
    assert [c["value"] for c in rows["browser.backend"]["choices"]] == ["managed", "cdp"]
    assert [c["value"] for c in rows["browser.channel"]["choices"]] == [
        "chromium",
        "chrome",
        "msedge",
    ]
    assert rows["browser.cdp_url"]["kind"] == "text" and rows["browser.cdp_url"]["value"] == ""


def test_a_new_browser_is_saved_and_switched_to_at_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    switched: list[bool] = []
    monkeypatch.setattr(browser_service, "switch_browser", lambda: switched.append(True) or 0)
    saved = _save(client, {"browser.backend": "cdp", "browser.cdp_url": " chrome "})
    assert saved.status_code == 200
    assert read_config()["browser"] == {"backend": "cdp", "cdp_url": "chrome"}
    assert browser_settings.backend() == "cdp" and browser_settings.cdp_url() == "chrome"
    assert switched == [True]
    assert _save(client, {"limits.lm_transient_retries": 3}).status_code == 200
    assert switched == [True]  # other settings leave the browser alone
    assert _save(client, {"browser.backend": None}).status_code == 200
    assert "backend" not in read_config()["browser"] and switched == [True, True]


def test_a_browser_setting_outside_its_choices_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    switched: list[bool] = []
    monkeypatch.setattr(browser_service, "switch_browser", lambda: switched.append(True) or 0)
    assert _save(client, {"browser.channel": "firefox"}).status_code == 422
    assert _save(client, {"browser.cdp_url": "chrome\nmsedge"}).status_code == 422
    assert _save(client, {"browser.cdp_url": 9222}).status_code == 422
    assert "browser" not in read_config() and switched == []
