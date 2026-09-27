"""``/v1/providers/{id}/components``: the update check and the staged update routes.

The PyPI answer is the recorded JSON (``test_providers/fixtures/pypi``); the
update mechanics themselves are covered against a scratch venv in
``test_providers/test_provider_component_updater.py``, so here the updater's
work function is replaced and the ROUTE semantics are what is asserted: typed
refusals, the 202 + polling contract, the 409 while busy, and the catalog
invalidation once an update finishes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.lm_provider_types import LMProviderPreset
from clio_agent.gact.routes import provider_components as routes
from clio_agent.providers.components import client_binary, pypi, status, updater

FIXTURES = Path(__file__).resolve().parents[1] / "test_providers" / "fixtures" / "pypi"


def _preset(preset_id: str, provider: str) -> LMProviderPreset:
    return LMProviderPreset(
        id=preset_id, label=preset_id, provider=provider, api_base="", suggested_model=""
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    lookup = pypi.ReleaseLookup(
        fetch=lambda url: json.loads(
            (FIXTURES / f"{url.rstrip('/').split('/')[-2]}.json").read_text(encoding="utf-8")
        )
    )
    real_status = status.provider_component_status
    monkeypatch.setattr(
        routes,
        "provider_component_status",
        lambda kind, refresh=False: real_status(kind, refresh=refresh, lookup=lookup),
    )
    monkeypatch.setattr(
        status,
        "installed_version",
        lambda name: "0.2.156" if name == "claude-agent-sdk" else "0.147.0",
    )
    monkeypatch.setattr(
        pypi, "sys_tags", lambda: iter(pypi.Tag("py3", "none", t) for t in ("win_amd64", "any"))
    )
    monkeypatch.setattr(pypi, "_python_version", lambda: "3.12.0")
    selection = client_binary.ClientSelection(
        client_binary.ClientBinary("C:/npm/codex.exe", "0.157.1", "installed"),
        "codex_installed_cli",
        bundled=client_binary.ClientBinary("C:/site/codex.exe", "0.147.0", "bundled"),
    )
    monkeypatch.setattr(
        client_binary, "provider_client", lambda kind: selection if kind == "codex" else None
    )
    monkeypatch.setattr(routes, "UPDATER", updater.ComponentUpdater())
    app = FastAPI()
    routes.register_provider_component_routes(
        app,
        [
            _preset("codex", "codex"),
            _preset("claude_code", "claude_code"),
            _preset("openai", "openai"),
        ],
    )
    return TestClient(app)


def test_components_report_update_available_and_the_client_in_use(client: TestClient) -> None:
    body = client.get("/v1/providers/codex/components").json()
    assert body["provider_id"] == "codex"
    assert body["update_available"] is True
    assert body["target_version"] == "0.157.1"
    assert {c["distribution"]: c["installed_version"] for c in body["components"]} == {
        "openai-codex": "0.147.0",
        "openai-codex-cli-bin": "0.147.0",
    }
    assert body["client"]["source"] == "installed"
    assert body["client"]["version"] == "0.157.1"
    assert body["client"]["bundled_version"] == "0.147.0"
    assert body["update"] is None


def test_claude_code_components_on_windows_target_the_newest_windows_wheel(
    client: TestClient,
) -> None:
    body = client.get("/v1/providers/claude_code/components").json()
    assert (body["update_available"], body["target_version"]) == (True, "0.2.159")


def test_a_provider_without_components_is_a_typed_405(client: TestClient) -> None:
    response = client.get("/v1/providers/openai/components")
    assert response.status_code == 405
    assert response.json()["detail"]["error"]["error"] == "provider_has_no_components"
    assert client.post("/v1/providers/openai/components/update").status_code == 405


def test_an_unknown_provider_is_404(client: TestClient) -> None:
    assert client.get("/v1/providers/nope/components").status_code == 404


def test_update_starts_returns_202_and_is_polled_to_done(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    invalidated: list[str] = []
    monkeypatch.setattr(
        routes, "invalidate_provider", lambda _app, provider_id: invalidated.append(provider_id)
    )

    def _perform(job: updater.UpdateJob, _env: updater.UpdateEnvironment) -> None:
        job.from_versions, job.to_versions = (
            {"openai-codex": "0.147.0"},
            {"openai-codex": "0.157.1"},
        )
        for stage in ("downloading", "installing", "verifying"):
            job.stage = stage  # type: ignore[assignment]
            time.sleep(0.05)
        job.changed = True

    monkeypatch.setattr(updater, "_perform", _perform)
    started = client.post("/v1/providers/codex/components/update")
    assert started.status_code == 202
    assert started.json()["running"] is True

    seen: set[str] = set()
    deadline = time.monotonic() + 10
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = client.get("/v1/providers/codex/components/update").json()
        seen.add(body["stage"])
        if not body["running"]:
            break
        time.sleep(0.01)
    assert body["stage"] == "done" and body["changed"] is True
    assert "done" in seen and len(seen) >= 2
    assert invalidated == ["codex"]
    assert client.get("/v1/providers/codex/components").json()["update"]["stage"] == "done"


def test_a_second_update_while_one_runs_is_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes.UPDATER._claim("claude_code")  # type: ignore[attr-defined]
    response = client.post("/v1/providers/codex/components/update")
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["error"] == "component_update_in_progress"


def test_update_status_before_any_update_is_404(client: TestClient) -> None:
    response = client.get("/v1/providers/codex/components/update")
    assert response.status_code == 404
    assert response.json()["detail"]["error"]["error"] == "component_update_not_found"


def test_live_environment_targets_this_runtime_with_the_real_provider_check() -> None:
    import sys

    env = routes.live_update_environment()
    assert env.python == sys.executable
    assert env.verify_provider is updater.verify_provider_in_child
    assert env.release_runtimes is routes.release_provider_runtimes
