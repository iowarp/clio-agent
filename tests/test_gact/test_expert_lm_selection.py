"""Per-expert LM selection through the rewritten ``_dynamic_agent_lm_config`` (design §4, step 6).

``_dynamic_agent_lm_config`` no longer builds a config behind a ``same_provider``
gate; it builds a serializable :class:`LMSpec` off the ``AgentDef`` (inheriting the
active default profile) and delegates to the pure resolver, returning a
``ResolvedLMSpec`` whose ``materialize`` resolves the credential fresh per
``forward()``. These tests prove the three behaviours the step must guarantee:

* **PER-EXPERT SELECTION** — an expert declaring a *different* provider +
  ``credential_ref`` authenticates (non-empty ``api_key``) and gets its own
  handshake-folded ``context_window`` (the cross-provider case the old gate broke).
* **BACKWARD-COMPAT** — an expert declaring nothing resolves byte-identical to the
  pre-change config (golden against the handshake-folded base the old gate copied).
* **CONCURRENCY** — N experts on N providers run their ``forward()`` concurrently
  and each real ``dspy.LM`` carries its own ``model`` / ``api_base`` / ``api_key``
  with no cross-talk (the #818 requirement a serializing lock could never deliver).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest

import clio_agent.config as config_mod
from clio_agent.config import PROVIDER_DEFAULTS, LMProviderConfig
from clio_agent.gact.agents.builders import (
    _build_prompt_user_agent_module,
    _dynamic_agent_lm_config,
)
from clio_agent.gact.types import AgentDef
from clio_agent.providers import resolver as resolver_mod
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    HandshakeReport,
    ModelProfile,
)

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _report(
    *,
    provider: str,
    model_id: str,
    context_window: int | None = None,
    loaded_context_window: int | None = None,
    output_limit: int | None = None,
    ok: bool = True,
) -> HandshakeReport:
    """Build a one-model :class:`HandshakeReport` (or a connectivity failure)."""
    if not ok:
        return HandshakeReport(
            provider_id=provider,
            provider_kind=provider,
            connectivity=ConnectivityState.UNREACHABLE,
            auth=AuthState.MISSING,
            error="backend unreachable",
            models=(),
        )
    return HandshakeReport(
        provider_id=provider,
        provider_kind=provider,
        connectivity=ConnectivityState.OK,
        auth=AuthState.OK,
        models=(
            ModelProfile(
                id=model_id,
                context_window=context_window,
                loaded_context_window=loaded_context_window,
                output_limit=output_limit,
            ),
        ),
    )


def _patch_handshake(monkeypatch: pytest.MonkeyPatch, report: HandshakeReport) -> None:
    """Patch the resolver's ``run_handshake_sync`` to return a fixed report."""

    def _fake(ctx: object, **_kwargs: object) -> HandshakeReport:
        return report

    monkeypatch.setattr(resolver_mod, "run_handshake_sync", _fake)


def _no_active_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``_default_profile_spec`` down the base-agent fallback path.

    A leaked ``active_app`` from another test would otherwise make the resolver
    read that app's profile store instead of the base agent's config; pin it to
    ``None`` so these tests deterministically exercise the baseline fallback.
    """
    monkeypatch.setattr("clio_agent.gact.context.active_app", lambda: None)


# --------------------------------------------------------------------------- #
# PER-EXPERT SELECTION
# --------------------------------------------------------------------------- #


def test_cross_provider_expert_authenticates_and_folds_own_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blueprint expert on a different provider + named credential gets its own key + window."""
    _no_active_app(monkeypatch)
    # Base/default profile is a LOCAL provider; the expert declares a cloud one.
    base_config = LMProviderConfig(provider="lm_studio", model="qwen-base")
    base_agent = SimpleNamespace(_provider_config=base_config)

    # The named per-account credential exists ONLY under its own keyed source.
    monkeypatch.setenv("CLIO_CRED_OPENAI_ACCTB", "sk-acctB-live")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _patch_handshake(
        monkeypatch, _report(provider="openai", model_id="gpt-4o", context_window=128000)
    )

    agent_def = AgentDef(
        id="cross-provider-expert",
        title="Cross Provider Expert",
        default_provider="openai",
        default_model="gpt-4o",
        credential_ref="openai:acctB",
    )

    resolved = _dynamic_agent_lm_config(base_agent, agent_def)
    cfg = resolved.materialize()

    # Cross-provider identity travelled with the expert (old same_provider gate
    # would have produced provider=openai but api_key="" and context_window=None).
    assert cfg.provider == "openai"
    assert cfg.model == "gpt-4o"
    assert cfg.api_base == PROVIDER_DEFAULTS["openai"]["api_base"]
    assert cfg.api_key == "sk-acctB-live"  # authenticated a second provider
    assert cfg.context_window == 128000  # its OWN handshake-folded window
    assert resolved.handshake_fallback is None


