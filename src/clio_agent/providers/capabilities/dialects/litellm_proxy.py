"""LiteLLM proxy dialect adapter (model-capabilities brief Part 6, LiteLLM proxy section).

A LiteLLM proxy is a GATEWAY: one alias (``model_name``) may fan out to several
real deployments (round-robin, fallback). Two endpoints:

* ``GET /v1/models`` -- the aliases the proxy exposes.
* ``GET /v1/model/info`` -- per-alias detail, INCLUDING every underlying
  deployment sharing that alias (LiteLLM's own ``model_info`` array groups
  them). ``max_input_tokens``/``max_output_tokens`` are [D] (what THIS proxy
  currently serves under the alias); the ``supports_*`` flags are [M] (a fact
  about the weights, echoed by the proxy).

When one alias covers several deployments, brief 6 says to keep only what ALL
of them share and the SMALLEST of their limits -- an alias is only as capable,
and only as roomy, as its worst-case member (clio-coder ``protocol/litellm.ts``
does the equivalent narrowing; no such file exists in this codebase's clio-coder
checkout, so this is a fresh port of the RULE, not existing code).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
    unknown,
)

DIALECT = "litellm_proxy"

#: The ``supports_*`` boolean fields LiteLLM's ``/v1/model/info`` echoes per
#: deployment, and the :class:`ModelCapabilities` field each maps onto.
_SUPPORTS_FIELD_MAP: dict[str, str] = {
    "supports_function_calling": "tools",
    "supports_parallel_function_calling": "parallel_tool_calls",
    "supports_response_schema": "structured_output",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def list_aliases(payload: Any) -> list[str]:
    """``GET /v1/models`` -> the alias ids the proxy exposes."""
    rows = payload.get("data") if isinstance(payload, Mapping) else payload
    if not isinstance(rows, list):
        return []
    return [str(r["id"]) for r in rows if isinstance(r, Mapping) and r.get("id")]


def _deployments_for_alias(payload: Any, alias: str) -> list[Mapping[str, Any]]:
    rows = payload.get("data") if isinstance(payload, Mapping) else payload
    if not isinstance(rows, list):
        return []
    matches: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        model_name = row.get("model_name")
        info = row.get("model_info")
        alias_id = model_name if model_name else (info.get("id") if isinstance(info, Mapping) else None)
        if alias_id == alias:
            matches.append(row)
    return matches


def parse_model_info(
    payload: Any,
    alias: str,
    *,
    provider_id: str,
    api_base: str,
    observed_at: str | None = None,
) -> tuple[ModelCapabilities, DeploymentCapabilities]:
    """Build ``(ModelCapabilities, DeploymentCapabilities)`` for one alias from ``GET /v1/model/info``.

    When ``alias`` fans out to N deployments, the result keeps only the
    ``supports_*`` flags every one of them agrees on (an unanimous ``True``) and
    the SMALLEST of their token limits (brief: "keep only the capabilities all
    of them share and the smallest limits").
    """
    observed_at = observed_at or _now_iso()
    rows = _deployments_for_alias(payload, alias)

    max_input = _min_positive(_model_info(row).get("max_input_tokens") for row in rows)
    max_output = _min_positive(_model_info(row).get("max_output_tokens") for row in rows)

    model_flags: dict[str, bool] = {}
    for source_field, target_field in _SUPPORTS_FIELD_MAP.items():
        values = [_model_info(row).get(source_field) for row in rows if source_field in _model_info(row)]
        known = [bool(v) for v in values if isinstance(v, bool)]
        if known:
            model_flags[target_field] = all(known)

    model = ModelCapabilities(
        model_key=alias,
        tools=(
            Fact(model_flags["tools"], "litellm", observed_at, "litellm /v1/model/info supports_function_calling")
            if "tools" in model_flags
            else unknown()
        ),
        parallel_tool_calls=(
            Fact(
                model_flags["parallel_tool_calls"],
                "litellm",
                observed_at,
                "litellm /v1/model/info supports_parallel_function_calling",
            )
            if "parallel_tool_calls" in model_flags
            else unknown()
        ),
        structured_output=(
            Fact(
                model_flags["structured_output"],
                "litellm",
                observed_at,
                "litellm /v1/model/info supports_response_schema",
            )
            if "structured_output" in model_flags
            else unknown()
        ),
    )
    deployment = DeploymentCapabilities(
        provider_id=provider_id,
        api_base=api_base,
        model_id=alias,
        context_served=(
            Fact(max_input, "server_report", observed_at, "litellm /v1/model/info max_input_tokens (min across deployments)")
            if max_input is not None
            else unknown()
        ),
        output_max=(
            Fact(max_output, "server_report", observed_at, "litellm /v1/model/info max_output_tokens (min across deployments)")
            if max_output is not None
            else unknown()
        ),
        fingerprint=(f"litellm_proxy:alias={alias}:deployments={len(rows)}" if rows else ""),
    )
    return model, deployment


def _min_positive(values: Any) -> int | None:
    known = [v for v in values if isinstance(v, int) and not isinstance(v, bool) and v > 0]
    return min(known) if known else None


def _model_info(row: Mapping[str, Any]) -> Mapping[str, Any]:
    info = row.get("model_info")
    return info if isinstance(info, Mapping) else {}


def build_endpoint_capabilities(provider_id: str, api_base: str, model_id: str) -> Any:
    """Build this endpoint's :class:`EndpointCapabilities` for the proxy itself."""
    from clio_agent.providers.capabilities import endpoint as capability_endpoint  # noqa: PLC0415

    return capability_endpoint.build_endpoint_capabilities(
        provider_id,
        api_base,
        DIALECT,
        model_id,
        custom_llm_provider="openai",
        multi_model=True,
    )


__all__ = [
    "DIALECT",
    "build_endpoint_capabilities",
    "list_aliases",
    "parse_model_info",
]
