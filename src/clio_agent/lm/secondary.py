"""Caller-relative identities for repair and summarization inference (#1322)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Literal
from urllib.parse import urlparse

from clio_agent import conf
from clio_agent.config import LMProviderConfig, create_chat_adapter
from clio_agent.providers.lm_spec import LMSpec, spec_from_config
from clio_agent.providers.resolver import resolve_endpoint_and_handshake

SecondaryRole = Literal["repairer", "summarizer"]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SecondarySettings:
    """One atomic configuration snapshot for a secondary invocation."""

    model: str = ""
    provider: str = ""
    api_base: str = ""
    credential_ref: str = ""
    transport: str = ""
    max_tokens: int | None = None


@dataclass(frozen=True)
class SecondaryLM:
    """One invocation's resolved LM and matching adapter."""

    lm: Any
    adapter: Any
    inherited: bool


def read_secondary_settings(role: SecondaryRole) -> SecondarySettings:
    """Snapshot a role's file/env configuration exactly once."""
    if role == "repairer":
        values = (
            conf.resolve("repairer.model", env="CLIO_REPAIRER_MODEL", default="", cast=conf.as_str),
            conf.resolve(
                "repairer.provider", env="CLIO_REPAIRER_PROVIDER", default="", cast=conf.as_str
            ),
            conf.resolve(
                "repairer.api_base", env="CLIO_REPAIRER_API_BASE", default="", cast=conf.as_str
            ),
            conf.resolve(
                "repairer.credential_ref",
                env="CLIO_REPAIRER_CREDENTIAL_REF",
                default="",
                cast=conf.as_str,
            ),
            conf.resolve(
                "repairer.transport", env="CLIO_REPAIRER_TRANSPORT", default="", cast=conf.as_str
            ),
            conf.resolve(
                "repairer.max_tokens",
                env="CLIO_REPAIRER_MAX_TOKENS",
                default=None,
                cast=conf.as_int,
            ),
        )
    else:
        values = (
            conf.resolve(
                "summarizer.model", env="CLIO_SUMMARIZER_MODEL", default="", cast=conf.as_str
            ),
            conf.resolve(
                "summarizer.provider", env="CLIO_SUMMARIZER_PROVIDER", default="", cast=conf.as_str
            ),
            conf.resolve(
                "summarizer.api_base", env="CLIO_SUMMARIZER_API_BASE", default="", cast=conf.as_str
            ),
            conf.resolve(
                "summarizer.credential_ref",
                env="CLIO_SUMMARIZER_CREDENTIAL_REF",
                default="",
                cast=conf.as_str,
            ),
            conf.resolve(
                "summarizer.transport",
                env="CLIO_SUMMARIZER_TRANSPORT",
                default="",
                cast=conf.as_str,
            ),
            conf.resolve(
                "summarizer.max_tokens",
                env="CLIO_SUMMARIZER_MAX_TOKENS",
                default=None,
                cast=conf.as_int,
            ),
        )
    cap = values[5]
    if cap is not None and cap < 0:
        raise ValueError(f"{role}.max_tokens must be non-negative")
    return SecondarySettings(
        model=str(values[0] or "").strip(),
        provider=str(values[1] or "").strip(),
        api_base=str(values[2] or "").strip(),
        credential_ref=str(values[3] or "").strip(),
        transport=str(values[4] or "").strip(),
        max_tokens=cap,
    )


def _overridden_spec(base: LMSpec, settings: SecondarySettings) -> LMSpec:
    provider_changed = bool(settings.provider and settings.provider != base.provider)
    provider = settings.provider or base.provider
    return replace(
        base,
        provider=provider,
        provider_id=provider if provider_changed else base.provider_id,
        model=settings.model or ("" if provider_changed else base.model),
        api_base=settings.api_base or ("" if provider_changed else base.api_base),
        credential_ref=settings.credential_ref or ("" if provider_changed else base.credential_ref),
        provider_options={} if provider_changed else dict(base.provider_options),
        transport=settings.transport or ("" if provider_changed else base.transport),
        max_tokens=base.max_tokens if settings.max_tokens is None else settings.max_tokens,
    )


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
    snapshot = settings if settings is not None else read_secondary_settings(role)
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
    same_provider = spec.provider == caller.provider
    resolved = resolve_endpoint_and_handshake(
        spec,
        default_credential=caller.api_key if same_provider and not spec.credential_ref else "",
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
    "read_secondary_settings",
    "resolve_secondary_lm",
]
