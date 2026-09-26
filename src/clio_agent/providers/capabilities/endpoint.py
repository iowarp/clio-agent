"""Endpoint-record resolution: what a server's SOFTWARE accepts (brief 5.2).

The main source is LiteLLM's own provider layer
(``litellm.get_supported_openai_params(model, custom_llm_provider)``), which
already knows each provider's accepted request fields and how it translates
them -- reimplementing that per dialect is exactly what the brief forbids
("don't reimplement any of that"). ``custom_llm_provider`` is the SAME string
:mod:`clio_agent.providers.catalog` already calls ``litellm_prefix`` per
provider (``ollama_chat``, ``hosted_vllm``, ...), so no second mapping table is
needed.

For a self-hosted server LiteLLM has no dedicated provider for (llama.cpp) or
whose generic list under- or over-states reality (vLLM's extra sampling
knobs, LM Studio never accepting ``chat_template_kwargs``), :data:`SUPPLEMENT_TABLE`
is the ONE place that dialect knowledge lives -- a fact about the server
SOFTWARE, not about any model, which is exactly what the ground rules allow in
code. Each row says which LiteLLM gap it fills so it can be deleted the day
LiteLLM covers it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from clio_agent.providers.capabilities.records import (
    EndpointCapabilities,
    Fact,
    FactSource,
    unknown,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SupplementRow:
    """One dialect's correction to LiteLLM's ``get_supported_openai_params`` answer.

    Attributes:
        dialect: The dialect this row applies to (brief
            :class:`~clio_agent.providers.capabilities.records.EndpointCapabilities.dialect`
            values: ``llama_cpp``, ``vllm``, ``lm_studio``, ...).
        adds: Fields the server accepts that LiteLLM's generic answer omits.
        removes: Fields LiteLLM's generic answer implies but this server must
            NEVER receive (LM Studio + ``chat_template_kwargs``).
        fills_litellm_gap: Why this row exists -- delete it once LiteLLM's own
            provider layer covers the gap.
        min_version / max_version: Optional server-version bounds (inclusive),
            when the gap is version-specific. ``None`` means unbounded on that
            side.
    """

    dialect: str
    adds: frozenset[str] = frozenset()
    removes: frozenset[str] = frozenset()
    fills_litellm_gap: str = ""
    min_version: str | None = None
    max_version: str | None = None


#: brief 5.2 step 3. Only what LiteLLM's provider layer does not already cover.
SUPPLEMENT_TABLE: tuple[SupplementRow, ...] = (
    SupplementRow(
        dialect="llama_cpp",
        adds=frozenset(
            {"top_k", "min_p", "repeat_penalty", "reasoning_effort", "chat_template_kwargs"}
        ),
        fills_litellm_gap="llama.cpp has no dedicated LiteLLM provider at all",
    ),
    SupplementRow(
        dialect="vllm",
        adds=frozenset({"top_k", "min_p", "repetition_penalty", "chat_template_kwargs"}),
        fills_litellm_gap=(
            "hosted_vllm's generic OpenAI-shaped list omits vLLM's own sampling/template extras"
        ),
    ),
    SupplementRow(
        dialect="lm_studio",
        removes=frozenset({"chat_template_kwargs"}),
        fills_litellm_gap=(
            "LM Studio must never receive chat_template_kwargs even where a generic "
            "OpenAI-shaped list would otherwise imply it is safe to send"
        ),
    ),
)

#: Dialect knowledge (brief ground rules: server software, not model facts):
#: which wire controls exist for driving thinking on this server type, when
#: the model's own mechanism can use one of them (brief 5.5/7). Not gated on
#: ``accepted_params`` -- these are provider-translated fields LiteLLM (or the
#: server's own native API) accepts even when they don't appear verbatim in a
#: generic OpenAI-shaped params list.
THINKING_CONTROLS_BY_DIALECT: dict[str, frozenset[str]] = {
    "llama_cpp": frozenset({"reasoning_effort", "chat_template_kwargs"}),
    "vllm": frozenset({"chat_template_kwargs", "thinking_token_budget"}),
    "ollama": frozenset({"think"}),
    "lm_studio": frozenset({"reasoning_effort"}),
    "openrouter": frozenset({"reasoning_object"}),
    # anthropic offers BOTH controls: "reasoning_effort" (LiteLLM's own kwarg,
    # translated into thinking=adaptive + output_config.effort) when the model
    # reports adaptive-thinking effort levels, "anthropic_thinking" (a token
    # budget) for a model with no effort evidence -- combine.py's per-
    # mechanism priority picks whichever the model's own ThinkingSpec needs.
    "openai": frozenset({"reasoning_effort"}),
    "anthropic": frozenset({"reasoning_effort", "anthropic_thinking"}),
    # SDK/CLI transports (not an HTTP request body): the "control" here just
    # names which SDK option channel carries thinking, for combine.py's
    # control-priority selection -- lm/dialect_wire.py spells the actual
    # kwarg (codex_reasoning_effort / claude_code_thinking).
    "codex": frozenset({"reasoning_effort"}),
    "claude_code": frozenset({"effort", "claude_code_thinking"}),
}

#: Dialect knowledge: structured-output modes a server type offers. Best-effort
#: and deliberately coarse -- refining this per model/version is later work;
#: what matters for this slice is that it is a typed, sourced fact rather than
#: an assumed True.
STRUCTURED_OUTPUT_MODES_BY_DIALECT: dict[str, frozenset[str]] = {
    "llama_cpp": frozenset({"json_schema", "gbnf"}),
    "vllm": frozenset({"json_schema"}),
    "ollama": frozenset({"json_schema"}),
    "lm_studio": frozenset({"json_schema"}),
    "openrouter": frozenset({"json_schema"}),
    "openai": frozenset({"json_schema", "json_object_schema"}),
    "anthropic": frozenset(),
}


def _supplement_for(dialect: str) -> SupplementRow | None:
    return next((row for row in SUPPLEMENT_TABLE if row.dialect == dialect), None)


#: ``provider_kind`` values that already ARE their own dialect one-to-one
#: (brief Part 3: a kind chooses the wire format, and for these it happens to
#: uniquely name the server software too). ``argonne`` fronts vLLM (every ALCF
#: cluster runs a vLLM-backed gateway), so its dialect is ``vllm``, not the
#: bare kind.
_DIALECT_BY_KIND: dict[str, str] = {
    "ollama": "ollama",
    "lm_studio": "lm_studio",
    "anthropic": "anthropic",
    "codex": "codex",
    "claude_code": "claude_code",
    "argonne": "vllm",
}


def dialect_for_provider(provider_kind: str, litellm_prefix: str, provider_id: str) -> str:
    """Map a registry provider row to the endpoint-record ``dialect`` (brief 4.1).

    ``provider_kind`` only selects the wire FORMAT (brief Part 3) and
    collapses many real server types onto ``"openai"`` (llama.cpp, a bare
    vLLM server, and every genuine cloud OpenAI-compatible endpoint all share
    it), so it cannot be used as the dialect on its own. ``litellm_prefix``
    (LiteLLM's own ``custom_llm_provider`` name, e.g. ``"hosted_vllm"``)
    disambiguates the self-hosted dialects this slice's supplement table
    knows about; ``provider_id`` is consulted only as a last resort for a
    dialect LiteLLM has no dedicated provider for at all (llama.cpp).
    """

    if provider_kind in _DIALECT_BY_KIND:
        return _DIALECT_BY_KIND[provider_kind]
    if litellm_prefix == "hosted_vllm":
        return "vllm"
    lowered_id = (provider_id or "").lower()
    if "llama_cpp" in lowered_id or "llamacpp" in lowered_id or "llama.cpp" in lowered_id:
        return "llama_cpp"
    return litellm_prefix or provider_kind or "openai"


def resolve_accepted_params(
    dialect: str, model_id: str, *, custom_llm_provider: str
) -> Fact[frozenset[str]]:
    """Resolve the accepted-parameter set for one endpoint (brief 5.2 steps 2-3).

    Args:
        dialect: The endpoint's dialect (see :data:`SUPPLEMENT_TABLE`).
        model_id: The model id passed through to LiteLLM (some providers'
            answers are model-sensitive).
        custom_llm_provider: LiteLLM's own provider name for this dialect --
            :mod:`clio_agent.providers.catalog`'s ``litellm_prefix``.

    Returns:
        A ``Fact`` whose value is the accepted parameter set, or unknown when
        LiteLLM has no mapping for ``custom_llm_provider`` AND no supplement
        row adds anything (brief 5.2 step 5: total miss).
    """

    try:
        import litellm  # noqa: PLC0415

        params = litellm.get_supported_openai_params(
            model=model_id, custom_llm_provider=custom_llm_provider
        )
    except Exception as exc:  # noqa: BLE001 - a broken litellm call degrades to unknown, logged
        logger.warning(
            "capabilities.endpoint: litellm.get_supported_openai_params failed "
            "dialect=%s custom_llm_provider=%s: %s",
            dialect,
            custom_llm_provider,
            exc,
        )
        params = None

    observed_at = _now_iso()
    known = params is not None
    accepted: set[str] = set(params) if params is not None else set()
    source: FactSource = "litellm" if known else "dialect"
    detail_parts: list[str] = []
    if not known:
        detail_parts.append(
            f"litellm has no mapping for custom_llm_provider={custom_llm_provider!r}"
        )

    supplement = _supplement_for(dialect)
    if supplement is not None:
        if supplement.adds:
            accepted |= supplement.adds
            known = True
            detail_parts.append(f"+{sorted(supplement.adds)} ({supplement.fills_litellm_gap})")
        if supplement.removes:
            removed = accepted & supplement.removes
            accepted -= supplement.removes
            if removed:
                detail_parts.append(f"-{sorted(removed)} ({supplement.fills_litellm_gap})")

    if not known:
        return unknown("; ".join(detail_parts) or "no evidence for this dialect")
    return Fact(
        value=frozenset(accepted),
        source=source,
        observed_at=observed_at,
        detail="; ".join(detail_parts),
    )


def build_endpoint_capabilities(
    provider_id: str,
    api_base: str,
    dialect: str,
    model_id: str,
    *,
    custom_llm_provider: str,
    multi_model: bool = False,
    server_version: Fact[str] | None = None,
    fingerprint: str = "",
) -> EndpointCapabilities:
    """Build one :class:`EndpointCapabilities` for ``(provider_id, api_base)``.

    Pure given its inputs: no network call beyond what ``litellm`` itself does
    internally (none -- ``get_supported_openai_params`` is a local lookup).
    """

    observed_at = _now_iso()
    accepted = resolve_accepted_params(dialect, model_id, custom_llm_provider=custom_llm_provider)
    thinking = THINKING_CONTROLS_BY_DIALECT.get(dialect, frozenset())
    structured = STRUCTURED_OUTPUT_MODES_BY_DIALECT.get(dialect, frozenset())
    return EndpointCapabilities(
        provider_id=provider_id,
        api_base=api_base,
        dialect=dialect,
        server_version=server_version if server_version is not None else unknown(),
        accepted_params=accepted,
        thinking_controls=Fact(value=thinking, source="dialect", observed_at=observed_at)
        if thinking
        else unknown("dialect has no known thinking control"),
        structured_output_modes=Fact(value=structured, source="dialect", observed_at=observed_at)
        if structured
        else unknown("dialect has no known structured-output mode"),
        multi_model=multi_model,
        fingerprint=fingerprint,
    )


__all__ = [
    "STRUCTURED_OUTPUT_MODES_BY_DIALECT",
    "SUPPLEMENT_TABLE",
    "THINKING_CONTROLS_BY_DIALECT",
    "SupplementRow",
    "build_endpoint_capabilities",
    "dialect_for_provider",
    "resolve_accepted_params",
]
