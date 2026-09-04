"""Codex model discovery through the official Python SDK."""

from __future__ import annotations

import asyncio
from typing import Any

from clio_agent.providers.model_discovery.modality_evidence import (
    modality_evidence,
    reported_modalities,
)
from clio_agent.providers.model_discovery.overlay import (
    CODEX_SOURCE,
    ProviderDiscoveryResult,
    attach_context_limits,
)

#: The modalities a Codex row can claim beyond text. Listed so an omitted
#: ``input_modalities`` field records exactly which capabilities went
#: unevidenced instead of leaving the negative anonymous.
_CODEX_NON_TEXT_MODALITIES = ("image",)


def _codex_capability_row(row: Any) -> dict[str, Any]:
    """Return one discovered row's capabilities plus their typed evidence.

    The pinned ``openai_codex`` SDK declares ``Model.input_modalities`` with a
    schema default of ``["text", "image"]`` (verified in
    ``openai_codex/generated/v2_all.py``), so reading the attribute directly
    manufactures an image capability for any wire row that omitted the field —
    and the typed negative could never fire in production. Capabilities are
    therefore stamped ONLY from ``model_fields_set``; an omitted field records
    no modality at all and a ``modality_unreported`` reason.
    """

    values = reported_modalities(row, "input_modalities")
    if values is None:
        return {
            "capabilities": [],
            "capability_evidence": modality_evidence(
                source="codex_sdk_input_modalities",
                reason="modality_unreported",
                unevidenced=_CODEX_NON_TEXT_MODALITIES,
            ),
        }
    return {
        "capabilities": values,
        "capability_evidence": modality_evidence(
            source="codex_sdk_input_modalities",
            reason="modality_reported",
        ),
    }


# Deliberate injection seam for focused discovery tests.  The official SDK is
# still imported only when discovery is requested; keeping the default as
# ``None`` prevents provider startup from loading Codex for unrelated users.
AsyncCodex: Any | None = None


def discover_codex(*, timeout: float = 20.0) -> ProviderDiscoveryResult:
    """Refresh the account's live Codex catalog through ``openai_codex``.

    The Python SDK owns its pinned runtime and authentication. CLIO neither
    resolves a ``codex`` executable nor opens an app-server protocol connection.
    """

    try:
        from openai_codex import AsyncCodex as SDKAsyncCodex  # noqa: PLC0415
        from openai_codex import CodexConfig, CodexError  # noqa: PLC0415

        from clio_agent.providers.codex_credential_home import (  # noqa: PLC0415
            IsolatedCodexHome,
        )
        from clio_agent.providers.codex_stream import (  # noqa: PLC0415
            BARE_LM_CONFIG_OVERRIDES,
        )
    except (ImportError, OSError) as exc:
        return ProviderDiscoveryResult(
            provider="codex",
            discovered=[],
            source=CODEX_SOURCE,
            failed_reason=f"Codex Python SDK is unavailable: {exc}",
        )

    sdk_client = AsyncCodex or SDKAsyncCodex

    async def _query() -> Any:
        sdk_home = IsolatedCodexHome()
        try:
            async with sdk_client(
                CodexConfig(
                    config_overrides=BARE_LM_CONFIG_OVERRIDES,
                    env=sdk_home.start(),
                )
            ) as client:
                return await client.models()
        finally:
            sdk_home.close()

    async def _bounded_query() -> Any:
        return await asyncio.wait_for(_query(), timeout=timeout)

    try:
        response = asyncio.run(_bounded_query())
    except (CodexError, OSError, RuntimeError, TimeoutError) as exc:
        return ProviderDiscoveryResult(
            provider="codex",
            discovered=[],
            source=CODEX_SOURCE,
            failed_reason=f"Codex Python SDK model discovery failed: {exc}",
        )

    rows = list(response.data)
    discovered = [
        {
            "id": str(row.id),
            "name": str(row.display_name or row.id),
            "description": str(row.description or ""),
            **_codex_capability_row(row),
        }
        for row in rows
        if row.id
    ]
    if not discovered:
        return ProviderDiscoveryResult(
            provider="codex",
            discovered=[],
            source=CODEX_SOURCE,
            failed_reason="Codex Python SDK returned zero models",
        )
    default_model = next(
        (str(row.id) for row in rows if row.is_default),
        str(discovered[0]["id"]),
    )
    return ProviderDiscoveryResult(
        provider="codex",
        discovered=attach_context_limits(discovered, "codex"),
        source=CODEX_SOURCE,
        default_model=default_model,
    )


__all__ = ["discover_codex"]
