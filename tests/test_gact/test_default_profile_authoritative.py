"""ProviderProfileStore is the authoritative default source (design §9 step 9).

Step 9 flips per-expert resolution ON for every path by making the per-app
:class:`ProviderProfileStore` the ONE authoritative provider identity:

* ``ClioAgent`` binds ``_main_lm`` / ``_planner_lm`` / ``_dspy_adapter`` off an
  **injected** default-profile config (the gact server resolves it off the store)
  rather than reading the environment a SECOND time — the dropped boot
  env-handoff. ``provider_config=None`` still reads ``load_config_from_env`` (the
  standalone CLI / test baseline, unchanged).
* The boot ``_construct_agent_async`` hands the ONE boot config to ``ClioAgent``
  and reseeds the store's default from the agent's FINAL resolved config, so the
  store's default profile and ``ClioAgent._main_lm`` are the SAME identity.
* An expert declaring no provider inherits the STORE default (not the base
  agent's ``_provider_config``); two experts on two providers resolve to two
  distinct ``dspy.LM`` objects with no cross-talk.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest

import clio_agent.config as config_mod
from clio_agent.config import PROVIDER_DEFAULTS, LMProviderConfig
from clio_agent.gact import context as _ctx
from clio_agent.gact.agents.builders import _build_prompt_user_agent_module
from clio_agent.gact.app import build_app
from clio_agent.gact.providers.profile_store import ProviderProfileStore
from clio_agent.gact.types import AgentDef
from clio_agent.providers import resolver as resolver_mod
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    HandshakeReport,
    ModelProfile,
)
from clio_agent.providers.lm_spec import LMSpec, spec_from_config

_BOOT_ENV_KEYS = (
    "CLIO_LM_PROVIDER",
    "CLIO_LM_API_BASE",
    "CLIO_LM_MODEL",
    "CLIO_LM_API_KEY",
)


def _report(*, provider: str, model_id: str, context_window: int | None = None) -> HandshakeReport:
    """Build a one-model OK :class:`HandshakeReport`."""
    return HandshakeReport(
        provider_id=provider,
        provider_kind=provider,
        connectivity=ConnectivityState.OK,
        auth=AuthState.OK,
        models=(ModelProfile(id=model_id, context_window=context_window),),
    )


# --------------------------------------------------------------------------- #
# ClioAgent binds the injected config (env-handoff dropped) + baseline fallback
# --------------------------------------------------------------------------- #


def test_clio_agent_binds_injected_provider_config_over_env(monkeypatch, tmp_path) -> None:
    """The injected default-profile config drives ``_provider_config`` / ``_main_lm``.

    The environment names a DIFFERENT model; the agent must ignore it and bind the
    injected config verbatim — proving ClioAgent no longer performs its own second
    ``load_config_from_env`` read (the dropped boot env-handoff, design §9 step 9).
    """
    from clio_agent.agent import ClioAgent  # noqa: PLC0415

    # Env points at a DIFFERENT model than the injected default profile.
    monkeypatch.setenv("CLIO_LM_PROVIDER", "openai")
    monkeypatch.setenv("CLIO_LM_MODEL", "env-model-should-be-ignored")
    monkeypatch.setenv("CLIO_LM_API_KEY", "sk-env")

    injected = LMProviderConfig(
        provider="openai",
        model="injected-default-model",
        api_base="http://injected.example/v1",
        api_key="sk-injected",
    )
    agent = ClioAgent(verbose=False, data_dir=str(tmp_path / "clio"), provider_config=injected)
    try:
        assert agent._provider_config is injected
        assert agent._provider_config.model == "injected-default-model"
        # The main LM was built off the injected config, not the env model.
        assert "injected-default-model" in agent._main_lm.model
        assert "env-model-should-be-ignored" not in agent._main_lm.model
    finally:
        agent.shutdown()


def test_clio_agent_falls_back_to_env_when_no_config(monkeypatch, tmp_path) -> None:
    """``provider_config=None`` reads ``load_config_from_env`` — the CLI baseline.

    This is the byte-identical standalone path: no injected config means the agent
    resolves its provider identity straight off the environment, exactly as before.
    """
    from clio_agent.agent import ClioAgent  # noqa: PLC0415
    from tests._config_layer import delete_config

    # ``lm.model`` is file-pinned by the autouse fixture (file > env); drop it so the
    # env model is the sole source this env-fallback contract resolves (#985 residual).
    delete_config("lm.model")
    monkeypatch.setenv("CLIO_LM_PROVIDER", "openai")
    monkeypatch.setenv("CLIO_LM_MODEL", "env-only-model")
    monkeypatch.setenv("CLIO_LM_API_KEY", "sk-env")

    agent = ClioAgent(verbose=False, data_dir=str(tmp_path / "clio"))
    try:
        assert agent._provider_config.provider == "openai"
        assert agent._provider_config.model == "env-only-model"
        assert "env-only-model" in agent._main_lm.model
    finally:
        agent.shutdown()


# --------------------------------------------------------------------------- #
# Boot: the store default drives ClioAgent._main_lm (env read ONCE)
# --------------------------------------------------------------------------- #


def test_boot_passes_one_config_and_store_default_drives_main_lm(monkeypatch, tmp_path) -> None:
    """``_construct_agent_async`` reads env ONCE, hands it to ClioAgent, and the
    store's default profile ends up the SAME identity as ``ClioAgent._main_lm``.

    A recording stub captures the ``provider_config`` the boot path injects and the
    LM it builds from it. The assertions prove (1) the boot passes exactly the boot
    config (one env read, no second handoff) and (2) the store's default profile
    equals ``spec_from_config`` of the config the main LM is built from — i.e. the
    default profile drives ``_main_lm`` when no per-expert override is declared.
    """
    import dspy  # noqa: PLC0415

    from clio_agent.gact.app import _construct_agent_async  # noqa: PLC0415

    # The boot ``dspy.configure`` (the harmless ambient fallback) runs in the
    # executor thread; dspy forbids reconfiguring its global settings from a
    # thread other than the one that first configured it, and an earlier test in
    # the suite may have configured it on the main thread. That ambient default is
    # not what this test exercises (the store + injected config are), so neutralize
    # it to keep the assertion about the authoritative store deterministic.
    monkeypatch.setattr(dspy, "configure", lambda **_kwargs: None)

    from tests._config_layer import delete_config

    for key in _BOOT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # ``lm.model`` is file-pinned by the autouse fixture (file > env); drop it so the
    # boot env read resolves the env model, not the fixture default (#985 residual).
    delete_config("lm.model")
    monkeypatch.setenv("CLIO_LM_PROVIDER", "openai")
    monkeypatch.setenv("CLIO_LM_MODEL", "boot-authoritative-model")
    monkeypatch.setenv("CLIO_LM_API_BASE", "http://boot.example/v1")
    monkeypatch.setenv("CLIO_LM_API_KEY", "sk-boot")

    recorded: dict[str, Any] = {}

    class _RecordingAgent:
        def __init__(
            self,
            *args: Any,
            arc: Any = None,
            provider_config: LMProviderConfig | None = None,
            **kwargs: Any,
        ) -> None:
            assert provider_config is not None, "boot must inject the default-profile config"
            recorded["provider_config"] = provider_config
            self.arc = arc
            self._provider_config = provider_config
            # Bind the main LM off the injected config, exactly like the real agent.
            self._main_lm = config_mod.create_lm(provider_config)

    monkeypatch.setattr("clio_agent.agent.ClioAgent", _RecordingAgent)

    app = build_app(sessions_path=tmp_path / "s.json")
    asyncio.run(_construct_agent_async(app))

    agent = app.state.agent
    assert isinstance(agent, _RecordingAgent)

    # (1) The boot injected exactly the one boot config (single env read).
    injected = recorded["provider_config"]
    assert injected.provider == "openai"
    assert injected.model == "boot-authoritative-model"
    assert injected.api_base == "http://boot.example/v1"

    # (2) The store's default profile is the SAME identity as the config the main
    # LM was built from — the default profile drives ClioAgent._main_lm.
    store = app.state.provider_profiles
    assert isinstance(store, ProviderProfileStore)
    assert store.default == spec_from_config(agent._provider_config)
    assert store.default.provider == "openai"
    assert store.default.model == "boot-authoritative-model"
    assert "boot-authoritative-model" in agent._main_lm.model


def test_boot_seed_still_matches_env_single_provider(monkeypatch, tmp_path) -> None:
    """GACT single-provider operation unchanged: build_app seeds default from env.

    Regression guard on the step-9 app.py change — the boot seed must still equal
    ``spec_from_config(load_config_from_env())`` for the single-default-LM case.
    """
    from clio_agent.config import load_config_from_env  # noqa: PLC0415

    for key in _BOOT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CLIO_LM_PROVIDER", "openai")
    monkeypatch.setenv("CLIO_LM_MODEL", "single-model")
    monkeypatch.setenv("CLIO_LM_API_KEY", "sk-single")

    app = build_app(sessions_path=tmp_path / "s.json")
    store = app.state.provider_profiles
    assert isinstance(store, ProviderProfileStore)
    assert store.default == spec_from_config(load_config_from_env())
    assert store.ids() == ("default",)


# --------------------------------------------------------------------------- #
# Experts resolve off the STORE default (not base_agent._provider_config)
# --------------------------------------------------------------------------- #


@pytest.mark.concurrency
def test_two_experts_two_providers_resolve_off_store_default(monkeypatch, tmp_path) -> None:
    """An undeclared expert inherits the STORE default; two experts → two LMs.

    With an ACTIVE app whose store default differs from the base agent's
    ``_provider_config``, an expert that declares nothing must inherit the STORE
    default (proving the store — not ``base_agent._provider_config`` — is the
    authoritative source, design §9 step 9). A second expert declaring its own
    provider + named credential resolves to a DISTINCT ``dspy.LM``. Both run their
    ``forward()`` concurrently with no cross-talk (the #818 requirement).
    """
    import dspy  # noqa: PLC0415

    for key in _BOOT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CLIO_CRED_OPENAI_ACCTB", "sk-openai-acctB")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    # The base agent points at a DIFFERENT provider/model than the store default,
    # so inheriting from base_agent (the old path) would be observably wrong.
    base_agent = SimpleNamespace(
        _provider_config=LMProviderConfig(provider="openai", model="base-agent-model")
    )

    app = build_app(sessions_path=tmp_path / "s.json")
    # Store default: a LOCAL provider with a marker model. This is what an
    # undeclared expert MUST inherit.
    app.state.provider_profiles = ProviderProfileStore.seed(
        LMSpec(provider="lm_studio", model="store-default-model")
    )

    windows = {"store-default-model": 8192, "gpt-4o": 128000}

    def _fake_handshake(ctx: Any, **_kwargs: Any) -> HandshakeReport:
        return _report(
            provider=ctx.provider_id,
            model_id=ctx.target_model,
            context_window=windows.get(ctx.target_model, 4096),
        )

    monkeypatch.setattr(resolver_mod, "run_handshake_sync", _fake_handshake)

    # Record every real dspy.LM built at each forward()'s dspy.context boundary.
    real_create_lm = config_mod.create_lm
    built_lms: list[Any] = []
    lms_lock = threading.Lock()

    def _recording_create_lm(cfg: LMProviderConfig) -> Any:
        lm = real_create_lm(cfg)
        with lms_lock:
            built_lms.append(lm)
        return lm

    monkeypatch.setattr(config_mod, "create_lm", _recording_create_lm)

    class _FakePredict:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def __call__(self, *_a: Any, **_k: Any) -> Any:
            return SimpleNamespace(answer="ok", expert_handoffs=[])

    monkeypatch.setattr(dspy, "Predict", _FakePredict)

    # Build the two expert modules under the ACTIVE app so _dynamic_agent_lm_config
    # reads the per-app store at __init__ time.
    token = _ctx.set_app(app)
    try:
        undeclared = _build_prompt_user_agent_module(
            base_agent,
            AgentDef(id="expert-undeclared", title="Undeclared"),  # inherits store default
        )
        cross = _build_prompt_user_agent_module(
            base_agent,
            AgentDef(
                id="expert-cross",
                title="Cross Provider",
                default_provider="openai",
                default_model="gpt-4o",
                credential_ref="openai:acctB",
            ),
        )
    finally:
        _ctx.reset(token)

    modules = {"undeclared": undeclared, "cross": cross}
    barrier = threading.Barrier(len(modules))

    def _run(name: str) -> None:
        barrier.wait()
        modules[name].forward(question="go", session_id=f"sess-{name}")

    with ThreadPoolExecutor(max_workers=len(modules)) as pool:
        list(pool.map(_run, list(modules)))

    built = {(lm.model, lm.kwargs.get("api_base"), lm.kwargs.get("api_key")) for lm in built_lms}
    expected = {
        # Undeclared expert inherited the STORE default (lm_studio/store-default-model),
        # NOT base_agent's openai/base-agent-model.
        (
            "openai/store-default-model",
            PROVIDER_DEFAULTS["lm_studio"]["api_base"],
            "lm-studio",
        ),
        # Cross-provider expert resolved its own openai identity + named credential.
        (
            "openai/gpt-4o",
            PROVIDER_DEFAULTS["openai"]["api_base"],
            "sk-openai-acctB",
        ),
    }
    assert len(built_lms) == len(modules)
    assert built == expected
    # The base agent's model must NOT appear — proof the store, not base_agent, drove it.
    assert all("base-agent-model" not in lm.model for lm in built_lms)
