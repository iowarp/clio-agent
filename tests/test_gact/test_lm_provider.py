"""GET / PUT /v1/providers/lm — TUI configures the LM at runtime
without redeploying the GACT process.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi.testclient import TestClient

from clio_agent.config import LMProviderConfig
from clio_agent.gact.app import build_app
from clio_agent.gact.providers.config import _effective_lm_config


def _wait_lm_provider_ready(c: TestClient, timeout_s: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = c.get("/v1/providers/lm").json()
        if body.get("state") != "configuring":
            return body
        time.sleep(0.05)
    raise AssertionError(f"LM provider did not finish configuring: {body}")


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


def test_effective_lm_config_reports_claude_code_transport() -> None:
    app = SimpleNamespace(
        state=SimpleNamespace(
            lm_config={},
            agent=SimpleNamespace(
                _provider_config=LMProviderConfig(
                    provider="claude_code",
                    api_base="claude-code://sdk",
                    model="haiku",
                    api_key="x",
                    claude_code_transport="exec",
                )
            ),
        )
    )

    cfg = _effective_lm_config(app)  # type: ignore[arg-type]

    assert cfg["provider"] == "claude_code"
    assert cfg["transport"] == "exec"


def test_get_lm_provider_reports_argonne_auth_required(tmp_path: Path, monkeypatch) -> None:
    """ALCF presets must not look usable when no Globus token exists."""

    monkeypatch.delenv("CLIO_ARGONNE_TOKEN", raising=False)
    monkeypatch.delenv("ALCF_INFERENCE_TOKEN", raising=False)
    monkeypatch.delenv("access_token", raising=False)
    monkeypatch.setattr("clio_agent.providers.argonne_auth.tokens_exist", lambda: False)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        body = c.get("/v1/providers/lm").json()

    sophia = next(p for p in body["presets"] if p["id"] == "argonne_sophia")
    assert sophia["auth_method"] == "oauth"
    assert sophia["is_authenticated"] is False
    assert sophia["status"] == "auth_required"
    assert "no Globus token" in sophia["status_message"]


def test_get_lm_provider_reports_argonne_valid_token_ready(tmp_path: Path, monkeypatch) -> None:
    """A refreshable cached Globus token should make ALCF selectable."""

    monkeypatch.delenv("CLIO_ARGONNE_TOKEN", raising=False)
    monkeypatch.delenv("ALCF_INFERENCE_TOKEN", raising=False)
    monkeypatch.delenv("access_token", raising=False)
    monkeypatch.setattr("clio_agent.providers.argonne_auth.tokens_exist", lambda: True)
    monkeypatch.setattr("clio_agent.providers.argonne_auth.check_auth_status", lambda: True)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        body = c.get("/v1/providers/lm").json()

    sophia = next(p for p in body["presets"] if p["id"] == "argonne_sophia")
    assert sophia["is_authenticated"] is True
    assert sophia["status"] == "ready"
    assert "validated" in sophia["status_message"]


def test_get_lm_provider_reports_argonne_refresh_failure(tmp_path: Path, monkeypatch) -> None:
    """Stored but unrefreshable tokens should ask for auth instead of looking usable."""

    monkeypatch.delenv("CLIO_ARGONNE_TOKEN", raising=False)
    monkeypatch.delenv("ALCF_INFERENCE_TOKEN", raising=False)
    monkeypatch.delenv("access_token", raising=False)
    monkeypatch.setattr("clio_agent.providers.argonne_auth.tokens_exist", lambda: True)
    monkeypatch.setattr("clio_agent.providers.argonne_auth.check_auth_status", lambda: False)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        body = c.get("/v1/providers/lm").json()

    sophia = next(p for p in body["presets"] if p["id"] == "argonne_sophia")
    assert sophia["is_authenticated"] is False
    assert sophia["status"] == "auth_required"
    assert "could not be refreshed" in sophia["status_message"]


def test_auth_provider_returns_interactive_argonne_instructions(
    tmp_path: Path, monkeypatch
) -> None:
    """ALCF auth must launch/describe an interactive flow, not block the backend."""

    import importlib.util

    from clio_agent.providers import argonne_auth

    popen_calls: list[list[str]] = []
    original_find_spec = importlib.util.find_spec

    def _find_spec(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "globus_sdk":
            return object()
        return original_find_spec(name, *args, **kwargs)

    def _popen(cmd: list[str], *args: Any, **kwargs: Any) -> object:
        popen_calls.append(cmd)
        return object()

    # The auth_provider handler moved to routes/providers.py (#714); patch the
    # module-level importlib/subprocess/shutil it resolves there.
    monkeypatch.setattr("clio_agent.gact.routes.providers.importlib.util.find_spec", _find_spec)
    monkeypatch.setattr(
        argonne_auth,
        "check_auth_status",
        lambda: (_ for _ in ()).throw(AssertionError("auth button must not probe token status")),
    )
    monkeypatch.setattr("clio_agent.gact.routes.providers.subprocess.Popen", _popen)
    if os.name != "nt":
        monkeypatch.setattr("clio_agent.gact.routes.providers.shutil.which", lambda name: None)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.post("/v1/providers/argonne_sophia/auth", json={"force": True})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_authenticated"] is False
    assert body["provider_id"] == "argonne_sophia"
    assert "interactive terminal" in body["instructions"] or "Opened" in body["instructions"]
    if os.name == "nt":
        assert popen_calls
        launched = popen_calls[0]
        assert launched[0].lower().endswith(("powershell.exe", "pwsh.exe"))
        assert "-NoExit" in launched
        assert "clio_agent.providers.argonne_auth" in launched[-1]
        assert "--force" in launched[-1]
        assert "Read-Host" in launched[-1]


def _patch_run_handshake(monkeypatch, report) -> None:
    """Make the picker's unified handshake return a canned report.

    These are endpoint-wiring tests; per-provider parsing is covered in
    tests/test_providers/ against captured fixtures.
    """

    async def _fake(ctx, **kwargs):  # noqa: ANN001, ANN003
        return report

    monkeypatch.setattr("clio_agent.providers.handshake.run_handshake", _fake)


def test_provider_model_catalog_returns_handshake_models(tmp_path: Path, monkeypatch) -> None:
    """The picker renders whatever the unified handshake discovered (to_models_wire)."""
    from clio_agent.providers.handshake import (
        AuthState,
        ConnectivityState,
        HandshakeReport,
        ModelProfile,
    )

    report = HandshakeReport(
        provider_id="lm_studio",
        provider_kind="lm_studio",
        connectivity=ConnectivityState.OK,
        auth=AuthState.NOT_REQUIRED,
        models=(
            ModelProfile(
                id="qwopus3.5-9b-v3",
                context_window=262144,
                quantization="Q4_K_M",
                context_source="live",
            ),
        ),
    )
    _patch_run_handshake(monkeypatch, report)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        body = c.get("/v1/providers/lm_studio/models").json()

    assert body["source"] == "live"
    assert len(body["models"]) == 1
    row = body["models"][0]
    assert row["id"] == "qwopus3.5-9b-v3"
    assert row["context_window"] == 262144
    assert row["quantization"] == "Q4_K_M"
    assert row["context_source"] == "live"


def test_provider_model_catalog_empty_live_provider_is_live(tmp_path: Path, monkeypatch) -> None:
    """A reachable provider with no models is 'live' with an empty list, not unavailable."""
    from clio_agent.providers.handshake import (
        AuthState,
        ConnectivityState,
        HandshakeReport,
    )

    report = HandshakeReport(
        provider_id="ollama",
        provider_kind="ollama",
        connectivity=ConnectivityState.OK,
        auth=AuthState.NOT_REQUIRED,
        models=(),
    )
    _patch_run_handshake(monkeypatch, report)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        body = c.get("/v1/providers/ollama/models").json()

    assert body["models"] == []
    assert body["source"] == "live"


def test_provider_model_catalog_unavailable_live_provider_has_no_static(
    tmp_path: Path, monkeypatch
) -> None:
    """A live provider that fails reports unavailable -- never stale static choices."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("CLIO_LM_API_KEY", raising=False)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        body = c.get("/v1/providers/openrouter/models").json()

    assert body["models"] == []
    assert body["source"] == "unavailable"
    assert body.get("error")


