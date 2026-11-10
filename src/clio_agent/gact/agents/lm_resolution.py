"""Resolve dynamic-agent model profiles without constructing or running agents."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from clio_agent.gact import context as _ctx

if TYPE_CHECKING:
    from clio_agent.gact.types import AgentDef
    from clio_agent.providers.lm_spec import LMSpec
    from clio_agent.providers.resolver import ResolvedLMSpec


def _default_profile_spec(base_agent: Any) -> "LMSpec":
    """Return the default-profile :class:`LMSpec` an undeclared expert inherits.

    Reads the active per-app profile store (``app.state.provider_profiles`` — the
    immutable, RCU-swapped registry from design §3.4) when an app is bound;
    otherwise falls back to the boot agent's live ``_provider_config`` (or a fresh
    :func:`load_config_from_env` config) projected to a secret-free spec. That
    fallback is the byte-identical single-default-LM baseline (RULE 2), so a
    direct call with no active app resolves exactly as before.

    Args:
        base_agent: The owning agent; consulted for ``_provider_config`` only when
            no per-app profile store is available.

    Returns:
        The default-profile :class:`LMSpec` whose fields undeclared expert fields
        inherit.
    """
    from clio_agent.providers.lm_spec import spec_from_config  # noqa: PLC0415

    app = _ctx.active_app()
    store = getattr(getattr(app, "state", None), "provider_profiles", None) if app else None
    if store is not None:
        default = getattr(store, "default", None)
        if default is not None:
            return default
    base_config = getattr(base_agent, "_provider_config", None)
    if base_config is None:
        from clio_agent.config import load_config_from_env  # noqa: PLC0415

        base_config = load_config_from_env()
    return spec_from_config(base_config)


def _dynamic_agent_lm_config(base_agent: Any, agent_def: "AgentDef") -> "ResolvedLMSpec":
    """Resolve a registered dynamic agent's provider identity to a ``ResolvedLMSpec``.

    Builds a serializable :class:`~clio_agent.providers.lm_spec.LMSpec` from the
    ``AgentDef`` — inheriting every field it does not declare from the active
    default profile (:func:`_default_profile_spec`) — then delegates to
    :func:`~clio_agent.providers.resolver.resolve_endpoint_and_handshake`, the
    pure endpoint + cached-handshake half (design §3.3/§4). The credential is
    deliberately NOT resolved here; each expert ``forward()`` resolves it fresh
    via :meth:`~clio_agent.providers.resolver.ResolvedLMSpec.materialize` because
    tokens rotate mid-session.

    This drops the former ``same_provider`` gate: a cross-provider expert now
    authenticates its own provider and gets its own handshake-folded
    ``context_window`` / context-aware ``max_tokens`` / reasoning + tool flags,
    instead of the empty credentials and ``None`` context window the gate produced
    (design §2 "the gap"). The undeclared same-provider expert still resolves to
    the default profile, preserving the baseline.

    Args:
        base_agent: The owning agent (source of the default-profile fallback).
        agent_def: The registered dynamic agent's definition.

    Returns:
        A :class:`~clio_agent.providers.resolver.ResolvedLMSpec` — the key-less,
        handshake-populated skeleton plus any structured handshake-fallback
        reason. Call :meth:`ResolvedLMSpec.materialize` to get the runnable config.
    """
    from clio_agent.providers.lm_spec import build_spec, same_provider_identity  # noqa: PLC0415
    from clio_agent.providers.resolver import resolve_endpoint_and_handshake  # noqa: PLC0415

    default_spec = _default_profile_spec(base_agent)
    # The boot/default-profile credential the main agent runs (its resolved
    # ``_provider_config.api_key``). It is carried onto the DEFAULT-profile path so
    # an undeclared expert authenticates with the exact key the main agent uses —
    # even when that key came from the generic ``CLIO_LM_API_KEY`` boot var and the
    # provider-native var (e.g. ``OPENAI_API_KEY``) is unset or names a different
    # account (finding #1: RULE-2 main-works/experts-401 asymmetry). The
    # credential_ref/spec stay secret-free — this key never serializes; it is a
    # runtime resolution artifact threaded only when the expert resolves to the
    # boot provider's default profile.
    base_config = getattr(base_agent, "_provider_config", None)
    boot_provider = str(getattr(base_config, "provider", "") or "")
    boot_key = str(getattr(base_config, "api_key", "") or "")
    declared_provider = str(getattr(agent_def, "default_provider", "") or "")
    if declared_provider and not same_provider_identity(declared_provider, default_spec):
        # Cross-provider expert: endpoint / model / credential-ref / transport and
        # provider options are provider-scoped, so inheriting the default provider's
        # values would point the new provider at the wrong endpoint (a foreign
        # credential) and fail on options it does not take (OpenRouter's
        # show_diffusion on codex). Identity is the preset/server id, not the wire
        # kind: OpenRouter and the direct OpenAI preset share kind ``openai``.
        # Blank them — the resolver fills the new provider's PROVIDER_DEFAULTS and
        # its own default credential — while provider-agnostic sampling params
        # still inherit (the old ``same_provider`` semantics, design §2; §4).
        default_spec = replace(
            default_spec,
            provider=declared_provider,
            model="",
            api_base="",
            credential_ref="",
            provider_options={},
            transport="",
        )
    spec = build_spec(agent_def, default_spec)
    # Thread the boot credential only when the expert resolves to the boot
    # provider's default profile and that captured key cannot go stale
    # (see boot_credential_stays_current).
    from clio_agent.providers.credentials import boot_credential_stays_current  # noqa: PLC0415

    current = boot_key and spec.provider == boot_provider
    default_credential = boot_key if current and boot_credential_stays_current(spec) else ""
    return resolve_endpoint_and_handshake(spec, default_credential=default_credential)
