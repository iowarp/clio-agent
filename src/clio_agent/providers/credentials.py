"""Keyed, read-only credential resolution for LM providers.

This is the owner module for turning a *credential reference* (a key, never
an inline secret) into an actual ``api_key``. It is deliberately read-only:
``resolve`` computes a value **fresh per call and returns it** — it NEVER
writes ``os.environ`` or any process-global state. That property is what makes
per-expert / per-node credential resolution safe under concurrency (design:
``docs/archive/per-expert-provider-lm.md`` §3.2): N experts on N providers can
resolve independently with zero contention and no shared mutable global.

Backends, keyed by the ``credential_ref``:

* **Default cloud ref** (``""`` / ``"<provider>:default"``) — reads the
  well-known env var ``_CLOUD_API_KEY_ENV[provider]`` (e.g. ``OPENAI_API_KEY``),
  read-only, exactly as ``LMProviderConfig.__post_init__`` did before this
  module existed. This preserves the baseline for a GACT booted from a
  provider key env var.
* **Named ref** (``"<provider>:<account>"``) — reads a distinct per-account
  source ``CLIO_CRED_<PROVIDER>_<ACCOUNT>`` so two experts on the same provider
  but different accounts each get their own key. This is the case that is
  impossible with a single process-global env var.
* **Argonne ref** — mints / looks up a Globus bearer token (lazy,
  non-interactive) via the ``argonne_auth`` machinery. Routed through the
  ``clio_agent.config._resolve_argonne_api_key`` seam so the runtime-refresh
  path and existing monkeypatch points keep working.
* **Missing ref on a node** — returns ``""``. The downstream LM call surfaces
  an actionable, structured auth error; there is **no silent fallback** to the
  default provider (design §3.2, no-silent-fallback ground rule).
"""

from __future__ import annotations

import os
import re

from clio_agent.providers.catalog import as_cloud_api_key_env

# Environment variable names for the well-known per-provider cloud API keys.
# Sourced from the provider registry (kind defaults) — the same mapping
# ``config._CLOUD_API_KEY_ENV`` is built from, kept here so this module does
# not import ``config`` at load time (avoids an import cycle: ``config``
# imports this module).
_CLOUD_API_KEY_ENV: dict[str, str] = as_cloud_api_key_env()

# Prefix for the per-account named-credential env vars. A ref
# ``"openai:acctB"`` reads ``CLIO_CRED_OPENAI_ACCTB``.
_NAMED_CRED_ENV_PREFIX = "CLIO_CRED_"


def _account_of(credential_ref: str) -> str:
    """Return the per-account label a ref names, or ``""`` for the default ref.

    ``""`` and ``"<anything>:default"`` (and a bare ``"default"``) all denote
    the default credential for the provider. ``"<provider>:<account>"`` yields
    ``<account>``; a bare non-empty label with no colon is itself treated as the
    account name.
    """
    ref = (credential_ref or "").strip()
    if not ref or ref.lower() == "default":
        return ""
    account = ref.split(":", 1)[1].strip() if ":" in ref else ref
    if account.lower() in ("", "default"):
        return ""
    return account


def _named_account_env_var(provider: str, account: str) -> str:
    """Build the env var name for a ``<provider>:<account>`` named credential."""
    sanitized = re.sub(r"[^A-Z0-9]", "_", f"{provider}_{account}".upper())
    return f"{_NAMED_CRED_ENV_PREFIX}{sanitized}"