def test_provider_model_catalog_keeps_static_cli_candidates(tmp_path: Path) -> None:
    """CLI providers (codex/claude_code) expose an editable static candidate catalog."""
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        codex = c.get("/v1/providers/codex/models").json()
        claude = c.get("/v1/providers/claude_code/models").json()

    assert codex["source"] == "static_catalog"
    assert {row["id"] for row in codex["models"]} >= {"gpt-5.5", "gpt-5.1"}
    assert claude["source"] == "static_catalog"
    assert {row["id"] for row in claude["models"]} == {"sonnet", "opus", "haiku"}


def test_provider_model_catalog_unknown_provider_404(tmp_path: Path) -> None:
    """An unknown provider id is a clean 404."""
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.get("/v1/providers/totally-unknown/models")
    assert resp.status_code == 404


def test_health_lm_row_when_unconfigured(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        rows = {r["name"]: r for r in c.get("/v1/health").json()["integrations"]}
        assert rows["lm"]["status"] == "unavailable"
        assert "PUT /v1/providers/lm" in rows["lm"]["detail"]


def test_get_lm_provider_when_configured_from_boot_agent(tmp_path: Path) -> None:
    """Env-booted agents should expose their effective provider config."""

    agent = SimpleNamespace(
        _provider_config=SimpleNamespace(
            provider="lm_studio",
            api_base="http://127.0.0.1:1234/v1",
            model="qwopus3.5-9b-v3",
            temperature=0.0,
            max_tokens=4096,
            context_length=32768,
            thinking_budget=0,
            codex_transport="exec",
        )
    )
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        body = c.get("/v1/providers/lm").json()
        rows = {r["name"]: r for r in c.get("/v1/health").json()["integrations"]}

    assert body["configured"] is True
    assert body["provider"] == "lm_studio"
    assert body["api_base"] == "http://127.0.0.1:1234/v1"
    assert body["model"] == "qwopus3.5-9b-v3"
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 4096
    assert body["context_length"] == 32768
    assert body["transport"] is None
    assert rows["lm"]["status"] == "ready"
    assert rows["lm"]["detail"] == "lm_studio/qwopus3.5-9b-v3"


def test_health_reports_argonne_token_failure_when_connected(tmp_path: Path, monkeypatch) -> None:
    """A connected ALCF provider is unavailable if its Globus token is missing."""

    monkeypatch.setattr("clio_agent.providers.argonne_auth.tokens_exist", lambda: False)

    agent = SimpleNamespace(
        _provider_config=SimpleNamespace(
            provider="argonne",
            api_base="https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1",
            model="meta-llama/Meta-Llama-3.1-8B-Instruct",
            temperature=0.0,
            max_tokens=4096,
            context_length=0,
            thinking_budget=0,
        )
    )
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        resp = c.get("/v1/health")

    assert resp.status_code == 503
    rows = {r["name"]: r for r in resp.json()["integrations"]}
    assert rows["lm"]["status"] == "unavailable"
    assert "token missing" in rows["lm"]["detail"]


def test_get_lm_provider_when_configured_via_put(tmp_path: Path, monkeypatch) -> None:
    """Stub out ClioAgent + create_lm so we can exercise the PUT
    code path without an LM endpoint."""

    fake_agent_constructed: dict[str, Any] = {}

    class _StubAgent:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            fake_agent_constructed["called"] = True
            self.arc = type(
                "ARC",
                (),
                {
                    "get_cache_stats": lambda self: {
                        "hits": 1,
                        "misses": 0,
                        "hit_rate": 1.0,
                        "capacity": 10,
                    }
                },
            )()

        def forward(self, *args: Any, **kwargs: Any) -> Any:
            return type("Pred", (), {"answer": "ok", "selected_expert": ""})()

    monkeypatch.setattr("clio_agent.agent.ClioAgent", _StubAgent)

    def _stub_create_lm(cfg: Any) -> Any:
        return type("FakeLM", (), {"history": []})()

    monkeypatch.setattr("clio_agent.config.create_lm", _stub_create_lm)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        # Initially unconfigured.
        assert c.get("/v1/providers/lm").json()["configured"] is False
        stale_session = c.post(
            "/v1/sessions",
            json={
                "workspace_id": "ws_default",
                "model": {"provider_id": "anthropic", "model_id": "claude-opus-4-7"},
            },
        ).json()
        assert stale_session["model"]["provider_id"] == "anthropic"

        # Configure via PUT.
        resp = c.put(
            "/v1/providers/lm",
            json={
                "provider": "openai",
                "api_base": "http://127.0.0.1:3456/v1",
                "model": "claude-haiku-4-5-20251001",
                "api_key": "x",
                "context_length": 16384,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["configured"] is True
        assert body["provider"] == "openai"
        assert body["model"] == "claude-haiku-4-5-20251001"
        assert body["context_length"] == 16384
        assert fake_agent_constructed["called"] is True

        # Subsequent GET reports the configured state.
        body = c.get("/v1/providers/lm").json()
        assert body["configured"] is True
        assert body["api_base"] == "http://127.0.0.1:3456/v1"
        assert body["context_length"] == 16384
        assert app.state.lm_config["context_length"] == 16384
        refreshed_session = c.get(f"/v1/sessions/{stale_session['id']}").json()
        assert refreshed_session["model"] == {"provider_id": "", "model_id": "", "variant": ""}

        # Health row flips to ready.
        rows = {r["name"]: r for r in c.get("/v1/health").json()["integrations"]}
        assert rows["lm"]["status"] == "ready"
        assert "openai/claude-haiku-4-5-20251001" in rows["lm"]["detail"]


def test_put_argonne_uses_provider_default_max_tokens_when_omitted(
    tmp_path: Path, monkeypatch
) -> None:
    """TUI default save should not force the global 32k cap onto ALCF."""

    captured: dict[str, Any] = {}

    class _StubAgent:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.arc = type(
                "ARC",
                (),
                {
                    "get_cache_stats": lambda self: {
                        "hits": 0,
                        "misses": 0,
                        "hit_rate": 0.0,
                        "capacity": 10,
                    }
                },
            )()

        def forward(self, *args: Any, **kwargs: Any) -> Any:
            return type("Pred", (), {"answer": "ok", "selected_expert": ""})()

    def _stub_create_lm(cfg: Any) -> object:
        captured["provider"] = cfg.provider
        captured["api_base"] = cfg.api_base
        captured["model"] = cfg.model
        captured["api_key"] = cfg.api_key
        captured["max_tokens"] = cfg.max_tokens
        return type("FakeLM", (), {"history": []})()

    monkeypatch.setattr("clio_agent.agent.ClioAgent", _StubAgent)
    monkeypatch.setattr("clio_agent.config._resolve_argonne_api_key", lambda: "token")
    monkeypatch.setattr("clio_agent.config.create_lm", _stub_create_lm)
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", lambda cfg: object())
    monkeypatch.setattr("clio_agent.config.create_planner_lm", lambda cfg: object())

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.put(
            "/v1/providers/lm",
            json={
                "provider": "argonne",
                "api_base": "https://inference-api.alcf.anl.gov/resource_server/metis/api/v1",
                "model": "gpt-oss-120b",
                "api_key": "",
            },
        )
        body = _wait_lm_provider_ready(c)

    assert resp.status_code == 200, resp.text
    assert body["state"] == "ready"
    assert captured == {
        "provider": "argonne",
        "api_base": "https://inference-api.alcf.anl.gov/resource_server/metis/api/v1",
        "model": "gpt-oss-120b",
        "api_key": "token",
        "max_tokens": 4096,
    }


def test_put_argonne_preset_id_normalizes_to_runtime_provider_kind(
    tmp_path: Path, monkeypatch
) -> None:
    """Preset ids are catalog choices; runtime checks need provider kind."""

    captured: dict[str, Any] = {}

    class _StubAgent:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.arc = type(
                "ARC",
                (),
                {
                    "get_cache_stats": lambda self: {
                        "hits": 0,
                        "misses": 0,
                        "hit_rate": 0.0,
                        "capacity": 10,
                    }
                },
            )()

        def forward(self, *args: Any, **kwargs: Any) -> Any:
            return type("Pred", (), {"answer": "ok", "selected_expert": ""})()

    def _stub_create_lm(cfg: Any) -> object:
        captured["provider"] = cfg.provider
        captured["api_base"] = cfg.api_base
        captured["model"] = cfg.model
        captured["api_key"] = cfg.api_key
        return type("FakeLM", (), {"history": []})()

    monkeypatch.setattr("clio_agent.agent.ClioAgent", _StubAgent)
    monkeypatch.setattr("clio_agent.config._resolve_argonne_api_key", lambda: "token")
    monkeypatch.setattr("clio_agent.config.create_lm", _stub_create_lm)
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", lambda cfg: object())
    monkeypatch.setattr("clio_agent.config.create_planner_lm", lambda cfg: object())

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.put(
            "/v1/providers/lm",
            json={
                "provider": "argonne_sophia",
                "api_base": "https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1",
                "model": "openai/gpt-oss-120b",
                "api_key": "",
            },
        )
        body = _wait_lm_provider_ready(c)

    assert resp.status_code == 200, resp.text
    assert body["state"] == "ready"
    assert body["provider"] == "argonne"
    assert captured == {
        "provider": "argonne",
        "api_base": "https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1",
        "model": "openai/gpt-oss-120b",
        "api_key": "token",
    }
    assert app.state.lm_config["provider"] == "argonne"


def test_put_argonne_ignores_placeholder_api_key(tmp_path: Path, monkeypatch) -> None:
    """OAuth providers must not treat local no-auth placeholder keys as bearer tokens."""

    captured: dict[str, Any] = {}

    class _StubAgent:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.arc = type(
                "ARC",
                (),
                {
                    "get_cache_stats": lambda self: {
                        "hits": 0,
                        "misses": 0,
                        "hit_rate": 0.0,
                        "capacity": 10,
                    }
                },
            )()

        def forward(self, *args: Any, **kwargs: Any) -> Any:
            return type("Pred", (), {"answer": "ok", "selected_expert": ""})()

    def _stub_create_lm(cfg: Any) -> object:
        captured["api_key"] = cfg.api_key
        return type("FakeLM", (), {"history": []})()

    monkeypatch.setattr("clio_agent.agent.ClioAgent", _StubAgent)
    monkeypatch.setattr("clio_agent.config._resolve_argonne_api_key", lambda: "fresh-globus-token")
    monkeypatch.setattr("clio_agent.config.create_lm", _stub_create_lm)
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", lambda cfg: object())
    monkeypatch.setattr("clio_agent.config.create_planner_lm", lambda cfg: object())

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.put(
            "/v1/providers/lm",
            json={
                "provider": "argonne",
                "api_base": "https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1",
                "model": "meta-llama/Meta-Llama-3.1-8B-Instruct",
                "api_key": "x",
            },
        )
        body = _wait_lm_provider_ready(c)

    assert resp.status_code == 200, resp.text
    assert body["state"] == "ready"
    assert captured["api_key"] == "fresh-globus-token"


def test_argonne_runtime_refresh_updates_live_lm_kwargs(monkeypatch) -> None:
    """A long-lived ALCF agent should refresh bearer tokens before turns."""

    from clio_agent.gact.app import _refresh_argonne_lm_token

    monkeypatch.setattr("clio_agent.config._resolve_argonne_api_key", lambda: "runtime-token")
    main_lm = SimpleNamespace(kwargs={"api_key": "old-token"})
    planner_lm = SimpleNamespace(kwargs={"api_key": "old-token"})
    router_lm = SimpleNamespace(kwargs={"api_key": "old-token"})
    agent = SimpleNamespace(
        _provider_config=SimpleNamespace(provider="argonne", api_key="old-token"),
        _main_lm=main_lm,
        _planner_lm=planner_lm,
        _router_lm=router_lm,
    )

    _refresh_argonne_lm_token(agent)

    assert agent._provider_config.api_key == "runtime-token"
    assert main_lm.kwargs["api_key"] == "runtime-token"
    assert planner_lm.kwargs["api_key"] == "runtime-token"
    assert router_lm.kwargs["api_key"] == "runtime-token"


def test_put_lm_provider_accepts_codex_sdk_transport(tmp_path: Path, monkeypatch) -> None:
    """The TUI can select and read back Codex SDK transport without an API key."""
    captured: dict[str, Any] = {}
    monkeypatch.delenv("CLIO_CODEX_TRANSPORT", raising=False)

    class _StubAgent:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.arc = type(
                "ARC",
                (),
                {
                    "get_cache_stats": lambda self: {
                        "hits": 0,
                        "misses": 0,
                        "hit_rate": 0.0,
                        "capacity": 10,
                    }
                },
            )()

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


def test_put_lm_provider_accepts_claude_code_exec_transport(tmp_path: Path, monkeypatch) -> None:
    """Claude Code runtime config must apply transport to claude_code_transport."""
    monkeypatch.delenv("CLIO_CLAUDE_CODE_TRANSPORT", raising=False)
    captured: dict[str, Any] = {}

    class _StubAgent:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.arc = type(
                "ARC",
                (),
                {
                    "get_cache_stats": lambda self: {
                        "hits": 0,
                        "misses": 0,
                        "hit_rate": 0.0,
                        "capacity": 10,
                    }
                },
            )()

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
                "provider": "claude_code",
                "api_base": "claude-code://exec",
                "model": "haiku",
                "transport": "exec",
            },
        )
        body = resp.json()
        get_body = c.get("/v1/providers/lm").json()

    assert resp.status_code == 200, resp.text
    assert body["transport"] == "exec"
    assert get_body["transport"] == "exec"
    assert captured["cfg"].provider == "claude_code"
    assert captured["cfg"].api_key == "x"
    assert captured["cfg"].claude_code_transport == "exec"
    assert app.state.lm_config["transport"] == "exec"
    assert os.environ["CLIO_CLAUDE_CODE_TRANSPORT"] == "exec"


def test_put_lm_provider_defaults_claude_code_to_sdk_transport(tmp_path: Path, monkeypatch) -> None:
    """Claude Code should use the streaming-capable SDK path unless exec is explicit."""
    monkeypatch.delenv("CLIO_CLAUDE_CODE_TRANSPORT", raising=False)
    captured: dict[str, Any] = {}

    class _StubAgent:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.arc = type(
                "ARC",
                (),
                {
                    "get_cache_stats": lambda self: {
                        "hits": 0,
                        "misses": 0,
                        "hit_rate": 0.0,
                        "capacity": 10,
                    }
                },
            )()

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
                "provider": "claude_code",
                "api_base": "claude-code://sdk",
                "model": "haiku",
            },
        )
        body = resp.json()
        get_body = c.get("/v1/providers/lm").json()

    assert resp.status_code == 200, resp.text
    assert body["transport"] == "sdk"
    assert get_body["transport"] == "sdk"
    assert captured["cfg"].provider == "claude_code"
    assert captured["cfg"].api_key == "x"
    assert captured["cfg"].claude_code_transport == "sdk"
    assert app.state.lm_config["transport"] == "sdk"
    assert os.environ["CLIO_CLAUDE_CODE_TRANSPORT"] == "sdk"


