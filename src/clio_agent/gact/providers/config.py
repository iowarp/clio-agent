"""Read-only LM-provider configuration helpers for the GACT server (#714).

This module owns the *read-only* provider-bind queries carved out of
``clio_agent.gact.app``:

* :func:`_effective_lm_config` -- the configured LM (``app.state.lm_config``,
  set by ``PUT /v1/providers/lm``) merged with the live agent's effective
  ``LMProviderConfig`` so a GACT booted from ``CLIO_LM_PROVIDER`` still reports a
  complete config.
* :func:`_provider_runtime_kind` -- translate a catalog provider id (or an
  already-runtime kind) into the wire/runtime provider kind via the real
  provider registry.

Both are pure reads: :func:`_effective_lm_config` only *reads* ``app.state`` (no
mutation), and :func:`_provider_runtime_kind` only queries the registry. The
write-side bind path (``_apply_lm_provider``) lives with the provider route
handler in ``gact.app`` and is out of scope for this module. Imports stay
lazy/leaf so this module never loads ``gact.app``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI


def _provider_runtime_kind(provider_id: str) -> str:
    """Return the wire/runtime provider kind for a catalog id or provider kind."""

    provider_id = str(provider_id or "").strip()
    if not provider_id:
        return ""
    try:
        from clio_agent.providers.registry import get_provider  # noqa: PLC0415

        provider = get_provider(provider_id)
    except Exception:
        provider = None
    if provider is not None:
        return str(provider.provider_kind or provider_id)
    return provider_id


def _effective_lm_config(app: "FastAPI") -> dict[str, Any]:
    """Return the configured LM, falling back to the live agent config.

    ``app.state.lm_config`` is populated by ``PUT /v1/providers/lm``.
    When GACT boots from ``CLIO_LM_PROVIDER`` instead, the live
    ``ClioAgent`` still carries the effective ``LMProviderConfig``.
    """

    cfg = dict(getattr(app.state, "lm_config", None) or {})
    agent = getattr(app.state, "agent", None)
    provider_config = getattr(agent, "_provider_config", None)
    if provider_config is None:
        return cfg

    for key in (
        "provider",
        "api_base",
        "model",
        "temperature",
        "max_tokens",
        "context_length",
        "thinking_budget",
        "chosen_context",
        "context_window",
        "is_reasoning",
        "native_tool_calling",
    ):
        if not cfg.get(key):
            value = getattr(provider_config, key, None)
            if value is not None:
                cfg[key] = value
    if not cfg.get("transport") and getattr(provider_config, "provider", "") == "codex":
        cfg["transport"] = getattr(provider_config, "codex_transport", None)
    return cfg
