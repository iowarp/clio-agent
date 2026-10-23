"""Caller-relative identities for summarization inference (#1322)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Literal
from urllib.parse import urlparse

from clio_agent.config import LMProviderConfig, create_chat_adapter
from clio_agent.providers.lm_spec import LMSpec, spec_from_config
from clio_agent.providers.resolver import resolve_endpoint_and_handshake

SecondaryRole = Literal["summarizer"]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SecondarySettings:
    """A model-only override on the caller's own account.

    A role that runs on another account names it in ``fleet.judges``
    (:mod:`clio_agent.gact.fleet_settings`).
    """

    model: str = ""


@dataclass(frozen=True)
class SecondaryLM:
    """One invocation's resolved LM and matching adapter."""

    lm: Any
    adapter: Any
    inherited: bool


def _overridden_spec(base: LMSpec, settings: SecondarySettings) -> LMSpec:
    return replace(base, model=settings.model or base.model)


def resolve_secondary_lm(
    role: SecondaryRole,
    *,
    caller_lm: Any,
    caller_adapter: Any = None,
    settings: SecondarySettings | None = None,
    retry_temperature: float | None = None,
) -> SecondaryLM:
    """Resolve one secondary call relative to its effective caller."""
    if caller_lm is None:
        raise RuntimeError(f"{role} inference has no effective caller LM")
    snapshot = settings if settings is not None else SecondarySettings()
    if snapshot == SecondarySettings() and retry_temperature is None:
        config = getattr(caller_lm, "_clio_provider_config", None)
        logger.info(
            "secondary lm role=%s operation=inherited model=%s endpoint_host=%s",
            role,
            getattr(config, "model", getattr(caller_lm, "model", "")),
            urlparse(str(getattr(config, "api_base", "") or "")).hostname or "default",
        )
        return SecondaryLM(caller_lm, caller_adapter, True)
    caller = getattr(caller_lm, "_clio_provider_config", None)
    if not isinstance(caller, LMProviderConfig):
        raise RuntimeError(f"explicit {role} identity requires a CLIO-constructed caller LM")
    spec = _overridden_spec(spec_from_config(caller), snapshot)
    if retry_temperature is not None:
        spec = replace(spec, temperature=retry_temperature)
    resolved = resolve_endpoint_and_handshake(
        spec, default_credential=caller.api_key if not spec.credential_ref else ""
    )
    config = resolved.materialize()
    logger.info(
        "secondary lm role=%s operation=override model=%s endpoint_host=%s",
        role,
        config.model,
        urlparse(config.api_base).hostname or "default",
    )
    from clio_agent.lm.hooked_lm import create_hooked_lm  # noqa: PLC0415

    return SecondaryLM(create_hooked_lm(config), create_chat_adapter(config), False)


__all__ = [
    "SecondaryLM",
    "SecondaryRole",
    "SecondarySettings",
    "resolve_secondary_lm",
]