def test_put_lm_provider_applies_lm_studio_context_length(tmp_path: Path, monkeypatch) -> None:
    """LM Studio context length is a model-load setting, not a chat completion param."""

    captured: dict[str, Any] = {}
    posts: list[dict[str, Any]] = []

    class _GetResp:
        status_code = 200
        text = ""

        def json(self) -> dict[str, Any]:
            return {"models": []}

    class _PostResp:
        status_code = 200
        text = ""

        def json(self) -> dict[str, Any]:
            return {"instance_id": "qwopus3.5-9b-v3", "status": "loaded"}

    class _StubAgent:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.arc = type(
                "ARC",
                (),
                {
                    "get_cache_stats": lambda self: {
                        "hits": 0,
                        "misses": 0,
                        "hit_rate": 0.0,
                        "capacity": 10,
                    }
                },
            )()

        def forward(self, *args: Any, **kwargs: Any) -> Any:
            return type("Pred", (), {"answer": "ok", "selected_expert": ""})()

    def _post(url: str, *args: Any, **kwargs: Any) -> _PostResp:
        call = {
            "url": url,
            "json": kwargs.get("json"),
            "timeout": kwargs.get("timeout"),
        }
        captured.update(call)
        posts.append(call)
        return _PostResp()

    monkeypatch.setattr("requests.get", lambda *args, **kwargs: _GetResp())
    monkeypatch.setattr("requests.post", _post)
    monkeypatch.setattr("clio_agent.agent.ClioAgent", _StubAgent)
    monkeypatch.setattr("clio_agent.config.create_lm", lambda cfg: object())
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", lambda cfg: object())
    monkeypatch.setattr("clio_agent.config.create_planner_lm", lambda cfg: object())

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.put(
            "/v1/providers/lm",
            json={
                "provider": "lm_studio",
                "api_base": "http://127.0.0.1:1234/v1",
                "model": "qwopus3.5-9b-v3",
                "context_length": 32768,
            },
        )
        assert resp.status_code == 200, resp.text
        body = _wait_lm_provider_ready(c)
        assert body["state"] == "ready"
        assert posts[0] == {
            "url": "http://127.0.0.1:1234/api/v1/models/load",
            "json": {
                "model": "qwopus3.5-9b-v3",
                "context_length": 32768,
                # concurrency cap (default 1) + flash attention are part of the
                # load config; see _maybe_load_lm_studio_model in gact/app.py.
                "parallel": 1,
                "flash_attention": True,
                "echo_load_config": True,
            },
            "timeout": 180,
        }
        assert app.state.lm_config["context_length"] == 32768
        owned = app.state.lm_studio_owned_instance
        assert owned["root"] == "http://127.0.0.1:1234"
        assert owned["instance_id"] == "qwopus3.5-9b-v3"
        assert owned["model"] == "qwopus3.5-9b-v3"
        assert owned["context_length"] == 32768
        assert owned["created_at"]


