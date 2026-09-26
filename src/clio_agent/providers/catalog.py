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

from typing import Any

from clio_agent.providers.catalog_types import (
    Provider,
    ProviderConfigurationField,
)
from clio_agent.providers.codex.constants import LITELLM_PROVIDER as _CODEX_LITELLM_PREFIX

# -- the catalog ------------------------------------------------------
#
# No static per-provider model lists or capability flags live here anymore
# (model-capabilities brief 9.1: the static `model_catalog` tuples --
# including the old `_ARGONNE_MODELS` fallback -- and the `supports_vision`/
# `max_tokens_default` flags are deleted). Model lists come from live
# discovery (`/v1/models`, a provider's `/jobs` endpoint, ...) or the
# community catalogs for clouds that can't list; when nothing is known the
# picker shows "enter a model id" rather than a stale compiled-in list.
# Vision/tool/thinking support comes from the effective capabilities
# (`providers.capabilities.accessor.get_effective_capabilities`, Part 5.5),
# never a static per-provider flag.

#: Canonical provider list. Ordered roughly local-first -> cloud -> ALCF
#: so gact's modal lists them in a sensible order.
PROVIDERS: tuple[Provider, ...] = (
    # ----- local-only ------------------------------------------------
    Provider(
        id="lm_studio",
        label="LM Studio",
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
    ),
    Provider(
        id="ollama",
        label="Ollama",
        description="Locally-hosted models via Ollama.",
        provider_kind="ollama",
        litellm_prefix="ollama_chat",
        # NOT "/v1": LiteLLM's native ollama_chat provider appends its own
        # /api/chat to this base (OllamaChatConfig.get_complete_url), so a
        # /v1 suffix here doubles into /v1/api/chat and 404s (#1413). The
        # discovery handshake still reaches the OpenAI-compatible /v1 shim by
        # going through native_root() the other way where it needs to.
        api_base="http://127.0.0.1:11434",
        # Empty means "discover the loaded model from Ollama's /api/tags".
        # A hardcoded id (#1413's own root cause family: a stale suggestion
        # papering over what discovery should answer) goes stale the moment
        # the user pulls a different model.
        suggested_model="",
        api_key_default="ollama",
        requires_api_key=False,
        auth_method="none",
        supports_runtime_sizing=True,
        is_kind_default=True,
    ),
    Provider(
        id="llama_cpp",
        label="llama.cpp server",
        description="A local or remote llama.cpp OpenAI-compatible server.",
        provider_kind="openai",
        litellm_prefix="openai",
        api_base="http://127.0.0.1:8088/v1",
        # Empty means "discover the loaded model from llama.cpp's /v1/models".
        # A hardcoded "local-model" id (#1418) was never what the server
        # actually serves, so it was never a usable message route.
        suggested_model="",
        api_key_default="llama-cpp",
        requires_api_key=False,
        auth_method="none",
        supports_runtime_sizing=True,
        managed_service_id="llama_cpp",
    ),
    # ----- cloud / proxy ---------------------------------------------
    Provider(
        id="openai",
        label="OpenAI API",
        description=(
            "Direct OpenAI API. Requires "
            "an OPENAI_API_KEY. Defaults to gpt-4o-mini for low cost; "
            "swap in gpt-4o or gpt-4-turbo for heavier work."
        ),
        provider_kind="openai",
        litellm_prefix="openai",
        api_base="https://api.openai.com/v1",
        # Model entitlement/pricing tiers change independently of CLIO
        # releases; a compiled-in default goes stale (the brief's own example:
        # "gpt-4o-mini" was already superseded). Live discovery / the
        # community catalog supplies the current default.
        suggested_model="",
        api_key_env="OPENAI_API_KEY",
        is_kind_default=True,
    ),
    Provider(
        id="anthropic",
        label="Anthropic API",
        description="Direct Anthropic API. Requires an ANTHROPIC_API_KEY.",
        provider_kind="anthropic",
        litellm_prefix="anthropic",
        api_base="https://api.anthropic.com/v1",
        suggested_model="",
        api_key_env="ANTHROPIC_API_KEY",
        is_kind_default=True,
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
        suggested_model="",
        api_key_env="GOOGLE_API_KEY",
        supports_live_catalog=False,
    ),
    Provider(
        id="vertex_ai",
        label="Google Vertex AI",
        description="Vertex AI models using Application Default Credentials.",
        provider_kind="openai",
        litellm_prefix="vertex_ai",
        api_base="https://aiplatform.googleapis.com",
        suggested_model="",
        requires_api_key=False,
        auth_method="none",
        host_credentials="google_cloud",
        supports_live_catalog=False,
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
    ),
    Provider(
        id="bedrock",
        label="AWS Bedrock",
        description="Bedrock models using the host AWS credential chain.",
        provider_kind="openai",
        litellm_prefix="bedrock",
        api_base="https://bedrock-runtime.us-east-1.amazonaws.com",
        suggested_model="",
        requires_api_key=False,
        auth_method="none",
        host_credentials="aws",
        supports_live_catalog=False,
        configuration_fields=(
            ProviderConfigurationField(
                "aws_region_name", "AWS region", "Region containing the model.", "us-east-1", True
            ),
            ProviderConfigurationField(
                "aws_profile_name", "AWS profile", "Optional shared AWS profile name."
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
        suggested_model="",
        api_key_env="NVIDIA_NIM_API_KEY",
        supports_live_catalog=False,
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
        suggested_model="",
        api_key_env="OPENROUTER_API_KEY",
        # GET /api/v1/key describes the calling key; 401 for a bad one.
        key_check_path="/key",
    ),
    Provider(
        id="codex",
        label="Codex",
        description=(
            "Signs in with your Codex account and calls the Codex backend "
            "directly from CLIO's own process -- no Codex CLI, no Codex SDK. "
            "Usage counts against your Codex plan limits."
        ),
        provider_kind="codex",
        # The litellm wire prefix is kept separate from the catalog id
        # ("codex") on purpose: this module was ORIGINALLY registered under
        # "chatgpt" (its catalog id at the time), and litellm ships its own
        # native "chatgpt" provider that silently intercepted every turn
        # before ours ever ran. See providers.codex.constants.LITELLM_PROVIDER
        # for the full story -- this indirection is the permanent fix.
        litellm_prefix=_CODEX_LITELLM_PREFIX,
        # No HTTP base to configure -- an identity marker only. The actual
        # transport endpoints (chatgpt.com/backend-api) live in
        # providers.codex.constants, never here.
        api_base="codex://direct",
        # The backend's live model list (providers.codex.model_list) supplies
        # the default; never auto-select a compiled-in candidate.
        suggested_model="",
        requires_api_key=False,
        auth_method="subscription",
        is_kind_default=True,
        supports_live_catalog=False,
        parse_retry_capability="single_attempt",
    ),
    Provider(
        id="claude_code",
        label="Claude Code",
        description=(
            "Uses the Claude Agent SDK with Claude Code subscription auth "
            "instead of direct Anthropic API keys. Authenticate Claude Code "
            "once on this machine; the SDK owns the model session and "
            "streaming lifecycle."
        ),
        provider_kind="claude_code",
        litellm_prefix="claude_code",
        api_base="claude-code://sdk",
        # Model entitlement and the account default are discovered by a live
        # Claude Code probe. Static aliases are candidates, never defaults.
        suggested_model="",
        requires_api_key=False,
        auth_method="subscription",
        is_kind_default=True,
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
        #
        # No static model_catalog (model-capabilities brief 9.1): model
        # existence AND per-model vision/pdf modality evidence for claude_code
        # come from exactly ONE trusted source now, the maintained catalog
        # document `catalogs/claude-code-models.json`
        # (providers/model_discovery/claude_code_catalog.py), read through the
        # refresh overlay when populated and through
        # ClaudeCodeCatalogHandshake._fallback_models's own disk cache before
        # the first refresh -- never a second, hand-typed candidate list here
        # that could drift from it (that was the previous four-row exception;
        # it is now a real data source, not a compiled-in tuple).
        # B7 not adopted (S2 Claude SDK tuning): CLIO's own DSPy ReAct loop
        # drives every claude_code turn end-to-end (tools=[] on the SDK
        # session — see build_sdk_options); Claude Code is a bare model
        # engine, never its own inner loop. Explicit (matches the dataclass
        # default) so the ruling is documented on the record itself.
        inner_loop_owner="clio",
    ),
    # ----- argonne ALCF ----------------------------------------------
    # NB: api_key for argonne presets is resolved lazily via
    # providers.argonne_auth (Globus OAuth), not from the registry.
    # api_key_default stays empty; the auth machinery kicks in inside
    # LMProviderConfig.__post_init__.
    Provider(
        id="argonne_sophia",
        label="ALCF Sophia",
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
        # Sophia's loaded model varies by running job; the live /jobs
        # discovery (kept as-is by this slice) reports the actual subset.
        suggested_model="",
        requires_api_key=False,
        auth_method="oauth",
        auth_label="Globus Auth",
        strip_openai_prefix=False,
        is_kind_default=True,
    ),
    Provider(
        id="argonne_metis",
        label="ALCF Metis",
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
        suggested_model="",
        requires_api_key=False,
        auth_method="oauth",
        auth_label="Globus Auth",
        strip_openai_prefix=False,
    ),
    Provider(
        id="vllm",
        label="vLLM",
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
        suggested_model="",
        requires_api_key=False,
        auth_method="none",
        supports_runtime_sizing=True,
        managed_service_id="vllm",
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
    }
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
    """Build the legacy ``_CLOUD_API_KEY_ENV`` mapping, keyed by ``provider_id``.

    Used by ``__post_init__`` to fill ``api_key`` from the process environment
    when the wire field is blank. Keyed by id only (never by ``provider_kind``,
    Part 3): nine presets share the kind ``"openai"``, so a kind-keyed entry
    would resolve openrouter or nvidia_nim to whichever of them happened to be
    that kind's default -- the literal OpenAI provider's ``OPENAI_API_KEY``.
    A previous kind-keyed second pass here was a pure no-op (the only kinds
    with an ``is_kind_default`` provider that also declares ``api_key_env`` are
    "openai" and "anthropic", each already covered by its own id), so it added
    nothing and is deleted rather than kept as dead scaffolding.
    """
    return {p.id: p.api_key_env for p in PROVIDERS if p.api_key_env}


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
            auth_label=p.auth_label,
            is_authenticated=p.auth_method == "none",
            description=p.description,
            supports_live_catalog=p.supports_live_catalog,
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
