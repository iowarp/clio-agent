"""Single source of truth for the LM provider catalog.

Every piece of provider metadata in clio-agent lives here:

- Wire defaults that ``LMProviderConfig.__post_init__`` reads (api_base,
  model, max_tokens, ``strip_openai_prefix``) - surfaced via
  :func:`as_provider_defaults_dict`.
- Catalog rows that ``GET /v1/providers/lm`` returns to gact's provider
  modal (label, description, requires_api_key) - surfaced via
  :func:`as_lm_presets`.
- Per-preset model catalogs used as the static fallback for
  ``GET /v1/providers/{id}/models`` - surfaced via
  :func:`as_provider_models_dict`.

No other module owns provider data. Adding a new provider = one new
:class:`Provider` entry in :data:`PROVIDERS`; the derived views update
automatically and the gact modal picks the new preset up at the next
``GET /v1/providers/lm``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

#: Wire-level provider kinds. These are the values that flow into
#: ``LMProviderConfig.provider`` and ultimately into the LiteLLM model
#: prefix (``openai/``, ``anthropic/``...). Catalog ids are usually a
#: superset (e.g. ``openrouter`` and ``openai`` both have
#: ``provider_kind="openai"``).
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

    # ----- catalog identity -------------------------------------------
    id: str
    label: str
    description: str

    # ----- wire kind --------------------------------------------------
    #: Drives ``LMProviderConfig.provider``. Multiple catalog entries can
    #: share a kind (e.g. openrouter and openai both share
    #: ``openai``); the entry flagged ``is_kind_default`` supplies the
    #: dict row in :func:`as_provider_defaults_dict`.
    provider_kind: ProviderKind
    #: Exact LiteLLM routing prefix.  This is deliberately independent of
    #: ``provider_kind`` so legacy configurations keep their broad runtime kind
    #: while catalog selections such as OpenRouter retain their real identity.
    litellm_prefix: str

    # ----- wire defaults ----------------------------------------------
    api_base: str
    suggested_model: str
    api_key_default: str = ""

    # ----- auth -------------------------------------------------------
    requires_api_key: bool = True
    auth_method: AuthMethod = "api_key"
    #: Env var that ``LMProviderConfig.__post_init__`` falls back to for
    #: cloud providers (e.g. ``"OPENAI_API_KEY"``). ``None`` for local /
    #: OAuth flows.
    api_key_env: str | None = None
    supports_live_catalog: bool = True
    supports_vision: bool = False

    # ----- capability flags -------------------------------------------
    max_tokens_default: int = 32000
    #: Whether to strip ``openai/`` / ``anthropic/`` LiteLLM prefixes
    #: before sending to the upstream model id. ``False`` only for
    #: HuggingFace-id backends like ALCF where ``openai/gpt-oss-120b``
    #: *is* the literal model id.
    strip_openai_prefix: bool = True
    parse_retry_capability: Literal["bounded", "single_attempt"] = "bounded"
    configuration_fields: tuple[ProviderConfigurationField, ...] = ()
    supports_runtime_sizing: bool = False
    managed_service_id: str = ""

    # ----- registry bookkeeping ---------------------------------------
    #: When True, this provider's wire fields populate the
    #: per-``provider_kind`` row returned by
    #: :func:`as_provider_defaults_dict`. Exactly one entry per kind
    #: should be flagged; multiple flags collapse to the first.
    is_kind_default: bool = False

    # ----- static model catalog ---------------------------------------
    model_catalog: tuple[ModelEntry, ...] = ()


# -- shared catalogs --------------------------------------------------

#: Argonne ALCF hosted model families. Sophia / Metis / local_vllm run
#: behind dynamic gateway jobs; the live ``/jobs`` endpoint reports the
#: actual subset loaded right now. Keep this static catalog as a
#: fallback/example list, not as a claim of current availability.
_ARGONNE_MODELS: tuple[ModelEntry, ...] = (
    ModelEntry(
        "openai/gpt-oss-120b",
        "GPT-OSS 120B (Sophia)",
        "Preferred modern Sophia baseline when loaded; verify with live discovery.",
    ),
    ModelEntry(
        "openai/gpt-oss-20b",
        "GPT-OSS 20B (Sophia)",
        "Lower-latency GPT-OSS option when loaded on Sophia.",
    ),
    ModelEntry(
        "gpt-oss-120b",
        "GPT-OSS 120B (Metis)",
        "Preferred modern Metis baseline when loaded.",
    ),
    ModelEntry(
        "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
        "Llama 4 Maverick",
        "Modern Llama 4 fallback; availability depends on running ALCF jobs.",
    ),
    ModelEntry(
        "meta-llama/Llama-4-Scout-17B-16E-Instruct",
        "Llama 4 Scout",
        "Modern Llama 4 fallback; useful when GPT-OSS is unavailable.",
    ),
    ModelEntry(
        "meta-llama/Meta-Llama-3.1-8B-Instruct",
        "Llama 3.1 8B Instruct (legacy)",
        "Legacy compatibility entry; prefer GPT-OSS or Llama 4 when available.",
    ),
    ModelEntry(
        "meta-llama/Meta-Llama-3.1-70B-Instruct",
        "Llama 3.1 70B Instruct (legacy)",
        "Legacy compatibility entry; not recommended as the default baseline.",
    ),
    ModelEntry(
        "meta-llama/Meta-Llama-3.1-405B-Instruct",
        "Llama 3.1 405B Instruct (legacy)",
        "Legacy compatibility entry; often offline, check active models first.",
    ),
    ModelEntry(
        "mistralai/Mistral-7B-Instruct-v0.3",
        "Mistral 7B Instruct v0.3",
        "Lightweight legacy fallback; check active models first.",
    ),
)


# -- the catalog ------------------------------------------------------

#: Canonical provider list. Ordered roughly local-first -> cloud -> ALCF
#: so gact's modal lists them in a sensible order.
PROVIDERS: tuple[Provider, ...] = (
    # ----- local-only ------------------------------------------------
    Provider(
        id="lm_studio",
        label="LM Studio (localhost)",
        description=(
            "Locally-hosted models via LM Studio. Clear the model "
            "field to auto-discover the loaded model from "
            "/v1/models."
        ),
        provider_kind="lm_studio",
        litellm_prefix="openai",
        api_base="http://127.0.0.1:1234/v1",
        # Empty means "discover the loaded model from LM Studio".
        # Hardcoding a local model id here makes the setup stale as
        # soon as the user unloads that model.
        suggested_model="",
        api_key_default="lm-studio",
        requires_api_key=False,
        auth_method="none",
        supports_runtime_sizing=True,
        is_kind_default=True,
        model_catalog=(
            ModelEntry(
                "",
                "(auto-discovered)",
                "LM Studio reports the loaded model on /v1/models.",
            ),
        ),
    ),
    Provider(
        id="ollama",
        label="Ollama (localhost)",
        description="Locally-hosted models via Ollama.",
        provider_kind="ollama",
        litellm_prefix="ollama_chat",
        api_base="http://127.0.0.1:11434/v1",
        suggested_model="granite3.1-dense:8b",
        api_key_default="ollama",
        requires_api_key=False,
        auth_method="none",
        supports_runtime_sizing=True,
        is_kind_default=True,
        model_catalog=(
            ModelEntry(
                "granite3.1-dense:8b",
                "Granite 3.1 Dense 8B",
                "Default Ollama model in clio. Local; matches the wire default.",
            ),
            ModelEntry("llama3.2", "Llama 3.2", "Lightweight, broadly available."),
            ModelEntry(
                "qwen2.5-coder:14b",
                "Qwen2.5 Coder 14B",
                "Better at code than llama3.2; same speed band.",
            ),
        ),
    ),
    Provider(
        id="llama_cpp",
        label="llama.cpp server",
        description="A local or remote llama.cpp OpenAI-compatible server.",
        provider_kind="openai",
        litellm_prefix="openai",
        api_base="http://127.0.0.1:8088/v1",
        suggested_model="local-model",
        api_key_default="llama-cpp",
        requires_api_key=False,
        auth_method="none",
        supports_runtime_sizing=True,
        managed_service_id="llama_cpp",
        model_catalog=(
            ModelEntry("local-model", "Loaded GGUF model", "The model served by llama.cpp."),
        ),
    ),
    # ----- cloud / proxy ---------------------------------------------
    Provider(
        id="openai",
        label="OpenAI / ChatGPT",
        description=(
            "Direct OpenAI API. Requires "
            "an OPENAI_API_KEY. Defaults to gpt-4o-mini for low cost; "
            "swap in gpt-4o or gpt-4-turbo for heavier work."
        ),
        provider_kind="openai",
        litellm_prefix="openai",
        api_base="https://api.openai.com/v1",
        suggested_model="gpt-4o-mini",
        api_key_env="OPENAI_API_KEY",
        is_kind_default=True,
        supports_vision=True,
        model_catalog=(
            ModelEntry(
                "gpt-4o-mini",
                "GPT-4o mini",
                "OpenAI's cheap fast model. Good default.",
            ),
            ModelEntry(
                "gpt-4o",
                "GPT-4o",
                "OpenAI's flagship multimodal model.",
            ),
            ModelEntry(
                "gpt-4-turbo",
                "GPT-4 Turbo",
                "Higher capability, slower + pricier.",
            ),
        ),
    ),
    Provider(
        id="anthropic",
        label="Anthropic API",
        description="Direct Anthropic API. Requires an ANTHROPIC_API_KEY.",
        provider_kind="anthropic",
        litellm_prefix="anthropic",
        api_base="https://api.anthropic.com/v1",
        suggested_model="claude-sonnet-4-20250514",
        api_key_env="ANTHROPIC_API_KEY",
        is_kind_default=True,
        supports_vision=True,
        model_catalog=(
            ModelEntry(
                "claude-haiku-4-5-20251001",
                "Claude Haiku 4.5",
                "Direct Anthropic. Fast + cheap.",
            ),
            ModelEntry(
                "claude-sonnet-4-6-20251001",
                "Claude Sonnet 4.6",
                "Direct Anthropic. Balanced.",
            ),
            ModelEntry(
                "claude-opus-4-6-20251001",
                "Claude Opus 4.6",
                "Direct Anthropic. Highest capability.",
            ),
        ),
    ),
    Provider(
        id="azure_openai",
        label="Azure OpenAI",
        description="Azure-hosted OpenAI models through LiteLLM.",
        provider_kind="openai",
        litellm_prefix="azure",
        api_base="https://YOUR-RESOURCE.openai.azure.com/",
        suggested_model="",
        api_key_env="AZURE_API_KEY",
        supports_live_catalog=False,
        configuration_fields=(
            ProviderConfigurationField(
                "api_version",
                "API version",
                "Azure OpenAI API version.",
                "2024-10-21",
                True,
            ),
        ),
    ),
    Provider(
        id="gemini",
        label="Google Gemini",
        description="Gemini AI Studio models through LiteLLM.",
        provider_kind="openai",
        litellm_prefix="gemini",
        api_base="https://generativelanguage.googleapis.com/v1beta",
        suggested_model="gemini-2.5-flash",
        api_key_env="GOOGLE_API_KEY",
        supports_live_catalog=False,
        supports_vision=True,
        model_catalog=(
            ModelEntry("gemini-2.5-flash", "Gemini 2.5 Flash", "Fast Gemini baseline."),
        ),
    ),
    Provider(
        id="vertex_ai",
        label="Google Vertex AI",
        description="Vertex AI models using Application Default Credentials.",
        provider_kind="openai",
        litellm_prefix="vertex_ai",
        api_base="https://aiplatform.googleapis.com",
        suggested_model="gemini-2.5-flash",
        requires_api_key=False,
        auth_method="none",
        supports_live_catalog=False,
        supports_vision=True,
        configuration_fields=(
            ProviderConfigurationField(
                "vertex_project",
                "Google Cloud project",
                "Project containing the model.",
                required=True,
            ),
            ProviderConfigurationField(
                "vertex_location", "Google Cloud location", "Vertex region.", "us-central1", True
            ),
        ),
        model_catalog=(
            ModelEntry("gemini-2.5-flash", "Gemini 2.5 Flash", "Vertex-hosted Gemini model."),
        ),
    ),
    Provider(
        id="bedrock",
        label="AWS Bedrock",
        description="Bedrock models using the host AWS credential chain.",
        provider_kind="openai",
        litellm_prefix="bedrock",
        api_base="https://bedrock-runtime.us-east-1.amazonaws.com",
        suggested_model="anthropic.claude-3-5-sonnet-20240620-v1:0",
        requires_api_key=False,
        auth_method="none",
        supports_live_catalog=False,
        configuration_fields=(
            ProviderConfigurationField(
                "aws_region_name", "AWS region", "Region containing the model.", "us-east-1", True
            ),
            ProviderConfigurationField(
                "aws_profile_name", "AWS profile", "Optional shared AWS profile name."
            ),
        ),
        model_catalog=(
            ModelEntry(
                "anthropic.claude-3-5-sonnet-20240620-v1:0",
                "Claude 3.5 Sonnet",
                "Bedrock model identifier.",
            ),
        ),
    ),
    Provider(
        id="nvidia_nim",
        label="NVIDIA NIM",
        description="NVIDIA-hosted or self-hosted NIM endpoints through LiteLLM.",
        provider_kind="openai",
        litellm_prefix="nvidia_nim",
        api_base="https://integrate.api.nvidia.com/v1",
        suggested_model="meta/llama-3.1-70b-instruct",
        api_key_env="NVIDIA_NIM_API_KEY",
        supports_live_catalog=False,
        model_catalog=(
            ModelEntry(
                "meta/llama-3.1-70b-instruct", "Llama 3.1 70B Instruct", "NVIDIA NIM model."
            ),
        ),
    ),
    Provider(
        id="openrouter",
        label="OpenRouter",
        description=(
            "OpenAI-compatible gateway over many providers. Free tier "
            "models (suffixed :free) work without spend but are "
            "heavily rate-limited."
        ),
        provider_kind="openai",
        litellm_prefix="openrouter",
        api_base="https://openrouter.ai/api/v1",
        suggested_model="openai/gpt-oss-120b:free",
        api_key_env="OPENROUTER_API_KEY",
        model_catalog=(
            ModelEntry(
                "openai/gpt-oss-120b:free",
                "GPT-OSS 120B (free)",
                "Free tier. Heavily rate-limited.",
            ),
            ModelEntry(
                "anthropic/claude-haiku-4-5",
                "Claude Haiku 4.5 via OpenRouter",
                "Pay-per-token via OpenRouter.",
            ),
            ModelEntry(
                "anthropic/claude-sonnet-4-6",
                "Claude Sonnet 4.6 via OpenRouter",
                "Pay-per-token via OpenRouter.",
            ),
        ),
    ),
    Provider(
        id="codex",
        label="OpenAI Codex (subscription)",
        description=(
            "Uses the official OpenAI Codex Python SDK so calls reuse "
            "your ChatGPT / Codex subscription instead of paying "
            "per-token on the OpenAI API. Authenticate Codex once on "
            "this machine; the SDK owns its pinned runtime."
        ),
        provider_kind="codex",
        litellm_prefix="codex",
        # Codex does not use an HTTP base. This is an identity marker only;
        # the official Python SDK owns its pinned runtime.
        api_base="codex://sdk",
        suggested_model="gpt-5.5",
        requires_api_key=False,
        auth_method="none",
        is_kind_default=True,
        supports_vision=True,
        supports_live_catalog=False,
        parse_retry_capability="single_attempt",
        model_catalog=(
            ModelEntry(
                "gpt-5.5",
                "GPT-5.5 (via Codex)",
                "Candidate Codex SDK model id; not guaranteed by account entitlement.",
            ),
            ModelEntry(
                "gpt-5.5-codex",
                "GPT-5.5 Codex",
                "Candidate Codex-tuned model id; not guaranteed by account entitlement.",
            ),
            ModelEntry(
                "gpt-5.1",
                "GPT-5.1 (via Codex)",
                "Fallback candidate model id; not guaranteed by account entitlement.",
            ),
        ),
    ),
    Provider(
        id="claude_code",
        label="Claude Code (subscription)",
        description=(
            "Uses the Claude Agent SDK with Claude Code subscription auth "
            "instead of direct Anthropic API keys. Authenticate Claude Code "
            "once on this machine; the SDK owns the model session and "
            "streaming lifecycle."
        ),
        provider_kind="claude_code",
        litellm_prefix="claude_code",
        api_base="claude-code://sdk",
        suggested_model="sonnet",
        requires_api_key=False,
        auth_method="none",
        is_kind_default=True,
        supports_vision=True,
        supports_live_catalog=False,
        # "fable" is the CLI's own current default alias (verified live
        # 2026-08-14: a bare `claude -p` call with no --model resolves to
        # claude-fable-5) -- listed first so a fresh install (before any
        # POST /v1/providers/models/refresh, #1211) already shows it.
        #
        # CORRECTION (#1211 review D4): an EARLIER version of this comment
        # claimed adding "fable" here caused a real per-bind network-timeout
        # cost because models.dev had never indexed it -- that mechanism is
        # FALSE. Verified: providers.handshake.sources.models_dev fetches the
        # WHOLE models.dev catalog ONCE and caches it to disk with a 24h TTL;
        # a per-id lookup (`lookup_models_dev`) only re-attempts the network
        # fetch when that ON-DISK CACHE is stale/absent -- true for ANY model
        # id, novel or not, and true on every isolated test run (a fresh
        # CLIO_USER_DIR per test never has a warm cache). The
        # test_lm_provider.py timing failure that prompted the original
        # (wrong) revert reproduces IDENTICALLY with "fable" absent -- it is
        # pre-existing cache-staleness cost the CLI-provider cascade already
        # paid for haiku/sonnet/opus, not something this entry adds. The REAL
        # fix is structural: model_discovery.attach_context_limits resolves +
        # persists each discovered model's context/output limit ONCE, at
        # explicit refresh time, and CliCatalogHandshake reads it back
        # pre-filled (skipping the cascade entirely on every later passive
        # call) -- see providers/handshake/cli_catalog.py.
        model_catalog=(
            ModelEntry(
                "fable",
                "Claude Fable (Claude Code alias)",
                "Candidate Claude Code alias; not guaranteed by account entitlement.",
            ),
            ModelEntry(
                "haiku",
                "Claude Haiku (Claude Code alias)",
                "Candidate Claude Code alias; not guaranteed by account entitlement.",
            ),
            ModelEntry(
                "sonnet",
                "Claude Sonnet (Claude Code alias)",
                "Candidate Claude Code alias; not guaranteed by account entitlement.",
            ),
            ModelEntry(
                "opus",
                "Claude Opus (Claude Code alias)",
                "Candidate Claude Code alias; not guaranteed by account entitlement.",
            ),
        ),
    ),
    # ----- argonne ALCF ----------------------------------------------
    # NB: api_key for argonne presets is resolved lazily via
    # providers.argonne_auth (Globus OAuth), not from the registry.
    # api_key_default stays empty; the auth machinery kicks in inside
    # LMProviderConfig.__post_init__.
    Provider(
        id="argonne_sophia",
        label="ALCF Sophia (Globus Auth)",
        description=(
            "Argonne's Sophia inference gateway (vLLM, OpenAI-"
            "compatible). Auth is a Globus access token minted on "
            "demand from the user's anl.gov / alcf.anl.gov identity. "
            "Run `python -m clio_agent.providers.argonne_auth "
            "authenticate` once per machine; tokens auto-refresh."
        ),
        provider_kind="argonne",
        litellm_prefix="hosted_vllm",
        api_base="https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1",
        suggested_model="openai/gpt-oss-120b",
        requires_api_key=False,
        auth_method="oauth",
        max_tokens_default=4096,
        strip_openai_prefix=False,
        is_kind_default=True,
        model_catalog=_ARGONNE_MODELS,
    ),
    Provider(
        id="argonne_metis",
        label="ALCF Metis (Globus Auth)",
        description=(
            "Argonne's Metis inference gateway (FastCoE 'api' "
            "framework, OpenAI-compatible chat-completions). Useful "
            "fallback when Sophia is in maintenance; typically loads "
            "gpt-oss-120b and Llama-4-Maverick. Same Globus tokens as "
            "Sophia/Polaris."
        ),
        provider_kind="argonne",
        litellm_prefix="hosted_vllm",
        # Metis hangs framework="api" off /api/v1, not /vllm/v1 the way
        # Sophia does. Same Globus auth, same /jobs schema for live
        # model discovery, different chat-completions path.
        api_base="https://inference-api.alcf.anl.gov/resource_server/metis/api/v1",
        suggested_model="gpt-oss-120b",
        requires_api_key=False,
        auth_method="oauth",
        max_tokens_default=4096,
        strip_openai_prefix=False,
        model_catalog=_ARGONNE_MODELS,
    ),
    Provider(
        id="vllm",
        label="vLLM (localhost)",
        description=(
            "Any local OpenAI-compatible vLLM server. No Globus needed; "
            "the server commonly accepts the literal 'EMPTY' API key. "
            "Override api_base with the bound port."
        ),
        # Local vLLM is OpenAI-compatible (not Argonne's gateway-quirky
        # path), so the wire kind is plain openai.
        provider_kind="openai",
        litellm_prefix="hosted_vllm",
        api_base="http://127.0.0.1:8000/v1",
        suggested_model="meta-llama/Llama-3.1-8B-Instruct",
        requires_api_key=False,
        auth_method="none",
        supports_runtime_sizing=True,
        managed_service_id="vllm",
        model_catalog=_ARGONNE_MODELS,
    ),
)


# -- lookup helpers ---------------------------------------------------


def get_provider(provider_id: str) -> Provider | None:
    """Return the :class:`Provider` matching ``provider_id``, or None."""
    provider_id = {"argonne_local_vllm": "vllm"}.get(provider_id, provider_id)
    for p in PROVIDERS:
        if p.id == provider_id:
            return p
    return None


def normalize_provider_options(provider_id: str, options: dict[str, Any]) -> dict[str, str]:
    """Validate and normalize the non-secret LiteLLM options for a provider."""

    provider = get_provider(provider_id)
    if provider is None:
        if options:
            raise ValueError(f"unknown provider id: {provider_id}")
        return {}
    fields = {field.id: field for field in provider.configuration_fields}
    unknown = sorted(set(options) - set(fields))
    if unknown:
        raise ValueError(f"unsupported options for {provider.id}: {', '.join(unknown)}")
    normalized: dict[str, str] = {}
    for key, raw_value in options.items():
        value = str(raw_value).strip()
        if len(value) > 512 or "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"invalid value for provider option: {key}")
        if value:
            normalized[key] = value
    missing = [
        field.id for field in fields.values() if field.required and not normalized.get(field.id)
    ]
    if missing:
        raise ValueError(f"missing required options for {provider.id}: {', '.join(missing)}")
    return normalized


def iter_providers() -> tuple[Provider, ...]:
    """All provider entries in registration order."""
    return PROVIDERS


def kind_default(kind: str) -> Provider | None:
    """Return the kind-default :class:`Provider` for a wire kind, or None."""
    for p in PROVIDERS:
        if p.provider_kind == kind and p.is_kind_default:
            return p
    return None


def provider_defaults(provider: Provider) -> dict[str, Any]:
    """Return the legacy runtime defaults represented by one catalog row."""

    entry: dict[str, Any] = {
        "api_base": provider.api_base,
        "model": provider.suggested_model,
        "api_key": provider.api_key_default,
        "supports_vision": provider.supports_vision,
    }
    if provider.max_tokens_default != 32000:
        entry["max_tokens"] = provider.max_tokens_default
    if not provider.strip_openai_prefix:
        entry["strip_openai_prefix"] = False
    if provider.parse_retry_capability != "bounded":
        entry["parse_retry_capability"] = provider.parse_retry_capability
    return entry


# -- derived legacy views ---------------------------------------------


def as_provider_defaults_dict() -> dict[str, dict[str, Any]]:
    """Build the legacy ``PROVIDER_DEFAULTS`` dict keyed by provider_kind.

    ``LMProviderConfig.__post_init__`` reads this to fill empty wire
    fields. Exactly one row per kind, supplied by the
    ``is_kind_default`` :class:`Provider` for that kind.
    """
    out: dict[str, dict[str, Any]] = {}
    for p in PROVIDERS:
        if not p.is_kind_default:
            continue
        if p.provider_kind in out:
            continue  # first wins; defensive against duplicate flags
        out[p.provider_kind] = provider_defaults(p)
    return out


def as_cloud_api_key_env() -> dict[str, str]:
    """Build the legacy ``_CLOUD_API_KEY_ENV`` mapping.

    Maps each provider_kind that has an ``api_key_env`` on its
    kind-default to that env var name. Used by ``__post_init__`` to
    fill ``api_key`` from the process environment when the wire field
    is blank.
    """
    out = {p.id: p.api_key_env for p in PROVIDERS if p.api_key_env}
    out.update(
        {p.provider_kind: p.api_key_env for p in PROVIDERS if p.is_kind_default and p.api_key_env}
    )
    return out


def as_lm_presets() -> list[Any]:
    """Build the gact ``_LM_PRESETS`` list.

    Imports the Pydantic model lazily so that importing the registry
    doesn't pull in fastapi/uvicorn. Callers in ``gact/app.py`` are
    inside ``build_app()`` where fastapi is already loaded.
    """
    from clio_agent.gact.types import (  # noqa: PLC0415
        LMProviderConfigurationField,
        LMProviderPreset,
    )

    return [
        LMProviderPreset(
            id=p.id,
            provider_id=p.id,
            label=p.label,
            provider=p.provider_kind,
            litellm_prefix=p.litellm_prefix,
            api_base=p.api_base,
            suggested_model=p.suggested_model,
            requires_api_key=p.requires_api_key,
            api_key_env=p.api_key_env or "",
            auth_method=p.auth_method,
            is_authenticated=p.auth_method == "none",
            description=p.description,
            supports_live_catalog=p.supports_live_catalog,
            supports_vision=p.supports_vision,
            configuration_fields=[
                LMProviderConfigurationField(
                    id=field.id,
                    label=field.label,
                    description=field.description,
                    placeholder=field.placeholder,
                    required=field.required,
                )
                for field in p.configuration_fields
            ],
            supports_runtime_sizing=p.supports_runtime_sizing,
            managed_service_id=p.managed_service_id,
        )
        for p in PROVIDERS
    ]


def as_provider_models_dict() -> dict[str, list[dict[str, str]]]:
    """Build the gact ``_PROVIDER_MODELS`` static fallback dict.

    Keyed by both preset ``id`` (every entry) and ``provider_kind``
    (the kind default's catalog covers callers that look up by bare
    kind - e.g. ``GET /v1/providers/argonne/models``).
    """
    out: dict[str, list[dict[str, str]]] = {}
    for p in PROVIDERS:
        out[p.id] = [
            {"id": m.id, "name": m.name, "description": m.description} for m in p.model_catalog
        ]
    # Add bare-kind keys for providers whose preset id != kind.
    for p in PROVIDERS:
        if p.is_kind_default and p.provider_kind not in out:
            out[p.provider_kind] = out[p.id]
    out["argonne_local_vllm"] = out["vllm"]
    return out
