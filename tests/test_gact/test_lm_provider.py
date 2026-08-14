"""GET / PUT /v1/providers/lm — TUI configures the LM at runtime
without redeploying the GACT process.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import requests
from fastapi.testclient import TestClient

from clio_agent.config import LMProviderConfig
from clio_agent.gact.app import build_app
from clio_agent.gact.providers.config import _effective_lm_config
from clio_agent.runtime.status import RuntimeProbe


def _patch_doctor(monkeypatch: Any, probe: RuntimeProbe) -> None:
    """Swap the unified doctor engine into /v1/health for a deterministic probe."""

    def _fake(
        *,
        api_state: Any = None,
        api_error: Any = None,
        env: Any = None,
        lm_timeout: float = 1.0,
        include_process_census: bool = True,
    ):
        return probe.collect(
            api_state=api_state, api_error=api_error, include_process_census=include_process_census
        )

    monkeypatch.setattr("clio_agent.gact.routes.system.collect_runtime_status", _fake)


class _RebindLMStub:
    """Mixin: a bound ``rebind_lms`` for bind-path ClioAgent stubs.

    The provider bind now calls ``agent.rebind_lms(cfg)`` (on a freshly built agent or
    a ``copy.copy`` of the existing one), so a stub standing in for ClioAgent needs a
    real bound method that rebuilds the four LM-surface fields together.
    """

    def rebind_lms(self, cfg: Any) -> None:
        self._provider_config = cfg
        self._main_lm = SimpleNamespace(
            model=getattr(cfg, "model", ""), provider=getattr(cfg, "provider", ""), history=[]
        )
        self._planner_lm = SimpleNamespace(
            model=getattr(cfg, "model", ""), provider=getattr(cfg, "provider", ""), history=[]
        )
        self._dspy_adapter = SimpleNamespace(provider=getattr(cfg, "provider", ""))


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
                    claude_code_transport="sdk",
                )
            ),
        )
    )

    cfg = _effective_lm_config(app)  # type: ignore[arg-type]

    assert cfg["provider"] == "claude_code"
    assert cfg["transport"] == "sdk"


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
    tmp_path: Path,
    monkeypatch: Any,
    floor_sandbox: Any,
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


def _patch_ambient_bind_network(monkeypatch) -> None:
    """Stub the two REAL, never-mocked network calls every ``/v1/providers/lm``
    bind reaches on its way through ``_apply_lm_provider`` (#1211 review B3).

    Neither is exercised by these tests' own assertions, and neither was
    previously mocked here (only the LM Studio-specific ``requests.get``/
    ``requests.post`` load calls were):

    * ``run_handshake`` -- dispatches to the per-provider connectivity probe
      (``LMStudioHandshake``, for the lm_studio tests here) via an INJECTED
      async http client, not the ``requests`` module the tests already mock.
      A live probe against ``127.0.0.1:1234`` with nothing listening blocks on
      a real OS-level connect timeout rather than failing instantly, and its
      observed cost varied 0.3s-35s across repeated live measurement on this
      box. Stubbed to raise, which ``_apply_lm_provider``'s existing
      ``except Exception: handshake_report = None`` already handles as a
      graceful degrade (matching a genuine unreachable-backend outcome).
    * ``relay_agent_kwargs`` -- the first-bind path (``app.state.agent is
      None``) calls ``construct_agent_with_relay``, which discovers this
      environment's configured relay/MCP-federation catalog over a real
      connection before constructing the (already-stubbed) agent; measured at
      a consistent ~2.4s whenever that endpoint didn't answer immediately.

    Combined, these two REAL dependencies (never touched by #1211 -- verified
    byte-identical against the branch's merge-base) can legitimately exceed
    ``_wait_lm_provider_ready``'s 5s deadline under real ambient conditions
    that have nothing to do with the code under test; stubbing them here is
    the root fix (hermetic isolation), not a timeout bump.
    """

    async def _unreachable_handshake(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise RuntimeError("handshake stubbed for hermetic test (no live backend)")

    async def _no_relay_kwargs(app):  # noqa: ANN001
        return {}

    monkeypatch.setattr("clio_agent.providers.handshake.run_handshake", _unreachable_handshake)
    monkeypatch.setattr("clio_agent.gact.relay_wiring.relay_agent_kwargs", _no_relay_kwargs)


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
    # "fable" is the CLI's own current default alias (#1211 review D4) -- listed
    # alongside the other documented aliases so a fresh install already shows it.
    assert {row["id"] for row in claude["models"]} == {"fable", "sonnet", "opus", "haiku"}


def test_provider_list_default_model_follows_overlay_once_refreshed(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """#1211 review D2 failing-first: the picker row's default_model follows the
    overlay's discovered default (once a refresh has run), not the stale static
    ``suggested_model`` the account may already reject (#1184)."""
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    from clio_agent.providers import model_discovery

    model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="codex",
            discovered=[{"id": "gpt-5.6-sol", "name": "Sol", "description": ""}],
            source=model_discovery.CODEX_SOURCE,
            default_model="gpt-5.6-sol",
        )
    )
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        rows = c.get("/v1/providers").json()["providers"]
        detail = c.get("/v1/providers/codex").json()
    codex_row = next(r for r in rows if r["id"] == "codex")
    assert codex_row["default_model"] == "gpt-5.6-sol"
    assert detail["default_model"] == "gpt-5.6-sol"


def test_provider_list_default_model_falls_back_to_static_without_overlay(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        rows = c.get("/v1/providers").json()["providers"]
    codex_row = next(r for r in rows if r["id"] == "codex")
    assert codex_row["default_model"] == "gpt-5.5"  # the frozen static suggested_model


def test_provider_list_default_model_claude_code_follows_cost_policy_not_cli_default(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Owner ruling 2026-08-14 (failing-first): the CLI's own bare default
    resolves to the premium ``fable`` tier, but clio must never silently
    default a user onto it -- the SERVED picker default for claude_code stays
    ``sonnet`` (a deliberate cost policy) even once a refresh has discovered
    fable as the CLI's live choice. Codex is unaffected: its own overlay
    default still wins verbatim (populated in the same test as the twin)."""
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    from clio_agent.providers import model_discovery

    model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="claude_code",
            discovered=[
                {"id": "fable", "name": "Fable", "description": ""},
                {"id": "sonnet", "name": "Sonnet", "description": ""},
                {"id": "opus", "name": "Opus", "description": ""},
            ],
            source=model_discovery.CLAUDE_CODE_SOURCE,
            default_model="fable",  # the CLI's own bare-default choice
        )
    )
    # Codex unaffected twin: its own overlay default is untouched by the
    # claude_code-only cost policy.
    model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="codex",
            discovered=[{"id": "gpt-5.6-sol", "name": "Sol", "description": ""}],
            source=model_discovery.CODEX_SOURCE,
            default_model="gpt-5.6-sol",
        )
    )
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        rows = c.get("/v1/providers").json()["providers"]
        claude_detail = c.get("/v1/providers/claude_code").json()
        codex_detail = c.get("/v1/providers/codex").json()
    claude_row = next(r for r in rows if r["id"] == "claude_code")
    codex_row = next(r for r in rows if r["id"] == "codex")
    assert claude_row["default_model"] == "sonnet"
    assert claude_detail["default_model"] == "sonnet"
    assert codex_row["default_model"] == "gpt-5.6-sol"
    assert codex_detail["default_model"] == "gpt-5.6-sol"
    # The overlay-diagnostic route surfaces the honest CLI choice alongside
    # the policy default, never silently dropping it.
    models_resp = c.get("/v1/providers/claude_code/models").json()
    assert models_resp["default_model"] == "sonnet"
    assert models_resp["cli_default"] == "fable"


def test_put_lm_provider_omitted_model_claude_code_binds_sonnet_cost_policy_default(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Owner ruling 2026-08-14 (failing-first): an omitted-model claude_code
    bind must resolve to the cost-policy default (sonnet), never the CLI's own
    premium bare default (fable) even once a refresh has recorded fable as the
    live CLI choice."""
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    from clio_agent.providers import model_discovery

    model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="claude_code",
            discovered=[
                {"id": "fable", "name": "Fable", "description": ""},
                {"id": "sonnet", "name": "Sonnet", "description": ""},
            ],
            source=model_discovery.CLAUDE_CODE_SOURCE,
            default_model="fable",
        )
    )

    captured: dict[str, Any] = {}

    class _StubAgent(_RebindLMStub):
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

    def _stub_create_lm(cfg: Any) -> Any:
        captured["cfg"] = cfg
        return type("FakeLM", (), {"history": []})()

    monkeypatch.setattr("clio_agent.agent.ClioAgent", _StubAgent)
    monkeypatch.setattr("clio_agent.config.create_lm", _stub_create_lm)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.put(
            "/v1/providers/lm",
            json={"provider": "claude_code", "api_base": "claude-code://sdk", "model": ""},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["model"] == "sonnet"
    assert captured["cfg"].model == "sonnet"


def test_put_lm_provider_omitted_model_binds_the_overlay_default(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """#1211 review D2 failing-first: an omitted ``model`` on a PUT bind resolves
    through the overlay's discovered default once a refresh has run, not the
    stale static ``suggested_model`` (#1184's rejected pins)."""
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    from clio_agent.providers import model_discovery

    model_discovery.record_refresh(
        model_discovery.ProviderDiscoveryResult(
            provider="codex",
            discovered=[{"id": "gpt-5.6-sol", "name": "Sol", "description": ""}],
            source=model_discovery.CODEX_SOURCE,
            default_model="gpt-5.6-sol",
        )
    )

    captured: dict[str, Any] = {}

    class _StubAgent(_RebindLMStub):
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

    def _stub_create_lm(cfg: Any) -> Any:
        captured["cfg"] = cfg
        return type("FakeLM", (), {"history": []})()

    monkeypatch.setattr("clio_agent.agent.ClioAgent", _StubAgent)
    monkeypatch.setattr("clio_agent.config.create_lm", _stub_create_lm)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.put(
            "/v1/providers/lm",
            json={"provider": "codex", "api_base": "codex://app-server", "model": ""},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["model"] == "gpt-5.6-sol"
    assert captured["cfg"].model == "gpt-5.6-sol"


def test_provider_model_catalog_unknown_provider_404(tmp_path: Path) -> None:
    """An unknown provider id is a clean 404."""
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.get("/v1/providers/totally-unknown/models")
    assert resp.status_code == 404


def test_health_lm_row_is_unified_probe_lm_provider(tmp_path: Path, monkeypatch) -> None:
    """#800: /v1/health's LM row is the unified doctor's ``lm_provider`` (from the
    probe engine), not an app.state-derived ``lm`` row. An unreachable local
    provider surfaces as an unavailable ``lm_provider`` row → 503."""

    def _refused(*a: Any, **k: Any):
        raise requests.ConnectionError("connection refused")

    probe = RuntimeProbe(
        env={"CLIO_ARC_STORE": "local", "CLIO_DATA_DIR": str(tmp_path)},
        http_get=_refused,
        gateway_lister=lambda: [{"name": "hdf5_x"}],
        module_checker=lambda name: True,
        port_checker=lambda port: False,
        clio_runtime_dir=tmp_path / "clio-home",
    )
    _patch_doctor(monkeypatch, probe)
    resp = TestClient(build_app(sessions_path=tmp_path / "s.json")).get("/v1/health")
    rows = {r["name"]: r for r in resp.json()["integrations"]}
    assert "lm" not in rows  # old hand-rolled row is gone
    assert rows["lm_provider"]["status"] == "unavailable"
    assert resp.status_code == 503


def test_get_lm_provider_when_configured_from_boot_agent(tmp_path: Path, monkeypatch) -> None:
    """Env-booted agents should expose their effective provider config.

    ``build_app`` unconditionally seeds ``app.state.provider_profiles`` from
    ``load_config_from_env()`` (the file/env-resolved BOOT config), regardless
    of the ``agent=`` object passed in here -- so this test is NOT hermetic
    against a developer's real ``.env`` (``conf``'s file/env layer is process-
    ambient, not tmp_path-scoped). Delete the knobs that boot config reads so
    the seeded default profile is deterministically the lm_studio/no-transport
    default this test's ``assert body["transport"] is None`` actually pins,
    instead of silently depending on nobody's ``.env`` naming codex/claude_code.
    """
    for env_var in (
        "CLIO_LM_PROVIDER",
        "CLIO_CODEX_TRANSPORT",
        "CLIO_CLAUDE_CODE_TRANSPORT",
    ):
        monkeypatch.delenv(env_var, raising=False)

    agent = SimpleNamespace(
        _provider_config=SimpleNamespace(
            provider="lm_studio",
            api_base="http://127.0.0.1:1234/v1",
            model="qwopus3.5-9b-v3",
            temperature=0.0,
            max_tokens=4096,
            context_length=32768,
            thinking_budget=0,
            codex_transport="app_server",
        )
    )
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        body = c.get("/v1/providers/lm").json()

    # #800: /v1/health no longer derives its LM row from the boot agent's
    # _provider_config — the unified doctor probes the real runtime. This test
    # now only pins the GET /v1/providers/lm surface (the LM-config source of
    # truth); the health lm_provider row is covered in test_doctor_integrations.
    assert body["configured"] is True
    assert body["provider"] == "lm_studio"
    assert body["api_base"] == "http://127.0.0.1:1234/v1"
    assert body["model"] == "qwopus3.5-9b-v3"
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 4096
    assert body["context_length"] == 32768
    assert body["transport"] is None


def test_health_surfaces_argonne_token_missing_via_probe(tmp_path: Path, monkeypatch) -> None:
    """#800: the unified doctor surfaces an ALCF provider with no stored Globus
    token as a misconfigured ``lm_provider`` row (degraded chip) — the token
    check now lives in the probe engine, not the health handler."""

    monkeypatch.setattr("clio_agent.providers.argonne_auth.tokens_exist", lambda: False)

    probe = RuntimeProbe(
        env={
            "CLIO_LM_PROVIDER": "argonne",
            "CLIO_ARC_STORE": "local",
            "CLIO_DATA_DIR": str(tmp_path),
        },
        gateway_lister=lambda: [{"name": "hdf5_x"}],
        module_checker=lambda name: True,  # globus_sdk importable
        port_checker=lambda port: False,
        clio_runtime_dir=tmp_path / "clio-home",
    )
    _patch_doctor(monkeypatch, probe)
    resp = TestClient(build_app(sessions_path=tmp_path / "s.json")).get("/v1/health")

    body = resp.json()
    rows = {r["name"]: r for r in body["integrations"]}
    assert rows["lm_provider"]["status"] == "degraded"  # misconfigured -> degraded chip
    assert "Globus tokens" in rows["lm_provider"]["summary"]
    assert body["overall_status"] == "degraded"


def test_get_lm_provider_when_configured_via_put(tmp_path: Path, monkeypatch) -> None:
    """Stub out ClioAgent + create_lm so we can exercise the PUT
    code path without an LM endpoint."""

    fake_agent_constructed: dict[str, Any] = {}

    class _StubAgent(_RebindLMStub):
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
        # #800: /v1/health no longer mirrors the PUT'd LM config — the unified
        # doctor probes the real runtime (and folds a cached handshake into the
        # lm_provider row). That surface is covered in test_doctor_integrations.


def test_put_argonne_uses_provider_default_max_tokens_when_omitted(
    tmp_path: Path, monkeypatch
) -> None:
    """TUI default save should not force the global 32k cap onto ALCF."""

    captured: dict[str, Any] = {}

    class _StubAgent(_RebindLMStub):
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
    _patch_ambient_bind_network(monkeypatch)

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

    class _StubAgent(_RebindLMStub):
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
    _patch_ambient_bind_network(monkeypatch)

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

    class _StubAgent(_RebindLMStub):
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
    _patch_ambient_bind_network(monkeypatch)

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
    agent = SimpleNamespace(
        _provider_config=SimpleNamespace(provider="argonne", api_key="old-token"),
        _main_lm=main_lm,
        _planner_lm=planner_lm,
    )

    _refresh_argonne_lm_token(agent)

    assert agent._provider_config.api_key == "runtime-token"
    assert main_lm.kwargs["api_key"] == "runtime-token"
    assert planner_lm.kwargs["api_key"] == "runtime-token"


def test_put_lm_provider_rejects_removed_codex_transport(tmp_path: Path, monkeypatch) -> None:
    """v0.8.0: a codex bind naming a deleted transport 400s typed; the default
    bind (no transport) lands on app_server — the only transport."""
    captured: dict[str, Any] = {}
    monkeypatch.delenv("CLIO_CODEX_TRANSPORT", raising=False)

    class _StubAgent(_RebindLMStub):
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
        rejected = c.put(
            "/v1/providers/lm",
            json={
                "provider": "codex",
                "api_base": "codex://app-server",
                "model": "gpt-5.5",
                "transport": "sdk",
            },
        )
        assert rejected.status_code == 400, rejected.text
        assert "removed in the v0.8.0 cleanup" in rejected.json()["error"]["message"]

        resp = c.put(
            "/v1/providers/lm",
            json={
                "provider": "codex",
                "api_base": "codex://app-server",
                "model": "gpt-5.5",
            },
        )
        body = resp.json()
        get_body = c.get("/v1/providers/lm").json()

    assert resp.status_code == 200, resp.text
    assert body["transport"] == "app_server"
    assert get_body["transport"] == "app_server"
    assert captured["cfg"].provider == "codex"
    assert captured["cfg"].api_key == "x"
    assert captured["cfg"].codex_transport == "app_server"
    assert app.state.lm_config["transport"] == "app_server"
    # Demoted bind (design §5): transport travels on the config / store default,
    # NOT process-global env. The bind must not stamp CLIO_CODEX_TRANSPORT.
    assert "CLIO_CODEX_TRANSPORT" not in os.environ
    assert app.state.provider_profiles.default.transport == "app_server"


def test_put_lm_provider_rejects_removed_claude_code_transport(tmp_path: Path, monkeypatch) -> None:
    """v0.8.0: a claude_code bind naming the deleted exec transport 400s typed;
    an explicit sdk transport still applies to claude_code_transport."""
    monkeypatch.delenv("CLIO_CLAUDE_CODE_TRANSPORT", raising=False)
    captured: dict[str, Any] = {}

    class _StubAgent(_RebindLMStub):
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
        rejected = c.put(
            "/v1/providers/lm",
            json={
                "provider": "claude_code",
                "api_base": "claude-code://sdk",
                "model": "haiku",
                "transport": "exec",
            },
        )
        assert rejected.status_code == 400, rejected.text
        assert "removed in the v0.8.0 cleanup" in rejected.json()["error"]["message"]

        resp = c.put(
            "/v1/providers/lm",
            json={
                "provider": "claude_code",
                "api_base": "claude-code://sdk",
                "model": "haiku",
                "transport": "sdk",
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
    # Demoted bind (design §5): no process-global env stamping.
    assert "CLIO_CLAUDE_CODE_TRANSPORT" not in os.environ
    assert app.state.provider_profiles.default.transport == "sdk"


def test_put_lm_provider_defaults_claude_code_to_sdk_transport(tmp_path: Path, monkeypatch) -> None:
    """Claude Code should use the streaming-capable SDK path unless exec is explicit."""
    monkeypatch.delenv("CLIO_CLAUDE_CODE_TRANSPORT", raising=False)
    captured: dict[str, Any] = {}

    class _StubAgent(_RebindLMStub):
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
    # Demoted bind (design §5): no process-global env stamping.
    assert "CLIO_CLAUDE_CODE_TRANSPORT" not in os.environ
    assert app.state.provider_profiles.default.transport == "sdk"


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

    class _StubAgent(_RebindLMStub):
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
    _patch_ambient_bind_network(monkeypatch)

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

    class _StubAgent(_RebindLMStub):
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
    _patch_ambient_bind_network(monkeypatch)

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
    """A rejected provider swap must not touch process-global env.

    The demoted bind (design §5) no longer stamps ``os.environ`` at all, so a
    failed connect leaves the pre-existing env untouched by construction — there
    is nothing to leak and nothing to restore.
    """

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


# ---- demoted default-only bind (design §5 / §9 step 8) --------------------


def _make_stub_agent_cls() -> type:
    """A minimal ClioAgent stub whose construction needs no live LM.

    Only the ``arc`` surface the bind path touches is provided; the bind rebinds
    ``_provider_config`` / ``_main_lm`` / ``_planner_lm`` / ``_dspy_adapter`` onto the
    instance via ``rebind_lms``.
    """

    class _StubAgent(_RebindLMStub):
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

    return _StubAgent


def _stub_lm_bind(monkeypatch) -> None:
    """Stub the LM factories + ClioAgent + handshake so a PUT needs no network."""

    monkeypatch.setattr("clio_agent.agent.ClioAgent", _make_stub_agent_cls())
    monkeypatch.setattr(
        "clio_agent.config.create_lm", lambda cfg: type("LM", (), {"history": []})()
    )
    monkeypatch.setattr("clio_agent.config.create_chat_adapter", lambda cfg: object())
    monkeypatch.setattr("clio_agent.config.create_planner_lm", lambda cfg: object())

    async def _no_handshake(ctx: Any, **kwargs: Any) -> Any:
        # No network from a unit test; the bind catches this and keeps the static
        # PROVIDER_DEFAULTS caps (the demoted bind never blocks on a handshake).
        raise RuntimeError("handshake disabled in test")

    monkeypatch.setattr("clio_agent.providers.handshake.run_handshake", _no_handshake)


_REMOVED_BIND_ENV_KEYS = (
    "CLIO_LM_PROVIDER",
    "CLIO_LM_API_BASE",
    "CLIO_LM_MODEL",
    "CLIO_LM_API_KEY",
    "CLIO_CODEX_TRANSPORT",
    "CLIO_CLAUDE_CODE_TRANSPORT",
)


def test_concurrent_default_swaps_yield_one_consistent_snapshot(tmp_path: Path) -> None:
    """Concurrent default-profile swaps converge on ONE internally-consistent
    default snapshot — never a torn multi-key mix.

    This is the concurrency-safety proof that replaces the reverted
    ``lm_bind_lock``'s failing-first test (design §1/§5/§9 step 8). It hammers the
    exact critical operation the demoted ``_apply_lm_provider`` performs — the RCU
    pointer swap ``app.state.provider_profiles =
    app.state.provider_profiles.with_default(spec)`` — from many threads racing on
    a barrier, against the real per-app store on ``app.state``.

    Because ``with_default`` builds a whole new immutable snapshot and the
    assignment is a single atomic pointer store under the GIL, the losing writer is
    fully overwritten (last-writer-wins): the surviving default's ``provider`` /
    ``model`` / ``api_base`` all belong to the SAME provider, never a half-A/half-B
    mix. This is precisely the guarantee the reverted ``app.state`` lock could not
    give (it guarded the *wrong-scoped* process-global ``os.environ`` +
    ``main_thread_config``); here there is no shared mutable global left to tear, so
    no lock is needed. We also assert the swap touches no process-global env.

    (Driven at the swap level rather than via two concurrent HTTP PUTs because
    neither ``TestClient`` nor ``httpx.ASGITransport`` can service two concurrent
    requests without interleaving their *bodies* — that tears the request, not the
    store. The store swap here is the route's only shared-mutable-state write; the
    resolved config/spec are per-call locals that never cross threads.)
    """
    import threading

    from clio_agent.gact.providers.profile_store import ProviderProfileStore
    from clio_agent.providers.lm_spec import LMSpec

    for key in _REMOVED_BIND_ENV_KEYS:
        os.environ.pop(key, None)
    env_before = dict(os.environ)

    app = build_app(sessions_path=tmp_path / "s.json")

    # One distinct whole spec per provider (the same immutable ``LMSpec`` shape
    # ``spec_from_config`` yields in ``_apply_lm_provider``), each carrying a
    # consistent provider/model/api_base triple.
    specs = [
        LMSpec(provider=f"prov{i}", model=f"model-{i}", api_base=f"http://{i}.example/v1")
        for i in range(12)
    ]
    expected = {spec.provider: (spec.api_base, spec.model) for spec in specs}

    barrier = threading.Barrier(len(specs))

    def _swap(spec: LMSpec) -> None:
        barrier.wait()  # maximise the race window
        for _ in range(300):
            store = app.state.provider_profiles
            app.state.provider_profiles = store.with_default(spec)

    threads = [threading.Thread(target=_swap, args=(spec,)) for spec in specs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    store = app.state.provider_profiles
    assert isinstance(store, ProviderProfileStore)
    default = store.default
    # The surviving default is ONE whole provider's spec, never a torn mix.
    assert default.provider in expected, default
    assert (default.api_base, default.model) == expected[default.provider]

    # The RCU swap never touched process-global env.
    assert dict(os.environ) == env_before
    for key in _REMOVED_BIND_ENV_KEYS:
        assert key not in os.environ, key


def test_single_provider_bind_reports_ready_from_store_default(tmp_path: Path, monkeypatch) -> None:
    """Backward-compat: a single-provider bind + GET + /wait still report 'ready'
    with the bound provider, and the read side reports the store default profile.
    """
    _stub_lm_bind(monkeypatch)
    for key in _REMOVED_BIND_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.put(
            "/v1/providers/lm",
            json={
                "provider": "openai",
                "api_base": "http://single.example/v1",
                "model": "the-model",
                "api_key": "key",
            },
        )
        assert resp.status_code == 200, resp.text

        info = c.get("/v1/providers/lm").json()
        assert info["configured"] is True
        assert info["provider"] == "openai"
        assert info["model"] == "the-model"
        assert info["api_base"] == "http://single.example/v1"

        waited = c.get("/v1/providers/lm/wait").json()
        assert waited["state"] == "ready"
        assert waited["provider"] == "openai"
        assert waited["model"] == "the-model"

    # The read side reports the default profile straight off the per-app store.
    default = app.state.provider_profiles.default
    assert default.provider == "openai"
    assert default.model == "the-model"
    assert default.api_base == "http://single.example/v1"

    # And no env was stamped by the demoted bind.
    for key in _REMOVED_BIND_ENV_KEYS:
        assert key not in os.environ, key


# ---- thinking-level wire (#895) -------------------------------------------


def test_put_lm_provider_accepts_thinking_level(tmp_path: Path, monkeypatch) -> None:
    """A valid thinking_level binds onto the config and round-trips on the GET (#895).

    The provider-generic level is threaded into the built ``LMProviderConfig`` and
    echoed on both the PUT result and a subsequent GET, alongside the resolved
    ``thinking_effective`` (per-provider effect) so the picker can display it.
    """

    captured: dict[str, Any] = {}

    class _StubAgent(_RebindLMStub):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.arc = type(
                "ARC",
                (),
                {"get_cache_stats": lambda self: {"hits": 0, "misses": 0, "hit_rate": 0.0}},
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
                "provider": "openai",
                "api_base": "http://127.0.0.1:3456/v1",
                "model": "gpt-5.5",
                "api_key": "x",
                "thinking_level": "medium",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        get_body = c.get("/v1/providers/lm").json()

    # PUT result echoes the level + the resolved effort effect.
    assert body["thinking_level"] == "medium"
    assert "medium" in body["thinking_effective"]
    # GET reports the same (never invisible).
    assert get_body["thinking_level"] == "medium"
    assert "medium" in get_body["thinking_effective"]
    # The built config carried the level, and it was bound onto lm_config.
    assert captured["cfg"].thinking_level == "medium"
    assert app.state.lm_config["thinking_level"] == "medium"


def test_put_lm_provider_rejects_invalid_thinking_level(tmp_path: Path) -> None:
    """A junk thinking_level is a structured 422 at the boundary — never ignored (#895).

    The ``Literal["off","low","medium","high"]`` on ``LMProviderRequest`` makes
    FastAPI reject an out-of-vocabulary level before the handler runs, so an
    unsupported value can never be silently dropped or bound.
    """

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        resp = c.put(
            "/v1/providers/lm",
            json={
                "provider": "openai",
                "api_base": "http://127.0.0.1:3456/v1",
                "model": "gpt-5.5",
                "api_key": "x",
                "thinking_level": "ultra",
            },
        )

    assert resp.status_code == 422, resp.text
    # The app wraps validation errors in a structured ErrorEnvelope; it must name
    # the offending field and the allowed vocabulary — never a silent drop.
    body = resp.json()
    assert body["error"]["error"] == "validation_error"
    errors = body["error"]["details"]["errors"]
    assert any("thinking_level" in str(item.get("loc")) for item in errors)


def test_effective_lm_config_surfaces_supported_thinking_effective() -> None:
    """A budget provider reports the raw level and the resolved budget effect (#895)."""

    app = SimpleNamespace(
        state=SimpleNamespace(
            lm_config={},
            agent=SimpleNamespace(
                _provider_config=SimpleNamespace(
                    provider="anthropic", thinking_level="high", thinking_budget=0
                )
            ),
        )
    )

    cfg = _effective_lm_config(app)  # type: ignore[arg-type]

    assert cfg["thinking_level"] == "high"
    # anthropic maps 'high' → budget_tokens 24576 (providers.thinking.LEVEL_BUDGET).
    assert cfg["thinking_effective"] == "high (budget 24576)"


def test_effective_lm_config_surfaces_unsupported_thinking() -> None:
    """A provider with no mapping surfaces a typed ``unsupported`` — no silent drop (#895).

    This is the GET-report side of the no-silent-fallback rule: a requested level
    on a provider ``providers.thinking`` cannot map still reaches the API as a
    structured ``unsupported (...)`` display instead of vanishing.
    """

    app = SimpleNamespace(
        state=SimpleNamespace(
            lm_config={},
            agent=SimpleNamespace(
                _provider_config=SimpleNamespace(
                    provider="mystery-transport", thinking_level="high", thinking_budget=0
                )
            ),
        )
    )

    cfg = _effective_lm_config(app)  # type: ignore[arg-type]

    assert cfg["thinking_level"] == "high"
    assert cfg["thinking_effective"].startswith("unsupported")
    assert "mystery-transport" in cfg["thinking_effective"]