def test_named_ref_absent_surfaces_empty_key_no_silent_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared named credential that is missing does NOT borrow the default account key."""
    _no_active_app(monkeypatch)
    base_agent = SimpleNamespace(_provider_config=LMProviderConfig(provider="lm_studio"))
    # The DEFAULT openai key is present, but a named ref must not silently use it.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-default-account")
    monkeypatch.delenv("CLIO_CRED_OPENAI_ACCTB", raising=False)
    _patch_handshake(
        monkeypatch, _report(provider="openai", model_id="gpt-4o", context_window=128000)
    )

    agent_def = AgentDef(
        id="missing-cred-expert",
        title="Missing Cred Expert",
        default_provider="openai",
        default_model="gpt-4o",
        credential_ref="openai:acctB",
    )
    cfg = _dynamic_agent_lm_config(base_agent, agent_def).materialize()
    assert cfg.api_key == ""  # downstream LM call surfaces an actionable auth error


# --------------------------------------------------------------------------- #
# BACKWARD-COMPAT (golden)
# --------------------------------------------------------------------------- #


def test_undeclared_expert_is_byte_identical_to_prechange_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expert declaring no provider resolves byte-identical to the old gated config.

    The pre-change ``_dynamic_agent_lm_config`` returned a config equal to the
    same-provider base on ``provider``/``api_base``/``model``/``api_key``/
    ``temperature``/``max_tokens``/``thinking_budget`` and copied the base's
    handshake-discovered ``context_window``/``chosen_context``. The captured
    baseline is exactly that handshake-folded base config.
    """
    _no_active_app(monkeypatch)
    base_config = LMProviderConfig(provider="lm_studio", model="qwen-test")
    report = _report(
        provider="lm_studio",
        model_id="qwen-test",
        context_window=262144,
        loaded_context_window=262144,
    )
    # Simulate today's global bind having folded the handshake into the base.
    base_config.apply_handshake(report)

    golden_fields = (
        "provider",
        "api_base",
        "model",
        "api_key",
        "temperature",
        "max_tokens",
        "thinking_budget",
        "context_window",
        "chosen_context",
    )
    baseline = {name: getattr(base_config, name) for name in golden_fields}
    # Sanity: the captured baseline is the real single-default-LM shape.
    assert baseline["api_key"] == "lm-studio"
    assert baseline["context_window"] == 262144
    assert baseline["chosen_context"] == 262144

    _patch_handshake(monkeypatch, report)
    base_agent = SimpleNamespace(_provider_config=base_config)
    agent_def = AgentDef(id="undeclared-expert", title="Undeclared Expert")  # inherits everything

    resolved = _dynamic_agent_lm_config(base_agent, agent_def)
    cfg = resolved.materialize()

    for name in golden_fields:
        assert getattr(cfg, name) == baseline[name], f"golden mismatch on {name!r}"
    assert resolved.handshake_fallback is None


