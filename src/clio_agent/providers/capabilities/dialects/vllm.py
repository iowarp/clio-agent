"""vLLM dialect adapter (model-capabilities brief Part 6, vLLM section).

vLLM has no official Python client either; this is two plain HTTP reads:

* ``GET /v1/models`` -- ``max_model_len`` (deployment: the context actually
  served) and ``root`` (the Hugging Face repo id vLLM loaded, which feeds the
  model link, brief 5.4 rule 2, AND the Part 6.1 Hugging Face layer directly --
  it is trusted verbatim, never guessed).
* ``GET /version`` -- the endpoint's own ``server_version`` / invalidation
  fingerprint (brief 5.6).

Tools/vision/reasoning parsing depend on vLLM launch flags it does not expose
on either endpoint, so the brief routes those through the active probes
(:mod:`clio_agent.providers.capabilities.dialects.probes`) instead of a static
read here -- this module intentionally leaves them unknown rather than
guessing from the model id.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.capabilities.link import deployment_model_key_fact
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    EndpointCapabilities,
    Fact,
    ModelCapabilities,
    unknown,
)

DIALECT = "vllm"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def fingerprint_from_version(version: Any) -> str:
    """The endpoint invalidation key (brief 5.6): vLLM's ``GET /version``."""
    text = str(version or "").strip()
    return f"vllm:version={text}" if text else ""


def deployment_fingerprint_from_row(row: Mapping[str, Any]) -> str:
    """The deployment invalidation key (brief 5.6): the ``/v1/models`` row itself.

    Keyed by ``root`` + ``max_model_len`` (brief: "the ``/v1/models`` row
    (``root``, ``max_model_len``)") -- either changing means a different model
    (or a different served window) is now behind this ``model_id``.
    """
    root = row.get("root")
    max_len = row.get("max_model_len")
    if root is None and max_len is None:
        return ""
    return f"vllm:root={root}:max_model_len={max_len}"


def find_model_row(payload: Any, model_id: str) -> Mapping[str, Any] | None:
    """Return the ``/v1/models`` row matching ``model_id``, or the sole row when only one exists."""
    rows = payload.get("data") if isinstance(payload, Mapping) else payload
    if not isinstance(rows, list):
        return None
    matches = [r for r in rows if isinstance(r, Mapping)]
    exact = next((r for r in matches if r.get("id") == model_id), None)
    if exact is not None:
        return exact
    return matches[0] if len(matches) == 1 else None


def parse_models_row(
    row: Mapping[str, Any],
    *,
    provider_id: str,
    api_base: str,
    observed_at: str | None = None,
) -> DeploymentCapabilities:
    """Build one :class:`DeploymentCapabilities` from a vLLM ``/v1/models`` row."""
    observed_at = observed_at or _now_iso()
    model_id = str(row.get("id") or "")
    max_model_len = _positive_int(row.get("max_model_len"))
    root = row.get("root")
    vllm_root = str(root) if isinstance(root, str) and root.strip() else None

    model_key_fact = deployment_model_key_fact(model_id, observed_at=observed_at, vllm_root=vllm_root)

    return DeploymentCapabilities(
        provider_id=provider_id,
        api_base=api_base,
        model_id=model_id,
        model_key=model_key_fact,
        context_served=(
            Fact(max_model_len, "server_report", observed_at, "vllm /v1/models max_model_len")
            if max_model_len is not None
            else unknown()
        ),
        fingerprint=deployment_fingerprint_from_row(row),
    )


def build_model_capabilities(model_key: str, row: Mapping[str, Any]) -> ModelCapabilities:
    """vLLM's ``/v1/models`` row carries no MODEL-level ceiling of its own.

    ``root`` is a Hugging Face repo id (Part 6.1 fetches the model's real
    ceiling from there); this row alone yields nothing beyond identity, so the
    model record is a bare, unknown-everything stub for the caller's
    ``model_sources`` merge to fill from the HF/community-catalog layers.
    """
    del row
    return ModelCapabilities(model_key=model_key)


def build_endpoint_capabilities(
    provider_id: str, api_base: str, model_id: str, *, version: Any, multi_model: bool = True
) -> EndpointCapabilities:
    """Build this endpoint's :class:`EndpointCapabilities`, plugging in ``GET /version``."""
    from clio_agent.providers.capabilities import endpoint as capability_endpoint  # noqa: PLC0415

    observed_at = _now_iso()
    version_text = str(version or "").strip()
    return capability_endpoint.build_endpoint_capabilities(
        provider_id,
        api_base,
        DIALECT,
        model_id,
        custom_llm_provider="hosted_vllm",
        multi_model=multi_model,
        server_version=(
            Fact(version_text, "server_report", observed_at, "vllm /version") if version_text else None
        ),
        fingerprint=fingerprint_from_version(version),
    )


__all__ = [
    "DIALECT",
    "build_endpoint_capabilities",
    "build_model_capabilities",
    "deployment_fingerprint_from_row",
    "find_model_row",
    "fingerprint_from_version",
    "parse_models_row",
]