def test_put_lm_provider_reuses_loaded_lm_studio_model(tmp_path: Path, monkeypatch) -> None:
    """Do not call LM Studio load when the requested model/context is already active."""

    captured: dict[str, Any] = {"post_called": False}

    class _GetResp:
        status_code = 200
        text = ""

        def json(self) -> dict[str, Any]:
            return {
                "models": [
                    {
                        "key": "qwopus3.5-9b-v3",
                        "loaded_instances": [
                            {
                                "id": "qwopus3.5-9b-v3",
                                # reuse requires BOTH context AND parallel to match
                                # the load config we'd otherwise send (parallel default 1)
                                "config": {"context_length": 32768, "parallel": 1},
                            },
                        ],
                    },
                ],
            }

    class _StubAgent:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.arc = type(
                "ARC",
                (),
                {
                    "get_cache_stats": lambda self: {
                        "hits": 0,
                        "misses": 0,
                        "hit_rate": 0.0,
                        "capacity": 10,
                    }
                },
            )()

        def forward(self, *args: Any, **kwargs: Any) -> Any:
            return type("Pred", (), {"answer": "ok", "selected_expert": ""})()

    def _post(*args: Any, **kwargs: Any) -> None:
        captured["post_called"] = True
        raise AssertionError("LM Studio load endpoint should not be called")

    monkeypatch.setattr("requests.get", lambda *args, **kwargs: _GetResp())
    monkeypatch.setattr("requests.post", _post)
    monkeypatch.setattr("clio_agent.agent.ClioAgent", _StubAgent)
    monkeypatch.setattr("clio_agent.config.create_lm", lambda cfg: object())
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", lambda cfg: object())
    monkeypatch.setattr("clio_agent.config.create_planner_lm", lambda cfg: object())

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.put(
            "/v1/providers/lm",
            json={
                "provider": "lm_studio",
                "api_base": "http://127.0.0.1:1234/v1",
                "model": "qwopus3.5-9b-v3",
                "context_length": 32768,
            },
        )

    assert resp.status_code == 200, resp.text
    assert captured["post_called"] is False
    assert app.state.lm_config["context_length"] == 32768
    assert app.state.lm_studio_owned_instance is None


