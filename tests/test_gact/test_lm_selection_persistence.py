"""An LM provider bound through ``PUT /v1/providers/lm`` survives a restart.

Boot reads the provider selection from the config file (or ``CLIO_LM_PROVIDER``)
only, so a bind that lived in memory was lost on restart and the service came
back unconfigured. These tests bind through the same route the model picker
uses, then read exactly what the next boot reads: the config-file layer via
``explicit_lm_provider`` and ``load_config_from_env``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from clio_agent import conf
from clio_agent.config import load_config_from_env
from clio_agent.gact.app import build_app
from clio_agent.gact.providers.boot_selection import explicit_lm_provider
from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.capabilities.accessor import clear_cache


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> Any:
    invalidation.clear_all()
    clear_cache()
    for name in ("CLIO_LM_PROVIDER", "CLIO_LM_MODEL", "CLIO_LM_API_BASE"):
        monkeypatch.delenv(name, raising=False)
    yield
    invalidation.clear_all()
    clear_cache()


class _StubAgent:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.arc = SimpleNamespace(
            get_cache_stats=lambda: {"hits": 0, "misses": 0, "hit_rate": 0.0, "capacity": 10}
        )

    def rebind_lms(self, cfg: Any) -> None:
        self._provider_config = cfg
        self._main_lm = SimpleNamespace(model=cfg.model, provider=cfg.provider, history=[])
        self._dspy_adapter = None


@pytest.fixture
def stub_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("clio_agent.agent.ClioAgent", _StubAgent)
    monkeypatch.setattr("clio_agent.config.create_lm", lambda cfg: SimpleNamespace(history=[]))


def _user_config() -> dict[str, Any]:
    from clio_agent.gact.providers.selection_store import user_config_path  # noqa: PLC0415

    return yaml.safe_load(user_config_path().read_text(encoding="utf-8")) or {}


def _put(client: TestClient, **body: Any) -> Any:
    payload = {
        "provider": "openai",
        "provider_id": "vllm",
        "api_base": "http://127.0.0.1:1/v1",
        "model": "served-model",
        "api_key": "sk-inline-secret-must-not-persist",
        **body,
    }
    return client.put("/v1/providers/lm", json=payload)


def test_a_bind_through_the_picker_route_is_what_the_next_boot_reads(
    tmp_path: Path, stub_bind: None
) -> None:
    # Precondition: an untouched install reports no selection (the bug's end state).
    assert explicit_lm_provider() == ""

    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        response = _put(client)
    assert response.status_code == 200, response.text

    # "Restart": drop every cached layer and read only what boot reads.
    conf.reload()
    assert explicit_lm_provider() == "vllm"
    restored = load_config_from_env()
    assert restored.provider_id == "vllm"
    assert restored.api_base == "http://127.0.0.1:1/v1"
    assert restored.model == "served-model"


def test_the_persisted_selection_never_carries_a_secret_and_keeps_other_settings(
    tmp_path: Path, stub_bind: None
) -> None:
    before = _user_config()
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        assert _put(client).status_code == 200

    after = _user_config()
    text = yaml.safe_dump(after)
    assert "sk-inline-secret-must-not-persist" not in text
    assert after["lm"]["provider"] == "vllm"
    assert "api_key" not in after["lm"]
    # Unrelated settings the user (here: the test harness) wrote are preserved.
    for key, value in before.items():
        if key != "lm":
            assert after[key] == value


def test_switching_provider_replaces_the_previous_selection_keys(
    tmp_path: Path, stub_bind: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.providers.selection_store import (  # noqa: PLC0415
        persist_lm_selection,
    )

    codex = SimpleNamespace(
        provider="codex",
        provider_id="codex",
        model="gpt-5",
        codex_transport="websocket",
        codex_variant="direct",
        claude_code_transport="sdk",
    )
    assert persist_lm_selection(codex, requested_api_base="").persisted
    assert _user_config()["lm"]["codex_transport"] == "websocket"

    claude = SimpleNamespace(
        provider="claude_code",
        provider_id="claude_code",
        model="sonnet",
        codex_transport="websocket",
        codex_variant="",
        claude_code_transport="sdk",
    )
    assert persist_lm_selection(claude, requested_api_base="").persisted
    lm = _user_config()["lm"]
    assert lm["provider"] == "claude_code"
    assert lm["model"] == "sonnet"
    assert lm["claude_code_transport"] == "sdk"
    assert "codex_transport" not in lm and "codex_variant" not in lm
    assert "api_base" not in lm  # preset default applies


def test_an_unwritable_config_is_a_typed_reason_not_a_silent_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.providers import selection_store  # noqa: PLC0415

    blocked = tmp_path / "not-a-dir"
    blocked.write_text("a file where the config dir should be", encoding="utf-8")
    monkeypatch.setattr(selection_store, "user_config_path", lambda: blocked / "config.yaml")
    cfg = SimpleNamespace(
        provider="claude_code",
        provider_id="claude_code",
        model="sonnet",
        claude_code_transport="sdk",
        codex_transport="websocket",
        codex_variant="",
    )

    outcome = selection_store.persist_lm_selection(cfg, requested_api_base="")

    assert outcome.persisted is False
    assert outcome.reason == selection_store.LM_SELECTION_NOT_PERSISTED
    assert outcome.detail


def test_a_bind_whose_selection_cannot_be_saved_says_so_on_the_status(
    tmp_path: Path, stub_bind: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.providers import selection_store  # noqa: PLC0415

    blocked = tmp_path / "not-a-dir"
    blocked.write_text("x", encoding="utf-8")
    monkeypatch.setattr(selection_store, "user_config_path", lambda: blocked / "config.yaml")

    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        assert _put(client).status_code == 200
        info = client.get("/v1/providers/lm/wait", params={"timeout": 30}).json()
        status = client.app.state.lm_config_status

    assert info["configured"] is True  # the bind itself still works now
    assert status["selection_persisted"] is False
    assert status["selection_persist_reason"] == selection_store.LM_SELECTION_NOT_PERSISTED
    assert "not saved for the next restart" in info["status_message"]
