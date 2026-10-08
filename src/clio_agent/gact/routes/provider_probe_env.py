"""Runtime provider inputs for the shared doctor probe."""

from __future__ import annotations

import os
from collections.abc import Mapping

from clio_agent import conf
from clio_agent.config import load_config_from_env


def runtime_provider_probe_env(live_lm: object) -> dict[str, str]:
    """The process environment with the provider CLIO actually uses overlaid.

    The live bound LM wins; with none bound yet (no agent built), the boot config the
    agent would be built from (``config.yaml`` -> ``CLIO_LM_*`` -> default). Probing the
    env-only default instead reported a Codex-configured server as an unreachable LM
    Studio (503). A boot config that does not load overlays its configured provider name,
    which the probe validates and reports as its typed MISCONFIGURED row.
    """

    env = dict(os.environ)
    lm = live_lm if isinstance(live_lm, Mapping) and live_lm.get("provider") else None
    if lm is None:
        try:
            boot = load_config_from_env()
        except ValueError:
            # The configured provider (file -> env), so the probe validates and reports it.
            env["CLIO_LM_PROVIDER"] = conf.resolve(
                "lm.provider", env="CLIO_LM_PROVIDER", default="lm_studio", cast=conf.as_str
            )
            return env
        lm = {"provider": boot.provider, "api_base": boot.api_base, "model": boot.model}
    provider_id = str(lm.get("provider_id") or "")
    if provider_id and _has_saved_key(provider_id):
        # A non-secret marker: the bound provider's key lives in CLIO's store (F024).
        env["CLIO_LM_KEY_SOURCE"] = f"store:{provider_id}"
    for key, value in (
        ("CLIO_LM_PROVIDER", lm.get("provider")),
        ("CLIO_LM_API_BASE", lm.get("api_base")),
        ("CLIO_LM_MODEL", lm.get("model")),
        ("CLIO_CODEX_VARIANT", lm.get("codex_variant")),
    ):
        if value is not None:
            env[key] = str(value)
    return env


def _has_saved_key(provider_id: str) -> bool:
    """Whether CLIO's credential store holds a key for this bound provider."""
    from clio_agent.providers.api_key_store import stored_api_key  # noqa: PLC0415

    return bool(stored_api_key(provider_id))
