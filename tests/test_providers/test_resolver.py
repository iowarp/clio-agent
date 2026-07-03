"""Tests for :mod:`clio_agent.providers.resolver` (design §3.3, step 3).

Covers the pure spec → :class:`LMProviderConfig` resolution with a cached
handshake fold:

* **Golden equivalence** — for a same-provider / same-model spec the materialized
  config matches today's ``_dynamic_agent_lm_config`` output on
  ``api_base`` / ``model`` / ``api_key`` / ``context_window``.
* **Per-expert / cross-provider** — a cross-provider spec resolves a real key via
  the :class:`CredentialResolver` and folds a discovered
  ``context_window`` / context-aware ``max_tokens``.
* **No silent fallback** — a forced handshake failure keeps the static
  ``PROVIDER_DEFAULTS`` caps AND attaches a structured handshake-fallback reason;
  a missing named credential does not silently fall back to the default account.
* **Purity / idempotence / concurrency** — resolve writes no process-global
  state, two resolves yield the same result, and N specs on N providers
  materialize independent, non-cross-talking configs.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from clio_agent.config import PROVIDER_DEFAULTS, LMProviderConfig
from clio_agent.gact.types import AgentDef
from clio_agent.providers import resolver as resolver_mod
from clio_agent.providers.credentials import CredentialResolver
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    HandshakeReport,
    ModelProfile,
)
from clio_agent.providers.lm_spec import LMSpec, build_spec, spec_from_config
from clio_agent.providers.resolver import (
    HANDSHAKE_FALLBACK_REASONS,
    resolve_endpoint_and_handshake,
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
    is_reasoning: bool = False,
    ok: bool = True,
    models: tuple[ModelProfile, ...] | None = None,
) -> HandshakeReport:
    """Build a :class:`HandshakeReport` with one model profile (or a failure)."""
    if not ok:
        return HandshakeReport(
            provider_id=provider,
            provider_kind=provider,
            connectivity=ConnectivityState.UNREACHABLE,
            auth=AuthState.MISSING,
            error="backend unreachable",
            models=(),
        )
    if models is None:
        models = (
            ModelProfile(
                id=model_id,
                context_window=context_window,
                loaded_context_window=loaded_context_window,
                output_limit=output_limit,
                is_reasoning=is_reasoning,
            ),
        )
    return HandshakeReport(
        provider_id=provider,
        provider_kind=provider,
        connectivity=ConnectivityState.OK,
        auth=AuthState.OK,
        models=models,
    )


def _patch_handshake(monkeypatch: pytest.MonkeyPatch, report_or_exc: object) -> list[object]:
    """Patch ``resolver.run_handshake_sync`` to return (or raise) a fixed value.

    Returns a list the fake appends each received context to, so a test can assert
    the resolver ran the handshake exactly as expected.
    """
    calls: list[object] = []

    def _fake(ctx: object, **_kwargs: object) -> HandshakeReport:
        calls.append(ctx)
        if isinstance(report_or_exc, Exception):
            raise report_or_exc
        assert isinstance(report_or_exc, HandshakeReport)
        return report_or_exc

    monkeypatch.setattr(resolver_mod, "run_handshake_sync", _fake)
    return calls


# --------------------------------------------------------------------------- #
# golden equivalence
# --------------------------------------------------------------------------- #


def test_golden_equivalence_same_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_dynamic_agent_lm_config`` (now a resolver delegate) matches a direct resolve.

    Since step 6 ``_dynamic_agent_lm_config`` builds an ``LMSpec`` and delegates to
    :func:`resolve_endpoint_and_handshake`, an undeclared same-provider expert must
    materialize to exactly what a direct resolve of the same base spec yields.
    """
    from clio_agent.gact.agents.builders import _dynamic_agent_lm_config

    base_config = LMProviderConfig(provider="lm_studio", model="qwen-test")
    report = _report(provider="lm_studio", model_id="qwen-test", context_window=262144)
    # Simulate today's global bind having folded the handshake into the base.
    base_config.apply_handshake(report)
    assert base_config.context_window == 262144
    assert base_config.api_key == "lm-studio"  # local-provider placeholder

    # Patch the handshake BEFORE the delegate runs it, and pin active_app to None so
    # the delegate takes the base-agent fallback (not a leaked profile store).
    _patch_handshake(monkeypatch, report)
    monkeypatch.setattr("clio_agent.gact.context.active_app", lambda: None)

    base_agent = SimpleNamespace(_provider_config=base_config)
    agent_def = AgentDef(id="expert-a", title="Expert A")  # declares nothing → inherit

    today_cfg = _dynamic_agent_lm_config(base_agent, agent_def).materialize(CredentialResolver())

    # Direct resolver path: build the spec off the same base + agent_def, fold the
    # same handshake, resolve the credential fresh.
    default_spec = spec_from_config(base_config)
    spec = build_spec(agent_def, default_spec)
    new_cfg = resolve_endpoint_and_handshake(spec).materialize(CredentialResolver())

    assert new_cfg.api_base == today_cfg.api_base
    assert new_cfg.model == today_cfg.model
    assert new_cfg.api_key == today_cfg.api_key == "lm-studio"
    assert new_cfg.context_window == today_cfg.context_window == 262144