def test_undeclared_cloud_expert_inherits_boot_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A GACT booted from CLIO_LM_API_KEY (provider-native var UNSET) still authenticates experts.

    Finding #1 (RULE 2 silent break): booting a cloud provider from
    ``CLIO_LM_API_KEY`` — the documented primary way to set the key — WITHOUT
    ``OPENAI_API_KEY`` gave every undeclared expert an EMPTY ``api_key`` (all
    expert calls 401) while the main agent kept working off ``_provider_config``.
    The undeclared/default expert MUST end up with the exact boot key.

    This is the strong golden the old ``lm_studio`` test could not be: the
    provider-native ``OPENAI_API_KEY`` is UNSET, so the coincidental
    placeholder-match cannot mask the regression — only the boot credential
    carried onto the default profile makes this pass.
    """
    _no_active_app(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("CLIO_LM_API_KEY", "sk-boot-key-XYZ")

    # The boot/default config resolves its key from CLIO_LM_API_KEY exactly as
    # ``load_config_from_env`` would (provider-native var absent).
    base_config = LMProviderConfig(provider="openai", model="gpt-4o", api_key="sk-boot-key-XYZ")
    assert base_config.api_key == "sk-boot-key-XYZ"
    base_agent = SimpleNamespace(_provider_config=base_config)

    _patch_handshake(
        monkeypatch, _report(provider="openai", model_id="gpt-4o", context_window=128000)
    )
    agent_def = AgentDef(id="undeclared-cloud", title="Undeclared Cloud")  # inherits everything

    cfg = _dynamic_agent_lm_config(base_agent, agent_def).materialize()
    assert cfg.provider == "openai"
    # The exact boot key the main agent runs — not an empty string (401), not the
    # cloud PROVIDER_DEFAULTS placeholder.
    assert cfg.api_key == "sk-boot-key-XYZ"


def test_undeclared_cloud_expert_prefers_boot_over_divergent_native_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When OPENAI_API_KEY names a DIFFERENT account than the boot key, the expert uses the boot key.

    Finding #1 ("Worse" sub-case): if ``OPENAI_API_KEY`` holds a different account
    than the ``CLIO_LM_API_KEY`` the GACT booted from, an undeclared expert must
    still authenticate as the SAME identity the main agent runs (the boot key),
    not silently as the wrong account.
    """
    _no_active_app(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-other-account")
    monkeypatch.setenv("CLIO_LM_API_KEY", "sk-boot-account")

    base_config = LMProviderConfig(provider="openai", model="gpt-4o", api_key="sk-boot-account")
    base_agent = SimpleNamespace(_provider_config=base_config)
    _patch_handshake(
        monkeypatch, _report(provider="openai", model_id="gpt-4o", context_window=128000)
    )
    agent_def = AgentDef(id="undeclared-cloud2", title="Undeclared Cloud 2")

    cfg = _dynamic_agent_lm_config(base_agent, agent_def).materialize()
    assert cfg.api_key == "sk-boot-account"


# --------------------------------------------------------------------------- #
# CONCURRENCY (safe by construction — #818)
# --------------------------------------------------------------------------- #


@pytest.mark.concurrency
def test_n_experts_n_providers_no_lm_cross_talk(monkeypatch: pytest.MonkeyPatch) -> None:
    """N experts on N providers run ``forward()`` concurrently; each dspy.LM is its own.

    Every expert's real ``dspy.LM`` must carry its own ``model``/``api_base``/
    ``api_key`` — no torn, interleaved multi-key state (the failure the process-
    global ``os.environ`` + dspy ``main_thread_config`` bind produced, and the
    reason a serializing lock is the wrong shape). The LM is built inside each
    ``forward()`` at the unchanged ``dspy.context`` boundary.
    """
    import dspy  # noqa: PLC0415

    _no_active_app(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-default")
    monkeypatch.setenv("CLIO_CRED_OPENAI_ACCTB", "sk-openai-acctB")

    # provider/model/credential_ref → each declares its own full identity.
    specs = {
        "local": ("lm_studio", "qwen-local", ""),
        "cloud_default": ("openai", "gpt-4o", ""),
        "cloud_acctB": ("openai", "gpt-4o-mini", "openai:acctB"),
    }
    windows = {"qwen-local": 8192, "gpt-4o": 128000, "gpt-4o-mini": 64000}

    # Route each handshake by (provider_id, target_model) so folds stay isolated.
    def _fake_handshake(ctx: Any, **_kwargs: Any) -> HandshakeReport:
        return _report(
            provider=ctx.provider_id,
            model_id=ctx.target_model,
            context_window=windows.get(ctx.target_model, 4096),
        )

    monkeypatch.setattr(resolver_mod, "run_handshake_sync", _fake_handshake)

    # Record every real dspy.LM built at a forward()'s dspy.context boundary.
    real_create_lm = config_mod.create_lm
    built_lms: list[dspy.LM] = []
    lms_lock = threading.Lock()

    def _recording_create_lm(cfg: LMProviderConfig) -> dspy.LM:
        lm = real_create_lm(cfg)
        with lms_lock:
            built_lms.append(lm)
        return lm

    monkeypatch.setattr(config_mod, "create_lm", _recording_create_lm)

    # Neutralize the actual model call so forward() never hits a network/LM.
    class _FakePredict:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def __call__(self, *_a: Any, **_k: Any) -> Any:
            return SimpleNamespace(answer="ok", expert_handoffs=[])

    monkeypatch.setattr(dspy, "Predict", _FakePredict)

    base_agent = SimpleNamespace(_provider_config=LMProviderConfig(provider="lm_studio"))
    modules = {
        name: _build_prompt_user_agent_module(
            base_agent,
            AgentDef(
                id=f"expert-{name}",
                title=f"Expert {name}",
                default_provider=provider,
                default_model=model,
                credential_ref=cred_ref,
            ),
        )
        for name, (provider, model, cred_ref) in specs.items()
    }

    barrier = threading.Barrier(len(modules))

    def _run(name: str) -> None:
        barrier.wait()  # maximize interleaving of the per-call materialize+bind
        modules[name].forward(question="go", session_id=f"sess-{name}")

    with ThreadPoolExecutor(max_workers=len(modules)) as pool:
        list(pool.map(_run, list(modules)))

    # One LM per expert forward; each carries its OWN model/api_base/api_key.
    built = {(lm.model, lm.kwargs.get("api_base"), lm.kwargs.get("api_key")) for lm in built_lms}
    expected = {
        (
            "openai/qwen-local",
            PROVIDER_DEFAULTS["lm_studio"]["api_base"],
            "lm-studio",
        ),
        (
            "openai/gpt-4o",
            PROVIDER_DEFAULTS["openai"]["api_base"],
            "sk-openai-default",
        ),
        (
            "openai/gpt-4o-mini",
            PROVIDER_DEFAULTS["openai"]["api_base"],
            "sk-openai-acctB",
        ),
    }
    assert len(built_lms) == len(modules)
    assert built == expected
