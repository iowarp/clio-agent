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
* The model-ref + multimodal-capability family used by the turn-entry routes to
  validate a per-message / per-session model override against the active global
  LM and to gate image parts: :func:`_model_ref_dict`,
  :func:`_model_ref_is_empty`, :func:`_active_lm_model_ref`,
  :func:`_model_ref_matches_active`, :func:`_unsupported_model_ref_error`,
  :func:`_active_lm_supports_vision`, :func:`_image_part_error`.

All are pure reads: they only *read* ``app.state`` / normalise a value (no
mutation), and :func:`_provider_runtime_kind` only queries the registry. The
write-side bind path (``_apply_lm_provider``) lives with the provider route
handler in ``gact.app`` and is out of scope for this module. Imports stay
lazy/leaf so this module never loads ``gact.app``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from fastapi import FastAPI


def _provider_runtime_kind(provider_id: str) -> str:
    """Return the wire/runtime provider kind for a catalog id or provider kind."""

    provider_id = str(provider_id or "").strip()
    if not provider_id:
        return ""
    try:
        from clio_agent.providers.catalog import get_provider  # noqa: PLC0415

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

    This stays the **gating** read: it reports the live/bound config only, and an
    unconfigured GACT (no live ``_provider_config``) reports an empty config so
    the model-ref / vision route gates behave. The per-app store's default profile
    is reported *separately* by :func:`_default_profile_spec` (used by the GET body
    builder ``_lm_provider_info``), which never feeds those gates. After a bind the
    two are ``spec_from_config``-consistent by construction.
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
    if not cfg.get("transport"):
        provider = getattr(provider_config, "provider", "")
        if provider == "codex":
            cfg["transport"] = getattr(provider_config, "codex_transport", None)
        elif provider == "claude_code":
            cfg["transport"] = getattr(provider_config, "claude_code_transport", None)
    return cfg


def _default_profile_spec(app: "FastAPI") -> Any:
    """Return the per-app profile store's default :class:`LMSpec`, or ``None``.

    Read-only: consults ``app.state.provider_profiles`` (the immutable, RCU-swapped
    :class:`~clio_agent.gact.providers.profile_store.ProviderProfileStore` the admin
    bind swaps and every undeclared expert inherits — design §3.4/§5). Returns
    ``None`` when no store is bound (e.g. a bare ``SimpleNamespace`` app in a unit
    test). This is the read side reading the default **off the store**; it is used
    by the GET body builder to report the bound default and never feeds the
    model-ref / vision route gates.
    """

    store = getattr(getattr(app, "state", None), "provider_profiles", None)
    if store is None:
        return None
    return getattr(store, "default", None)


def _model_ref_dict(value: Any) -> dict[str, str]:
    """Normalize a GACT ModelRef-like value to its wire keys."""

    if value is None:
        raw: Mapping[str, Any] = {}
    elif isinstance(value, Mapping):
        raw = value
    elif hasattr(value, "model_dump"):
        raw = value.model_dump(exclude_none=True)
    else:
        raw = {
            "provider_id": getattr(value, "provider_id", ""),
            "model_id": getattr(value, "model_id", ""),
            "variant": getattr(value, "variant", ""),
        }
    return {
        "provider_id": str(raw.get("provider_id") or raw.get("provider") or ""),
        "model_id": str(raw.get("model_id") or raw.get("model") or ""),
        "variant": str(raw.get("variant") or ""),
    }


def _model_ref_is_empty(value: Any) -> bool:
    """Return true when a model ref carries no selection."""

    ref = _model_ref_dict(value)
    return not any(ref.values())


def _active_lm_model_ref(app: "FastAPI") -> dict[str, str]:
    """Return the active global LM as a GACT ModelRef-shaped dict."""

    cfg = _effective_lm_config(app)
    provider = str(cfg.get("provider") or "")
    model = str(cfg.get("model") or "")
    return {"provider_id": provider, "model_id": model, "variant": ""}


def _model_ref_matches_active(value: Any, app: "FastAPI") -> bool:
    """Return true when a requested model ref exactly matches the active LM."""

    return _model_ref_dict(value) == _active_lm_model_ref(app)


def _unsupported_model_ref_error(
    *,
    session_id: str,
    source: str,
    model_ref: Any,
    active_model: Mapping[str, str],
) -> ErrorEnvelope:
    """Build a structured error for currently unsupported model refs."""

    return ErrorEnvelope(
        error=ErrorInfo(
            error="not_implemented",
            message=(
                f"{source} model overrides are not implemented for a model "
                "that differs from the active global LM."
            ),
            details={
                "session_id": session_id,
                "source": source,
                "model": _model_ref_dict(model_ref),
                "active_model": dict(active_model),
                "recovery_actions": [
                    "put_global_lm_provider",
                    "clear_session_model",
                    "retry",
                    "exit",
                ],
            },
            recoverable=True,
        )
    )


def _active_lm_supports_vision(app: "FastAPI") -> bool:
    """Return whether the active provider transport can carry image parts."""

    cfg = _effective_lm_config(app)
    if "supports_vision" in cfg:
        return bool(cfg.get("supports_vision"))
    return str(cfg.get("provider") or "") in {"openai", "anthropic"}


def _image_part_error(
    *,
    session_id: str,
    image_count: int,
    provider: Mapping[str, Any],
) -> ErrorEnvelope:
    """Build the structured error for image parts on a text-only provider."""

    provider_id = str(provider.get("provider") or provider.get("provider_id") or "")
    model_id = str(provider.get("model") or provider.get("model_id") or "")
    return ErrorEnvelope(
        error=ErrorInfo(
            error="unsupported_multimodal_image",
            message=(
                "The active LM provider cannot receive image message parts. "
                "Switch to a vision-capable direct provider or remove the image."
            ),
            details={
                "session_id": session_id,
                "image_part_count": image_count,
                "provider": provider_id,
                "model": model_id,
                "supports_vision": False,
                "recovery_actions": [
                    "switch_to_openai_or_anthropic",
                    "remove_image_part",
                    "attach_image_as_context_file_for_tool_inspection",
                ],
            },
            recoverable=True,
        )
    )


def _current_lm_model_id() -> str:
    """Best-effort: which model the active dspy LM is bound to.

    Resolves through the ambient guard so that a read outside any per-profile
    ``dspy.context`` (e.g. turn-end metadata assembly) records a structured
    ``ambient_lm_default`` reason instead of silently depending on the process
    boot default (#818)."""
    from clio_agent.gact.runtime.ambient_lm import resolve_active_lm  # noqa: PLC0415

    lm = resolve_active_lm(site="app._current_lm_model_id")
    return getattr(lm, "model", "") if lm else ""