def test_lifespan_unloads_clio_owned_lm_studio_instance(tmp_path: Path, monkeypatch) -> None:
    """Shutdown cleanup must unload only the LM Studio instance CLIO owns."""

    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200
        text = ""

    def _post(url: str, *args: Any, **kwargs: Any) -> _Resp:
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["timeout"] = kwargs.get("timeout")
        return _Resp()

    monkeypatch.setattr("requests.post", _post)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app):
        app.state.lm_studio_owned_instance = {
            "root": "http://127.0.0.1:1234",
            "instance_id": "clio-owned-qwopus",
        }

    assert captured == {
        "url": "http://127.0.0.1:1234/api/v1/models/unload",
        "json": {"instance_id": "clio-owned-qwopus"},
        "timeout": 30,
    }
    assert app.state.lm_studio_owned_instance is None


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


def test_put_lm_provider_failed_first_connect_restores_env(tmp_path: Path, monkeypatch) -> None:
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


async def test_concurrent_lm_binds_serialize_on_lm_bind_lock(tmp_path: Path, monkeypatch) -> None:
    """Two concurrent binds for different providers must serialize on
    ``app.state.lm_bind_lock``.

    The snapshot -> mutate ``os.environ`` -> reconfigure dspy -> restore section
    is process-global; without one serialized owner two concurrent
    ``PUT /v1/providers/lm`` calls interleave it and leave a mixed final state.
    With the lock the critical section runs one bind at a time (a single winner)
    and the binds run directly on the serving loop (no nested-loop ``asyncio.run``,
    so no nested-loop ``RuntimeError``).
    """

    # These get stamped by _stamp_process_env via direct os.environ writes;
    # record them so monkeypatch restores the pre-test values on teardown.
    for key in ("CLIO_LM_PROVIDER", "CLIO_LM_API_BASE", "CLIO_LM_MODEL", "CLIO_LM_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    import clio_agent.providers.handshake as handshake_mod

    # Count how many binds sit inside the env-mutating critical section at once.
    # run_handshake is awaited from within _apply_lm_provider, so it is a fair
    # sampling point for "are two binds overlapping the critical section".
    overlap = {"active": 0, "max": 0}

    async def _tracking_handshake(ctx: Any, **kwargs: Any) -> Any:
        overlap["active"] += 1
        overlap["max"] = max(overlap["max"], overlap["active"])
        try:
            # Yield the loop: a second, unserialized bind would enter here too.
            await asyncio.sleep(0.05)
        finally:
            overlap["active"] -= 1
        # Skip the real handshake; the bind falls back to the static cfg.
        raise RuntimeError("handshake disabled in test")

    monkeypatch.setattr(handshake_mod, "run_handshake", _tracking_handshake)

    class _StubAgent:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.arc = type(
                "ARC",
                (),
                {
                    "get_cache_stats": lambda self: {
                        "hits": 0,
                        "misses": 0,
                        "hit_rate": 0.0,
                        "capacity": 10,
                    }
                },
            )()

        def forward(self, *args: Any, **kwargs: Any) -> Any:
            return type("Pred", (), {"answer": "ok", "selected_expert": ""})()

    monkeypatch.setattr("clio_agent.agent.ClioAgent", _StubAgent)
    monkeypatch.setattr(
        "clio_agent.config.create_lm", lambda cfg: type("FakeLM", (), {"history": []})()
    )
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", lambda cfg: object())
    monkeypatch.setattr("clio_agent.config.create_planner_lm", lambda cfg: object())

    app = build_app(sessions_path=tmp_path / "s.json")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r1, r2 = await asyncio.gather(
            client.put(
                "/v1/providers/lm",
                json={
                    "provider": "openai",
                    "api_base": "http://provider-a/v1",
                    "model": "model-a",
                    "api_key": "k",
                },
            ),
            client.put(
                "/v1/providers/lm",
                json={
                    "provider": "openrouter",
                    "api_base": "http://provider-b/v1",
                    "model": "model-b",
                    "api_key": "k",
                },
            ),
        )

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text

    # Serialized: the critical section never held two binds at once.
    assert overlap["max"] == 1, f"binds interleaved (max concurrency {overlap['max']})"

    # One winner: model + api_base in os.environ come from the SAME bind.
    winners = {
        "model-a": "http://provider-a/v1",
        "model-b": "http://provider-b/v1",
    }
    stamped_model = os.environ["CLIO_LM_MODEL"]
    assert stamped_model in winners
    assert os.environ["CLIO_LM_API_BASE"] == winners[stamped_model]
    assert app.state.lm_config["model"] == stamped_model
    assert app.state.lm_config["api_base"] == winners[stamped_model]

    # The serialization seam exists and is a plain asyncio.Lock (non-reentrant).
    assert isinstance(app.state.lm_bind_lock, asyncio.Lock)


def test_turn_timeout_precedence_runtime_over_conf() -> None:
    """The per-turn no-progress timeout is drivable on the LM-config channel.

    A runtime value (set via PUT /v1/providers/lm -> app.state.lm_config) wins so
    a client (e.g. the test harness) configures it on the SAME channel it
    configures the LM; absent/zero falls back to conf file -> env -> 900s default.
    Regression: the server-side watchdog used to be a disconnected launch-only env
    that silently disagreed with the client's no-progress setting.
    """
    import clio_agent.conf as conf
    from clio_agent.gact.app import _gact_turn_timeout_s

    os.environ.pop("CLIO_GACT_TURN_TIMEOUT_S", None)
    conf.reload()
    app = SimpleNamespace(state=SimpleNamespace())

    # No app / no runtime config -> default.
    assert _gact_turn_timeout_s(None) == 900.0
    assert _gact_turn_timeout_s(app) == 900.0

    # Runtime value (what the SUT PUT stores) wins.
    app.state.lm_config = {"turn_timeout_s": 1800.0}
    assert _gact_turn_timeout_s(app) == 1800.0

    # Runtime 0 -> fall through to env.
    app.state.lm_config = {"turn_timeout_s": 0}
    os.environ["CLIO_GACT_TURN_TIMEOUT_S"] = "1234"
    conf.reload()
    try:
        assert _gact_turn_timeout_s(app) == 1234.0
    finally:
        os.environ.pop("CLIO_GACT_TURN_TIMEOUT_S", None)
        conf.reload()