def test_skeleton_carries_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cached skeleton is key-less; the credential only lands at materialize."""
    _patch_handshake(
        monkeypatch, _report(provider="lm_studio", model_id="qwen-test", context_window=8192)
    )
    spec = LMSpec(provider="lm_studio", model="qwen-test")
    resolved = resolve_endpoint_and_handshake(spec)
    assert resolved.config_skeleton.api_key == ""
    # And the endpoint/caps are populated (PROVIDER_DEFAULTS fill happened).
    assert resolved.config_skeleton.api_base == PROVIDER_DEFAULTS["lm_studio"]["api_base"]
    assert resolved.config_skeleton.context_window == 8192


# --------------------------------------------------------------------------- #
# per-expert / cross-provider
# --------------------------------------------------------------------------- #


def test_cross_provider_real_key_and_folded_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cross-provider spec gets a real key + folded context_window / max_tokens."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-key")
    report = _report(
        provider="openai",
        model_id="gpt-4o",
        context_window=128000,
        output_limit=16384,
        is_reasoning=False,
    )
    _patch_handshake(monkeypatch, report)

    # max_tokens=None → the handshake is allowed to size a context-aware cap.
    spec = LMSpec(provider="openai", model="gpt-4o", max_tokens=None)
    resolved = resolve_endpoint_and_handshake(spec)
    cfg = resolved.materialize(CredentialResolver())

    assert resolved.handshake_fallback is None
    assert cfg.provider == "openai"
    assert cfg.api_key == "sk-live-key"  # resolved fresh, cross-provider
    assert cfg.context_window == 128000
    # context-aware max_tokens = min(output_limit, context_window)
    assert cfg.max_tokens == 16384


def test_named_ref_missing_no_silent_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A named credential ref that resolves empty does NOT borrow the default key."""
    # The default account key IS present, but a named ref must not silently use it.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-default-account")
    monkeypatch.delenv("CLIO_CRED_OPENAI_ACCTB", raising=False)
    _patch_handshake(
        monkeypatch, _report(provider="openai", model_id="gpt-4o", context_window=128000)
    )

    spec = LMSpec(provider="openai", model="gpt-4o", credential_ref="openai:acctB")
    resolved = resolve_endpoint_and_handshake(spec)
    cfg = resolved.materialize(CredentialResolver())

    assert cfg.api_key == ""  # surfaced as auth error downstream, not a silent swap


def test_named_ref_present_uses_its_own_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A named credential ref reads its own per-account source."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-default-account")
    monkeypatch.setenv("CLIO_CRED_OPENAI_ACCTB", "sk-acctB-key")
    _patch_handshake(
        monkeypatch, _report(provider="openai", model_id="gpt-4o", context_window=128000)
    )

    spec = LMSpec(provider="openai", model="gpt-4o", credential_ref="openai:acctB")
    cfg = resolve_endpoint_and_handshake(spec).materialize(CredentialResolver())
    assert cfg.api_key == "sk-acctB-key"


# --------------------------------------------------------------------------- #
# no-silent-fallback: handshake failure records a structured reason
# --------------------------------------------------------------------------- #


