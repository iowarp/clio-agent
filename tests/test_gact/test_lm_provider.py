"""GET / PUT /v1/providers/lm — TUI configures the LM at runtime
without redeploying the GACT process.
"""

from __future__ import annotations

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
