"""Typed provider-catalog records independent of catalog data and views."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProviderKind = Literal[
    "lm_studio",
    "ollama",
    "openai",
    "anthropic",
    "argonne",
    "codex",
    "claude_code",
]
AuthMethod = Literal["none", "api_key", "oauth"]


@dataclass(frozen=True)
class ProviderConfigurationField:
    """One non-secret provider option rendered by provider clients."""

    id: str
    label: str
    description: str = ""
    placeholder: str = ""
    required: bool = False


@dataclass(frozen=True)
class ModelEntry:
    """One row in a provider's static model catalog.

    Used as the fallback when live discovery against the upstream
    ``/v1/models`` endpoint fails or isn't supported.
    """

    id: str
    name: str
    description: str = ""


@dataclass(frozen=True)
class Provider:
    """One LM provider preset.

    Carries both the catalog metadata that gact's provider modal renders
    and the wire defaults that ``LMProviderConfig`` falls back to when
    the user leaves api_base / model blank.
    """

    id: str
    label: str
    description: str
    provider_kind: ProviderKind
    litellm_prefix: str
    api_base: str
    suggested_model: str
    api_key_default: str = ""
    requires_api_key: bool = True
    auth_method: AuthMethod = "api_key"
    api_key_env: str | None = None
    supports_live_catalog: bool = True
    supports_vision: bool = False
    max_tokens_default: int = 32000
    strip_openai_prefix: bool = True
    parse_retry_capability: Literal["bounded", "single_attempt"] = "bounded"
    configuration_fields: tuple[ProviderConfigurationField, ...] = ()
    supports_runtime_sizing: bool = False
    managed_service_id: str = ""
    is_kind_default: bool = False
    model_catalog: tuple[ModelEntry, ...] = ()