def test_handshake_unreachable_records_reason_and_static_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed handshake keeps static PROVIDER_DEFAULTS caps AND records a reason."""
    _patch_handshake(monkeypatch, _report(provider="argonne", model_id="x", ok=False))

    spec = LMSpec(provider="argonne", model="openai/gpt-oss-120b")
    resolved = resolve_endpoint_and_handshake(spec)

    assert resolved.handshake_fallback is not None
    assert resolved.handshake_fallback["reason"] == "handshake_unreachable"
    assert resolved.handshake_fallback["reason"] in HANDSHAKE_FALLBACK_REASONS
    assert resolved.handshake_fallback["degraded"] is True
    assert "message" in resolved.handshake_fallback
    # Static caps preserved: no discovered window, provider-default max_tokens.
    cfg = resolved.materialize(CredentialResolver())
    assert cfg.context_window is None
    assert cfg.max_tokens == int(PROVIDER_DEFAULTS["argonne"]["max_tokens"])  # 4096 static cap


def test_handshake_error_records_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """A handshake that raises is caught and recorded as ``handshake_error``."""
    _patch_handshake(monkeypatch, RuntimeError("boom"))
    spec = LMSpec(provider="openai", model="gpt-4o")
    resolved = resolve_endpoint_and_handshake(spec)
    assert resolved.handshake_fallback is not None
    assert resolved.handshake_fallback["reason"] == "handshake_error"
    assert "boom" in resolved.handshake_fallback["message"]
    assert resolved.config_skeleton.context_window is None


def test_handshake_model_unresolved_records_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """A report that lists other models (no match) records ``handshake_model_unresolved``."""
    other = ModelProfile(id="some-other-model", context_window=4096)
    also = ModelProfile(id="yet-another", context_window=4096)
    _patch_handshake(
        monkeypatch, _report(provider="openai", model_id="gpt-4o", models=(other, also))
    )
    spec = LMSpec(provider="openai", model="gpt-4o")
    resolved = resolve_endpoint_and_handshake(spec)
    assert resolved.handshake_fallback is not None
    assert resolved.handshake_fallback["reason"] == "handshake_model_unresolved"
    assert resolved.config_skeleton.context_window is None


def test_handshake_fallback_payload_rejects_unknown_reason() -> None:
    """The reason builder refuses an unknown reason (no silent empty payload)."""
    with pytest.raises(ValueError, match="Unknown handshake fallback reason"):
        resolver_mod.handshake_fallback_payload("not_a_real_reason")


# --------------------------------------------------------------------------- #
# purity / idempotence / concurrency
# --------------------------------------------------------------------------- #


def test_resolve_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two resolves of the same spec yield equal materialized configs."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-key")
    _patch_handshake(
        monkeypatch,
        _report(provider="openai", model_id="gpt-4o", context_window=128000, output_limit=16384),
    )
    spec = LMSpec(provider="openai", model="gpt-4o", max_tokens=None)

    a = resolve_endpoint_and_handshake(spec).materialize(CredentialResolver())
    b = resolve_endpoint_and_handshake(spec).materialize(CredentialResolver())

    for attr in ("provider", "api_base", "model", "api_key", "context_window", "max_tokens"):
        assert getattr(a, attr) == getattr(b, attr)


def test_resolve_writes_no_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve + materialize mutate no os.environ state (pure)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-key")
    _patch_handshake(
        monkeypatch, _report(provider="openai", model_id="gpt-4o", context_window=128000)
    )
    before = dict(os.environ)
    spec = LMSpec(provider="openai", model="gpt-4o")
    resolve_endpoint_and_handshake(spec).materialize(CredentialResolver())
    assert dict(os.environ) == before


def test_concurrent_specs_do_not_cross_talk(monkeypatch: pytest.MonkeyPatch) -> None:
    """N specs on N providers materialize independent configs with no cross-talk."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("CLIO_CRED_OPENAI_ACCTB", "sk-openai-acctB")

    specs = {
        "lm": LMSpec(provider="lm_studio", model="qwen-local"),
        "openai_default": LMSpec(provider="openai", model="gpt-4o"),
        "openai_acctB": LMSpec(provider="openai", model="gpt-4o", credential_ref="openai:acctB"),
    }
    reports = {
        "lm": _report(provider="lm_studio", model_id="qwen-local", context_window=8192),
        "openai_default": _report(provider="openai", model_id="gpt-4o", context_window=128000),
        "openai_acctB": _report(provider="openai", model_id="gpt-4o", context_window=128000),
    }

    # Route each handshake by provider_id/model so concurrent folds stay isolated.
    def _fake(ctx: object, **_kwargs: object) -> HandshakeReport:
        for key, spec in specs.items():
            if ctx.provider_id == spec.provider and ctx.target_model == spec.model:
                # Distinguish the two openai specs by nothing on ctx (same
                # provider/model): return the shared openai report — the api_key
                # differs at materialize, which is what we assert.
                return reports[key]
        raise AssertionError("unexpected handshake ctx")

    monkeypatch.setattr(resolver_mod, "run_handshake_sync", _fake)

    barrier = threading.Barrier(len(specs))
    results: dict[str, LMProviderConfig] = {}
    lock = threading.Lock()

    def _run(name: str) -> None:
        resolved = resolve_endpoint_and_handshake(specs[name])
        barrier.wait()  # maximize interleaving of the materialize half
        cfg = resolved.materialize(CredentialResolver())
        with lock:
            results[name] = cfg

    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        list(pool.map(_run, list(specs)))

    assert results["lm"].provider == "lm_studio"
    assert results["lm"].api_key == "lm-studio"
    assert results["openai_default"].provider == "openai"
    assert results["openai_default"].api_key == "sk-openai"
    assert results["openai_acctB"].api_key == "sk-openai-acctB"
    # Endpoints did not cross-talk.
    assert results["lm"].api_base == PROVIDER_DEFAULTS["lm_studio"]["api_base"]
    assert results["openai_default"].api_base == PROVIDER_DEFAULTS["openai"]["api_base"]
