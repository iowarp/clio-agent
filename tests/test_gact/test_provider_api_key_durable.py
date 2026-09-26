"""A key saved from the picker is durable, and Verify proves it.

Drives the real app factory and routes: ``POST /v1/providers/{id}/auth
{action: save_api_key}`` writes the durable 0600 store (never the process
environment), a second app built from the factory -- a service restart --
still sees the key, and ``GET .../handshake`` rejects a fake OpenRouter key
through the registry's key-check endpoint even though OpenRouter's model
listing is public. Only the network is faked (an ``httpx.MockTransport``).
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from clio_agent import paths
from clio_agent.gact.app import build_app
from clio_agent.providers.api_key_store import ProviderApiKeyStore
from clio_agent.providers.handshake import cache as handshake_cache

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _openrouter_preset(client: TestClient) -> dict[str, Any]:
    presets = client.get("/v1/providers/lm").json()["presets"]
    return next(preset for preset in presets if preset["id"] == "openrouter")


def _save(client: TestClient, api_key: str) -> Any:
    return client.post(
        "/v1/providers/openrouter/auth", json={"action": "save_api_key", "api_key": api_key}
    )


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("CLIO_LM_API_KEY", raising=False)
    handshake_cache.invalidate_provider("openrouter")
    yield
    # The handshake cache is process-wide: never leak this file's reports.
    handshake_cache.invalidate_provider("openrouter")


def test_a_saved_key_survives_a_restart_of_the_app_factory(tmp_path: Path) -> None:
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        assert _openrouter_preset(client)["is_authenticated"] is False
        saved = _save(client, "sk-or-v1-durable")
        assert saved.status_code == 200, saved.text
        assert _openrouter_preset(client)["is_authenticated"] is True

    # Never an in-memory-only copy: nothing went into the environment.
    assert "OPENROUTER_API_KEY" not in os.environ
    store_file = paths.user_config_dir() / "provider_api_keys.json"
    assert store_file.is_file()
    if os.name != "nt":  # POSIX mode bits; Windows relies on the profile ACL
        assert stat.S_IMODE(store_file.stat().st_mode) == 0o600

    # A fresh app from the factory is a service restart.
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        preset = _openrouter_preset(client)
        assert preset["is_authenticated"] is True
        assert preset.get("status") != "missing_key"
        assert ProviderApiKeyStore().load("openrouter") == "sk-or-v1-durable"

        cleared = client.post("/v1/providers/openrouter/auth", json={"action": "clear_api_key"})
        assert cleared.json()["is_authenticated"] is False

    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        assert _openrouter_preset(client)["is_authenticated"] is False


def test_verify_rejects_a_fake_key_even_though_the_model_listing_is_public(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("authorization", "")))
        if request.url.host == "openrouter.ai" and request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"data": [{"id": "openai/gpt-oss-120b:free"}]})
        if request.url.host == "openrouter.ai" and request.url.path == "/api/v1/key":
            return httpx.Response(401, json={"error": {"message": "No auth credentials found"}})
        return httpx.Response(404)

    def mock_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)

    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        assert _save(client, "sk-or-v1-fake").status_code == 200
        report = client.get(
            "/v1/providers/openrouter/handshake",
            params={"api_base": "https://openrouter.ai/api/v1", "refresh": "true"},
        ).json()

    assert report["auth"] == "rejected"
    assert report["error"].startswith("api_key_rejected")
    assert ("/api/v1/key", "Bearer sk-or-v1-fake") in seen


def test_the_runtime_credential_resolver_uses_the_saved_key_over_the_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn bound to OpenRouter authenticates with the key saved in the
    picker, not only the catalog -- one resolution order everywhere."""
    from clio_agent.providers import credentials

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-from-env")
    assert credentials.resolve("openrouter") == "sk-from-env"
    ProviderApiKeyStore().save("openrouter", "sk-saved")
    assert credentials.resolve("openrouter") == "sk-saved"
    ProviderApiKeyStore().clear("openrouter")
    assert credentials.resolve("openrouter") == "sk-from-env"
