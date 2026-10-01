"""Response settings through the GACT surface: catalog rows, PUT validation, GET echo.

* A catalog model row carries ``accepted_parameters`` -- only what the model accepts.
* ``PUT /v1/providers/lm`` refuses (typed 422) a value outside an accepted setting's
  range, before any bind starts.
* Every saved setting is stored and echoed on ``GET /v1/providers/lm`` -- including
  one the bound model does not accept (kept for the next model, never sent).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.provider_catalog import model_catalog_row
from clio_agent.gact.types import LMProviderPreset
from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.capabilities.accessor import clear_cache
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    DiscoveredModel,
    HandshakeReport,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    invalidation.clear_all()
    clear_cache()
    yield
    invalidation.clear_all()
    clear_cache()


def _report(provider_id: str, api_base: str, model_id: str) -> HandshakeReport:
    return HandshakeReport(
        provider_id=provider_id,
        provider_kind=provider_id,
        connectivity=ConnectivityState.OK,
        auth=AuthState.NOT_REQUIRED,
        api_base=api_base,
        models=(DiscoveredModel(id=model_id),),
        generated_at="2026-09-26T00:00:00+00:00",
    )


@pytest.mark.parametrize(
    ("preset", "expected"),
    [
        (
            LMProviderPreset(
                id="ollama",
                label="Ollama",
                provider="ollama",
                api_base="http://127.0.0.1:11434",
                suggested_model="",
            ),
            {"top_k", "min_p", "repetition_penalty", "context_length", "temperature"},
        ),
        (
            LMProviderPreset(
                id="claude_code",
                label="Claude Code",
                provider="claude_code",
                api_base="claude-code://sdk",
                suggested_model="",
            ),
            set(),
        ),
    ],
)
def test_catalog_row_serves_only_accepted_parameters(
    preset: LMProviderPreset, expected: set[str]
) -> None:
    report = _report(preset.id, preset.api_base, "m")
    row = model_catalog_row(preset, report, report.models[0])
    names = {parameter["name"] for parameter in row["accepted_parameters"]}
    assert expected <= names
    if not expected:
        assert row["accepted_parameters"] == []


class _StubAgent:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.arc = SimpleNamespace(
            get_cache_stats=lambda: {"hits": 0, "misses": 0, "hit_rate": 0.0, "capacity": 10}
        )

    def rebind_lms(self, cfg: Any) -> None:
        self._provider_config = cfg
        self._main_lm = SimpleNamespace(model=cfg.model, provider=cfg.provider, history=[])
        self._dspy_adapter = None

    def forward(self, *args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(answer="ok", selected_expert="")


@pytest.fixture
def bound(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def _create_lm(cfg: Any) -> Any:
        captured["cfg"] = cfg
        return SimpleNamespace(history=[])

    monkeypatch.setattr("clio_agent.agent.ClioAgent", _StubAgent)
    monkeypatch.setattr("clio_agent.config.create_lm", _create_lm)
    return captured


def _put(client: TestClient, **settings: Any) -> Any:
    return client.put(
        "/v1/providers/lm",
        json={
            "provider": "openai",
            "provider_id": "vllm",
            "api_base": "http://127.0.0.1:1/v1",
            "model": "m",
            "api_key": "x",
            **settings,
        },
    )


def test_put_refuses_a_value_outside_an_accepted_range(
    tmp_path: Path, bound: dict[str, Any]
) -> None:
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        resp = _put(client, temperature=3.5, top_k=2)
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["error"] == "response_setting_out_of_range"
    assert error["details"]["problems"] == ["temperature=3.5 is above 2"]
    assert "cfg" not in bound  # refused before any bind


def test_every_saved_setting_is_bound_and_echoed(tmp_path: Path, bound: dict[str, Any]) -> None:
    settings = {
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.05,
        "presence_penalty": 0.1,
        "frequency_penalty": 0.2,
        "repetition_penalty": 1.05,
        "seed": 42,
        "max_tokens": 2048,
    }
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        assert _put(client, **settings).status_code == 200
        echoed = client.get("/v1/providers/lm").json()
    for name, value in settings.items():
        assert echoed[name] == value, name
        assert getattr(bound["cfg"], name) == value, name


def test_a_setting_the_model_does_not_accept_is_kept_not_refused(
    tmp_path: Path, bound: dict[str, Any]
) -> None:
    # vLLM fixes its context at launch: no accepted context_length, so any value
    # is stored (it may apply to the next model) rather than validated.
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        assert _put(client, context_length=100).status_code == 200
        assert client.get("/v1/providers/lm").json()["context_length"] == 100