def resolve_argonne_token() -> str:
    """Return a Globus bearer token for the ALCF inference gateway.

    Two escape hatches before we touch globus-sdk:

    1. ``CLIO_ARGONNE_TOKEN`` — explicit override. Set by automation that
       already has a token (e.g. a parent agent that ran
       ``argonne_auth.get_access_token`` and exports the result). The ALCF
       ecosystem's own ``ALCF_INFERENCE_TOKEN`` is also accepted, since users
       often already have one exported. (A bare, un-namespaced ``access_token``
       is deliberately NOT read — it is far too generic to claim as an ALCF
       bearer and risked hijacking an unrelated process env var.)
    2. Otherwise go through ``providers.argonne_auth``. The import is deferred
       so ``globus-sdk`` is only required when this provider is actually
       selected.

    Read-only and non-interactive: no stored token → return ``""`` and let the
    upstream validator emit the actionable "run authenticate" message. Never
    triggers an interactive OAuth flow (this runs from ``/health``, ``/doctor``
    and TUI introspection where blocking on a browser would be hostile).
    """
    override = (
        os.environ.get("CLIO_ARGONNE_TOKEN", "").strip()
        or os.environ.get("ALCF_INFERENCE_TOKEN", "").strip()
    )
    if override:
        return override

    try:
        from clio_agent.providers.argonne_auth import (  # noqa: PLC0415
            GlobusUnavailable,
            get_access_token,
            tokens_exist,
        )
    except Exception:  # pragma: no cover - import-time error  # noqa: BLE001 - import-time failure yields an empty token
        return ""

    # No stored token → surface "" (don't drive interactive OAuth from here).
    if not tokens_exist():
        return ""

    try:
        # Passive probe: an expired refresh token must raise GlobusAuthError
        # (reason=argonne_login_required) instead of blocking server boot /
        # health / doctor on an interactive Globus login.
        return get_access_token(allow_interactive=False)
    except GlobusUnavailable:
        # Logged elsewhere; let the downstream error message guide the user.
        return ""
    except Exception:  # noqa: BLE001 - OAuth failure yields empty token; downstream 401 guides the user
        # OAuth could not complete (network, refresh expired, login required).
        # Returning "" lets the LM call fail with a clean 401 rather than
        # masking it behind config-load tracebacks.
        return ""


def resolve(provider: str, credential_ref: str = "") -> str:
    """Resolve the ``api_key`` for ``provider`` under ``credential_ref``.

    Computed fresh and returned; never writes ``os.environ``. See the module
    docstring for the backend keyed by each ref shape. An unknown / missing ref
    returns ``""`` (the LM call surfaces the actionable auth error).

    Args:
        provider: Runtime provider kind (``"openai"``, ``"argonne"``, ...).
        credential_ref: The credential reference (a key, never a secret). Empty
            or ``"<provider>:default"`` selects the default credential.

    Returns:
        The resolved API key / bearer token, or ``""`` when none is available.
    """
    account = _account_of(credential_ref)

    # Argonne mints/looks up ONE node-local Globus token — it is account-agnostic
    # (``resolve_argonne_token`` / ``_resolve_argonne_api_key`` take no account).
    # So it may only answer the DEFAULT ref. A NAMED (non-default) argonne ref has
    # no per-account backend, so returning the default identity would silently
    # authenticate the expert as the wrong account (finding #5). Gate on the
    # default account and surface "" for a named ref instead — the LM call then
    # raises an actionable auth error (no silent default identity, design §3.2).
    if provider == "argonne":
        if account:
            return ""
        from clio_agent import config as _config  # noqa: PLC0415

        return _config._resolve_argonne_api_key()

    if account:
        # Named per-account credential: a distinct, keyed source. No fallback
        # to the default env var — a missing named credential returns "".
        return os.environ.get(_named_account_env_var(provider, account), "")

    # Default cloud ref: the well-known per-provider env var, read-only.
    env_var = _CLOUD_API_KEY_ENV.get(provider)
    if env_var:
        return os.environ.get(env_var, "")
    return ""


class CredentialResolver:
    """Read-only, keyed credential resolver (design §3.2).

    A thin, injectable object around :func:`resolve` so the per-expert
    resolution path can carry a resolver as data (and future backends can
    override it) without any call site reaching for process-global state.
    Resolution is stateless and computed fresh per call.
    """

    def resolve(self, provider: str, credential_ref: str = "") -> str:
        """Resolve the ``api_key`` for ``provider`` under ``credential_ref``."""
        return resolve(provider, credential_ref)
