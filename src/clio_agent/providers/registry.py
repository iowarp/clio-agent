"""Single source of truth for the LM provider catalog.

Every piece of provider metadata in clio-agent lives here:

- Wire defaults that ``LMProviderConfig.__post_init__`` reads (api_base,
  model, max_tokens, ``strip_openai_prefix``) — surfaced via
  :func:`as_provider_defaults_dict`.
- Catalog rows that ``GET /v1/providers/lm`` returns to gact's provider
  modal (label, description, requires_api_key) — surfaced via
  :func:`as_lm_presets`.
- Per-preset model catalogs used as the static fallback for
  ``GET /v1/providers/{id}/models`` — surfaced via
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
#: prefix (``openai/``, ``anthropic/``…). Catalog ids are usually a
#: superset (e.g. ``openrouter`` and the legacy codex bridge both have
#: ``provider_kind="openai"``).
ProviderKind = Literal[
    "lm_studio",
    "ollama",
    "openai",
    "anthropic",
    "argonne",
    "codex",
]

AuthMethod = Literal["none", "api_key", "oauth"]


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
    #: share a kind (``openrouter`` and the legacy codex bridge both
    #: share ``openai``); the entry flagged ``is_kind_default`` supplies the
    #: dict row in :func:`as_provider_defaults_dict`.
    provider_kind: ProviderKind

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

    # ----- capability flags -------------------------------------------
    max_tokens_default: int = 32000
    #: Whether to strip ``openai/`` / ``anthropic/`` LiteLLM prefixes
    #: before sending to the upstream model id. ``False`` only for
    #: HuggingFace-id backends like ALCF where ``openai/gpt-oss-120b``
    #: *is* the literal model id.
    strip_openai_prefix: bool = True

    # ----- registry bookkeeping ---------------------------------------
    #: When True, this provider's wire fields populate the
    #: per-``provider_kind`` row returned by
    #: :func:`as_provider_defaults_dict`. Exactly one entry per kind
    #: should be flagged; multiple flags collapse to the first.
    is_kind_default: bool = False

    # ----- static model catalog ---------------------------------------
    model_catalog: tuple[ModelEntry, ...] = ()


# -- shared catalogs --------------------------------------------------

#: Argonne ALCF Llama family. Sophia / Metis / local_vllm all run vLLM
#: behind the gateway and serve roughly this set; the live ``/jobs``
#: endpoint reports the actual subset loaded right now.
_ARGONNE_MODELS: tuple[ModelEntry, ...] = (
    ModelEntry(
        "meta-llama/Meta-Llama-3.1-8B-Instruct",
        "Llama 3.1 8B Instruct (Sophia/Polaris)",
        "Default ALCF demo model. Fastest of the lot.",
    ),
    ModelEntry(
        "meta-llama/Meta-Llama-3.1-70B-Instruct",
        "Llama 3.1 70B Instruct",
        "Heavier reasoning; jobs may need to warm up.",
    ),
    ModelEntry(
        "meta-llama/Meta-Llama-3.1-405B-Instruct",
        "Llama 3.1 405B Instruct",
        "Frontier-class. Often offline; check active models first.",
    ),
    ModelEntry(
        "mistralai/Mistral-7B-Instruct-v0.3",
        "Mistral 7B Instruct v0.3",
        "Lightweight alternative to Llama.",
    ),
)


# -- the catalog ------------------------------------------------------

#: Canonical provider list. Ordered roughly local-first → cloud → ALCF
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
        api_base="http://127.0.0.1:1234/v1",
        # Matches today's PROVIDER_DEFAULTS["lm_studio"]["model"]. The
        # ClioAgent constructor falls back to fetch_lm_studio_models()
        # when the user explicitly clears this field.
        suggested_model="ibm/granite-4-h-tiny",
        api_key_default="lm-studio",
        requires_api_key=False,
        auth_method="none",
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
        api_base="http://127.0.0.1:11434/v1",
        suggested_model="granite3.1-dense:8b",
        api_key_default="ollama",
        requires_api_key=False,
        auth_method="none",
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
    # ----- cloud / proxy ---------------------------------------------
    Provider(
        id="openai",
        label="OpenAI / ChatGPT",
        description=(
            "Direct OpenAI API (powers ChatGPT + Codex CLI). Requires "
            "an OPENAI_API_KEY. Defaults to gpt-4o-mini for low cost; "
            "swap in gpt-4o or gpt-4-turbo for heavier work."
        ),
        provider_kind="openai",
        api_base="https://api.openai.com/v1",
        suggested_model="gpt-4o-mini",
        api_key_env="OPENAI_API_KEY",
        is_kind_default=True,
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
        api_base="https://api.anthropic.com/v1",
        suggested_model="claude-sonnet-4-20250514",
        api_key_env="ANTHROPIC_API_KEY",
        is_kind_default=True,
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
        id="openrouter",
        label="OpenRouter",
        description=(
            "OpenAI-compatible gateway over many providers. Free tier "
            "models (suffixed :free) work without spend but are "
            "heavily rate-limited."
        ),
        provider_kind="openai",
        api_base="https://openrouter.ai/api/v1",
        suggested_model="openai/gpt-oss-120b:free",
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
        label="OpenAI Codex (via bridge)",
        description=(
            "Routes through scripts/codex_bridge.py which fronts the "
            "Codex app-server SDK with an OpenAI-compatible HTTP "
            "interface. Requires the bridge running locally (default "
            "port 18900) and the `codex` binary on PATH. Will be "
            "superseded by a LiteLLM CustomLLM in v0.6 (see #48)."
        ),
        provider_kind="openai",
        api_base="http://127.0.0.1:18900/v1",
        suggested_model="gpt-5.4",
        requires_api_key=False,
        auth_method="none",
        model_catalog=(
            ModelEntry(
                "gpt-5.4",
                "GPT-5.4 (via Codex)",
                "Codex's reasoning-tuned default.",
            ),
            ModelEntry(
                "gpt-5",
                "GPT-5 (via Codex)",
                "Standard GPT-5 through the Codex app-server.",
            ),
            ModelEntry(
                "gpt-4.1",
                "GPT-4.1 (via Codex)",
                "Older GPT-4.1 routed through Codex.",
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
        api_base="https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1",
        suggested_model="meta-llama/Meta-Llama-3.1-8B-Instruct",
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
            "fallback when Sophia is in maintenance — typically loads "
            "gpt-oss-120b and Llama-4-Maverick. Same Globus tokens as "
            "Sophia/Polaris."
        ),
        provider_kind="argonne",
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
        id="argonne_local_vllm",
        label="ALCF local vLLM (compute-node)",
        description=(
            "vLLM running co-located on an Aurora / Polaris compute "
            "node (see localWorkflow/scripts/vllm_setup.sh). No Globus "
            "needed — the server accepts the literal 'EMPTY' API key. "
            "Override api_base with the bound port."
        ),
        # Local vLLM is OpenAI-compatible (not Argonne's gateway-quirky
        # path), so the wire kind is plain openai.
        provider_kind="openai",
        api_base="http://127.0.0.1:8000/v1",
        suggested_model="meta-llama/Llama-3.1-8B-Instruct",
        requires_api_key=False,
        auth_method="none",
        model_catalog=_ARGONNE_MODELS,
    ),
)


# -- lookup helpers ---------------------------------------------------


def get_provider(provider_id: str) -> Provider | None:
    """Return the :class:`Provider` matching ``provider_id``, or None."""
    for p in PROVIDERS:
        if p.id == provider_id:
            return p
    return None


def iter_providers() -> tuple[Provider, ...]:
    """All provider entries in registration order."""
    return PROVIDERS


def kind_default(kind: str) -> Provider | None:
    """Return the kind-default :class:`Provider` for a wire kind, or None."""
    for p in PROVIDERS:
        if p.provider_kind == kind and p.is_kind_default:
            return p
    return None


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
        entry: dict[str, Any] = {
            "api_base": p.api_base,
            "model": p.suggested_model,
            "api_key": p.api_key_default,
        }
        if p.max_tokens_default != 32000:
            entry["max_tokens"] = p.max_tokens_default
        if not p.strip_openai_prefix:
            entry["strip_openai_prefix"] = False
        out[p.provider_kind] = entry
    return out


def as_cloud_api_key_env() -> dict[str, str]:
    """Build the legacy ``_CLOUD_API_KEY_ENV`` mapping.

    Maps each provider_kind that has an ``api_key_env`` on its
    kind-default to that env var name. Used by ``__post_init__`` to
    fill ``api_key`` from the process environment when the wire field
    is blank.
    """
    return {
        p.provider_kind: p.api_key_env
        for p in PROVIDERS
        if p.is_kind_default and p.api_key_env
    }


def as_lm_presets() -> list[Any]:
    """Build the gact ``_LM_PRESETS`` list.

    Imports the Pydantic model lazily so that importing the registry
    doesn't pull in fastapi/uvicorn. Callers in ``gact/app.py`` are
    inside ``build_app()`` where fastapi is already loaded.
    """
    from clio_agent.gact.types import LMProviderPreset  # noqa: PLC0415

    return [
        LMProviderPreset(
            id=p.id,
            label=p.label,
            provider=p.provider_kind,
            api_base=p.api_base,
            suggested_model=p.suggested_model,
            requires_api_key=p.requires_api_key,
            description=p.description,
        )
        for p in PROVIDERS
    ]


def as_provider_models_dict() -> dict[str, list[dict[str, str]]]:
    """Build the gact ``_PROVIDER_MODELS`` static fallback dict.

    Keyed by both preset ``id`` (every entry) and ``provider_kind``
    (the kind default's catalog covers callers that look up by bare
    kind — e.g. ``GET /v1/providers/argonne/models``).
    """
    out: dict[str, list[dict[str, str]]] = {}
    for p in PROVIDERS:
        out[p.id] = [
            {"id": m.id, "name": m.name, "description": m.description}
            for m in p.model_catalog
        ]
    # Add bare-kind keys for providers whose preset id != kind.
    for p in PROVIDERS:
        if p.is_kind_default and p.provider_kind not in out:
            out[p.provider_kind] = out[p.id]
    return out
