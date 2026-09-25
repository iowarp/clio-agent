"""Codex model discovery: the maintained catalog plus a credential-store check.

Per owner ruling (A.8), the model list, its context/output limits, and its
reasoning-effort levels come from the maintained catalog
(:mod:`.codex_catalog`) -- the Codex backend offers no account model-
enumeration RPC the way the deleted ``openai_codex`` SDK's ``client.models()``
did. This module's only LIVE check is whether CLIO holds a signed-in Codex
credential -- never a network probe, which would spend this passive discovery
path on a token refresh nobody asked for.
"""

from __future__ import annotations

from typing import Any

from clio_agent.providers.codex.credentials import CodexCredentialStore
from clio_agent.providers.codex.errors import CODEX_AUTHENTICATION_ERROR_MESSAGE
from clio_agent.providers.model_discovery.codex_catalog import (
    CodexCatalogError,
    refresh_codex_catalog,
)
from clio_agent.providers.model_discovery.modality_evidence import modality_evidence
from clio_agent.providers.model_discovery.overlay import CODEX_SOURCE, ProviderDiscoveryResult


def _capability_row(model: dict[str, Any]) -> dict[str, Any]:
    capabilities = [str(v) for v in model.get("capabilities") or ["text"]]
    return {
        "capabilities": capabilities,
        "capability_evidence": modality_evidence(
            source="codex_catalog", reason="modality_cataloged"
        ),
    }


def _reasoning_row(model: dict[str, Any]) -> dict[str, Any]:
    """Persist the catalog's effort levels in the SAME field names Codex's SDK
    discovery used (``supported_reasoning_efforts``/``default_reasoning_effort``)
    so :mod:`clio_agent.providers.handshake.cli_catalog` and
    :mod:`clio_agent.providers.reasoning_levels` need no shape change."""

    levels = [str(v) for v in model.get("effort_levels") or []]
    default = "medium" if "medium" in levels else (levels[0] if levels else "")
    return {"supported_reasoning_efforts": levels, "default_reasoning_effort": default}


def discover_codex(
    *,
    credential_store: CodexCredentialStore | None = None,
    catalog_candidates: list[dict[str, Any]] | None = None,
) -> ProviderDiscoveryResult:
    """Trust the maintained catalog for models; verify sign-in via the credential store.

    Args:
        credential_store: Injection seam for tests.
        catalog_candidates: An explicit row list (diagnostic callers / tests)
            bypassing the fetched catalog.
    """

    if catalog_candidates is None:
        try:
            catalog = refresh_codex_catalog()
        except CodexCatalogError as exc:
            return ProviderDiscoveryResult(
                provider="codex", discovered=[], source=CODEX_SOURCE, failed_reason=str(exc)
            )
        rows, default_model = catalog.models, catalog.default_model
    else:
        rows, default_model = catalog_candidates, ""

    store = credential_store or CodexCredentialStore()
    if not store.is_signed_in():
        return ProviderDiscoveryResult(
            provider="codex",
            discovered=[],
            source=CODEX_SOURCE,
            failed_reason=CODEX_AUTHENTICATION_ERROR_MESSAGE,
        )

    discovered = [
        {
            "id": str(row["id"]),
            "name": str(row.get("name") or row["id"]),
            "description": "",
            # A.8: the maintained catalog IS the source of truth for these two
            # -- never routed through overlay.attach_context_limits()'s
            # models.dev/litellm/local-DB cascade. That cascade is for
            # providers with no such catalog of their own (claude_code); for
            # Codex it would look up candidate ids like "gpt-5.6-sol" that
            # cascade has never heard of, miss, and silently overwrite a real
            # catalog value with None (caught in review, never shipped).
            "context_window": row.get("context_window"),
            "output_limit": row.get("max_output_tokens"),
            "context_source": "codex_catalog",
            **_capability_row(row),
            **_reasoning_row(row),
        }
        for row in rows
        if isinstance(row, dict) and row.get("id")
    ]
    if not discovered:
        return ProviderDiscoveryResult(
            provider="codex",
            discovered=[],
            source=CODEX_SOURCE,
            failed_reason="Codex catalog has zero models",
        )
    return ProviderDiscoveryResult(
        provider="codex",
        discovered=discovered,
        source=CODEX_SOURCE,
        default_model=default_model,
    )


__all__ = ["discover_codex"]
