"""Where the health probe's LM key comes from when no environment variable holds one."""

from __future__ import annotations

from collections.abc import Mapping


def fallback_key(env: Mapping[str, str], provider: str, default_key: str) -> tuple[str, str]:
    """Return ``(api_key, key_source)`` for a provider without an env key.

    A bound provider whose key lives in CLIO's credential store is marked by
    ``CLIO_LM_KEY_SOURCE=store:<provider_id>`` (F024). The probe never sends a
    key, so only its presence and source are recorded, never the secret.
    """

    stored = env.get("CLIO_LM_KEY_SOURCE", "")
    if stored.startswith("store:"):
        return "[stored]", stored
    return default_key, f"default:{provider}"
