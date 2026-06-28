"""Runtime LM-provider credential helpers for the GACT server (#714).

This module owns the runtime credential helpers carved out of
``clio_agent.gact.app``:

* :func:`_is_placeholder_api_key` -- recognize the local no-auth placeholder keys
  (``""``/``x``/``EMPTY``/...) so a local provider is never treated as
  authenticated.
* :func:`_resolve_argonne_runtime_api_key` -- mint a fresh ALCF/Argonne bearer
  token for runtime provider use (delegates to the real provider auth).
* :func:`_refresh_argonne_lm_token` -- push a fresh ALCF token onto the live DSPy
  LM objects of an Argonne-backed agent so its short-lived token stays valid
  across a long turn.

The real Argonne/Globus auth lives in :mod:`clio_agent.providers`; this module is
the gact-server-side wiring that resolves a token and threads it onto the live
agent. Imports stay lazy/leaf so this module never loads ``gact.app``.
"""

from __future__ import annotations

from typing import Any


def _is_placeholder_api_key(value: str | None) -> bool:
    """Return whether an API key is a local no-auth placeholder."""

    return (value or "").strip() in {"", "x", "X", "EMPTY", "empty"}


def _resolve_argonne_runtime_api_key() -> str:
    """Return a fresh ALCF bearer token for runtime provider use."""

    from clio_agent.config import _resolve_argonne_api_key  # noqa: PLC0415

    token = _resolve_argonne_api_key()
    if not token:
        raise RuntimeError("ALCF Globus token is unavailable or could not be refreshed.")
    return token


def _refresh_argonne_lm_token(agent: Any) -> None:
    """Refresh Argonne's short-lived token on live DSPy LM objects."""

    cfg = getattr(agent, "_provider_config", None)
    if cfg is None or getattr(cfg, "provider", "") != "argonne":
        return
    token = _resolve_argonne_runtime_api_key()
    cfg.api_key = token
    for attr in ("_main_lm", "_planner_lm", "_router_lm"):
        lm = getattr(agent, attr, None)
        kwargs = getattr(lm, "kwargs", None)
        if isinstance(kwargs, dict):
            kwargs["api_key"] = token
