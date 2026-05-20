"""GET / PUT /v1/providers/lm — TUI configures the LM at runtime
without redeploying the GACT process.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


def test_get_lm_provider_unconfigured(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        body = c.get("/v1/providers/lm").json()
        assert body["configured"] is False
        # Presets always shipped — TUI uses them to populate the picker.
        ids = {p["id"] for p in body["presets"]}
        assert "openai" in ids
        assert "openrouter" in ids
        assert "lm_studio" in ids
        assert "codex" in ids


def test_health_lm_row_when_unconfigured(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        rows = {r["name"]: r for r in c.get("/v1/health").json()["integrations"]}
        assert rows["lm"]["status"] == "unavailable"
        assert "PUT /v1/providers/lm" in rows["lm"]["detail"]


def test_get_lm_provider_when_configured_via_put(
    tmp_path: Path, monkeypatch
) -> None:
    """Stub out ClioAgent + create_lm so we can exercise the PUT
    code path without an LM endpoint."""

    fake_agent_constructed: dict[str, Any] = {}

    class _StubAgent:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            fake_agent_constructed["called"] = True
            self.arc = type("ARC", (), {
                "get_cache_stats": lambda self: {
                    "hits": 1, "misses": 0, "hit_rate": 1.0, "capacity": 10,
                }
            })()

        def forward(self, *args: Any, **kwargs: Any) -> Any:
            return type("Pred", (), {"answer": "ok", "selected_expert": ""})()

    monkeypatch.setattr(
        "clio_agent.agent.ClioAgent", _StubAgent
    )

    def _stub_create_lm(cfg: Any) -> Any:
        return type("FakeLM", (), {"history": []})()

    monkeypatch.setattr("clio_agent.config.create_lm", _stub_create_lm)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        # Initially unconfigured.
        assert c.get("/v1/providers/lm").json()["configured"] is False

        # Configure via PUT.
        resp = c.put(
            "/v1/providers/lm",
            json={
                "provider": "openai",
                "api_base": "http://127.0.0.1:3456/v1",
                "model": "claude-haiku-4-5-20251001",
                "api_key": "x",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["configured"] is True
        assert body["provider"] == "openai"
        assert body["model"] == "claude-haiku-4-5-20251001"
        assert fake_agent_constructed["called"] is True

        # Subsequent GET reports the configured state.
        body = c.get("/v1/providers/lm").json()
        assert body["configured"] is True
        assert body["api_base"] == "http://127.0.0.1:3456/v1"

        # Health row flips to ready.
        rows = {r["name"]: r for r in c.get("/v1/health").json()["integrations"]}
        assert rows["lm"]["status"] == "ready"
        assert "openai/claude-haiku-4-5-20251001" in rows["lm"]["detail"]


def test_put_lm_provider_accepts_codex_sdk_transport(
    tmp_path: Path, monkeypatch
) -> None:
    """The TUI can select and read back Codex SDK transport without an API key."""
    captured: dict[str, Any] = {}
    monkeypatch.delenv("CLIO_CODEX_TRANSPORT", raising=False)

    class _StubAgent:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.arc = type("ARC", (), {
                "get_cache_stats": lambda self: {
                    "hits": 0,
                    "misses": 0,
                    "hit_rate": 0.0,
                    "capacity": 10,
                }
            })()

        def forward(self, *args: Any, **kwargs: Any) -> Any:
            return type("Pred", (), {"answer": "ok", "selected_expert": ""})()

    monkeypatch.setattr("clio_agent.agent.ClioAgent", _StubAgent)

    def _stub_create_lm(cfg: Any) -> Any:
        captured["cfg"] = cfg
        return type("FakeLM", (), {"history": []})()

    monkeypatch.setattr("clio_agent.config.create_lm", _stub_create_lm)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.put(
            "/v1/providers/lm",
            json={
                "provider": "codex",
                "api_base": "codex://exec",
                "model": "gpt-5.5",
                "transport": "sdk",
            },
        )
        body = resp.json()
        get_body = c.get("/v1/providers/lm").json()

    assert resp.status_code == 200, resp.text
    assert body["transport"] == "sdk"
    assert get_body["transport"] == "sdk"
    assert captured["cfg"].provider == "codex"
    assert captured["cfg"].api_key == "x"
    assert captured["cfg"].codex_transport == "sdk"
    assert app.state.lm_config["transport"] == "sdk"
    assert os.environ["CLIO_CODEX_TRANSPORT"] == "sdk"


def test_put_lm_provider_invalid_returns_400(tmp_path: Path, monkeypatch) -> None:
    def _raises(cfg: Any) -> None:
        raise RuntimeError("bad creds")

    monkeypatch.setattr("clio_agent.config.create_lm", _raises)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.put(
            "/v1/providers/lm",
            json={
                "provider": "openai",
                "api_base": "http://nonsense",
                "model": "x",
                "api_key": "x",
            },
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["error"] == "config_error"
        assert "bad creds" in body["error"]["message"]


def test_put_lm_provider_failed_first_connect_restores_env(
    tmp_path: Path, monkeypatch
) -> None:
    """A rejected provider swap must not leak failed settings into env."""

    before = {
        "CLIO_LM_PROVIDER": "lm_studio",
        "CLIO_LM_API_BASE": "http://127.0.0.1:1234/v1",
        "CLIO_LM_MODEL": "stable-model",
        "CLIO_LM_API_KEY": "stable-key",
        "CLIO_CODEX_TRANSPORT": "sdk",
    }
    for key, value in before.items():
        monkeypatch.setenv(key, value)

    def _fake_lm(cfg: Any) -> object:
        return object()

    class _BoomAgent:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("agent boot failed")

    monkeypatch.setattr("clio_agent.config.create_lm", _fake_lm)
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", _fake_lm)
    monkeypatch.setattr("clio_agent.config.create_planner_lm", _fake_lm)
    monkeypatch.setattr("clio_agent.agent.ClioAgent", _BoomAgent)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.put(
            "/v1/providers/lm",
            json={
                "provider": "openai",
                "api_base": "http://rejected.example/v1",
                "model": "rejected-model",
                "api_key": "rejected-key",
            },
        )
        body = resp.json()
        get_body = c.get("/v1/providers/lm").json()

    assert resp.status_code == 400
    assert body["error"]["error"] == "config_error"
    assert "agent boot failed" in body["error"]["message"]
    assert {key: os.environ.get(key) for key in before} == before
    assert get_body["configured"] is False
    assert app.state.lm_config is None
