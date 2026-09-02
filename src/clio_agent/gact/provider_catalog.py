"""Normalized provider/model discovery for resource-aware clients.

The legacy provider routes remain available for older clients. This module
projects their underlying handshake facts into an explicit catalog whose
availability and modalities carry freshness and evidence instead of optimistic
static guesses.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from clio_agent.gact.types import LMProviderPreset
from clio_agent.providers import model_discovery
from clio_agent.providers.catalog import get_provider
from clio_agent.providers.handshake import HandshakeContext, HandshakeReport, run_handshake
from clio_agent.providers.handshake.model import ModelProfile


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _api_key_for(preset: LMProviderPreset) -> str:
    return model_discovery.resolve_cloud_api_key(preset.provider)


async def _ensure_codex_live_catalog(preset: LMProviderPreset) -> str:
    """Populate a fresh install from Codex's local app-server model catalog.

    This is deliberately restricted to Codex.  Its ``model/list`` RPC is a
    local, authenticated catalog read and does not create a model turn.  Claude
    Code discovery validates aliases with real provider calls and therefore
    remains an explicit user action.

    Returns an empty string on success or when an overlay already exists, and
    an actionable failure reason otherwise.
    """

    if preset.id != "codex" or preset.provider != "codex":
        return ""
    try:
        existing = model_discovery.overlay_models_wire(preset.id, preset.provider)
    except model_discovery.OverlayMalformedError as exc:
        return f"model catalog overlay is malformed: {exc}"
    if existing and existing.get("models"):
        return ""
    provider = get_provider(preset.id)
    if provider is None:
        return "Codex provider is not registered"
    results = await model_discovery.refresh_all(presets=[provider], only_configured=False)
    if not results:
        return "Codex model discovery returned no result"
    failure = str(results[0].get("failed_reason") or "")
    if failure:
        return failure
    return ""


def _modalities(profile: ModelProfile) -> list[str]:
    """Normalize only modalities reported by the live provider handshake."""

    normalized: set[str] = {"text"}
    for capability in profile.capabilities:
        value = capability.strip().lower().replace("-", "_")
        if value in {"vision", "image", "images", "image_input"}:
            normalized.add("image")
        elif value in {"pdf", "document", "documents", "pdf_input"}:
            normalized.add("pdf")
        elif value in {"audio", "audio_input"}:
            normalized.add("audio")
        elif value in {"video", "video_input"}:
            normalized.add("video")
    return sorted(normalized)


def model_catalog_row(
    preset: LMProviderPreset,
    report: HandshakeReport,
    profile: ModelProfile,
) -> dict[str, Any]:
    """Return one normalized model row with explicit discovery evidence."""

    live = report.models_source == "live" and report.ok
    return {
        "provider_id": preset.id,
        "provider_kind": preset.provider,
        "endpoint": preset.api_base,
        "deployment": profile.raw.get("deployment") or profile.raw.get("owned_by") or "",
        "model_id": profile.id,
        "revision": str(profile.raw.get("revision") or profile.raw.get("version") or ""),
        "modalities": _modalities(profile) if live else ["text"],
        "reasoning": {
            "supported": profile.is_reasoning,
            "parameter": profile.reasoning_param or "",
        },
        "native_tool_calling": profile.native_tool_calling,
        "context_window": profile.context_window,
        "loaded_context_window": profile.loaded_context_window,
        "output_limit": profile.output_limit,
        "availability": "available" if live else "candidate",
        "evidence": {
            "source": report.models_source,
            "generated_at": report.generated_at,
            "live": live,
            "context_source": profile.context_source,
        },
        "failure": report.error or "",
    }


async def discover_provider(preset: LMProviderPreset, *, refresh: bool = False) -> dict[str, Any]:
    """Run one passive handshake and return a normalized provider record."""

    bootstrap_failure = await _ensure_codex_live_catalog(preset)
    report = await run_handshake(
        HandshakeContext(
            provider_id=preset.id,
            provider_kind=preset.provider,
            api_base=preset.api_base,
            api_key=_api_key_for(preset),
            auth_mode="passive",
            allow_external_sources=True,
        ),
        force=refresh or preset.id == "codex",
    )
    models = report.models
    failure = report.error or bootstrap_failure
    if preset.id == "codex" and report.models_source != "live":
        # Static Codex ids are compatibility candidates for legacy clients,
        # never evidence that the current account can actually select them.
        models = ()
    return {
        "id": preset.id,
        "name": preset.label,
        "kind": preset.provider,
        "endpoint": preset.api_base,
        "configuration_url": f"/settings/providers/{preset.id}",
        "connectivity": report.connectivity.value,
        "auth": report.auth.value,
        "health": "ready" if report.ok and not bootstrap_failure else "unavailable",
        "freshness": {
            "generated_at": report.generated_at or _now_iso(),
            "source": report.models_source,
        },
        "failure": failure or "",
        "models": [model_catalog_row(preset, report, model) for model in models],
    }


__all__ = ["discover_provider", "model_catalog_row"]
