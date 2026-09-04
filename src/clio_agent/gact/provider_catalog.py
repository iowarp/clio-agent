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

    Returns an empty string on success, when a FRESH overlay already exists, or
    when re-discovery over a stale-but-present catalog failed (the prior list is
    kept and already carries a typed ``overlay_refresh_failed`` staleness marker
    — prior evidence is not an availability failure). Returns an actionable
    failure reason only when the account has no discovered catalog at all.
    """

    if preset.id != "codex" or preset.provider != "codex":
        return ""
    try:
        existing = model_discovery.overlay_models_wire(preset.id, preset.provider)
    except model_discovery.OverlayMalformedError as exc:
        return f"model catalog overlay is malformed: {exc}"
    # A STALE entry no longer short-circuits the bootstrap. Treating "any models
    # at all" as satisfied is what made the overlay a sticky cache: once written,
    # nothing ever re-ran discovery, so a rotated account catalog was served
    # indefinitely. Discovery here is a local, authenticated catalog read (no
    # model turn), so re-running it on a stale entry is cheap and honest.
    entry = existing or {}
    has_prior_models = bool(entry.get("models"))
    if has_prior_models and not entry.get("staleness"):
        return ""
    provider = get_provider(preset.id)
    if provider is None:
        return "Codex provider is not registered"
    results = await model_discovery.refresh_all(presets=[provider], only_configured=False)
    if not results:
        return "" if has_prior_models else "Codex model discovery returned no result"
    failure = str(results[0].get("failed_reason") or "")
    if failure and not has_prior_models:
        return failure
    return ""


#: Catalog sources that are real probe EVIDENCE rather than a compiled-in guess.
#: ``live`` is this run's own probe; ``overlay`` is a persisted earlier discovery
#: run (the Codex SDK catalog read / the claude_code alias probe) — both were
#: produced by asking the provider. ``static`` is the frozen registry snapshot
#: and is never evidence.
EVIDENCED_CATALOG_SOURCES: frozenset[str] = frozenset({"live", "overlay"})


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

    evidenced = report.models_source in EVIDENCED_CATALOG_SOURCES and report.ok
    return {
        "provider_id": preset.id,
        "provider_kind": preset.provider,
        "endpoint": preset.api_base,
        "deployment": profile.raw.get("deployment") or profile.raw.get("owned_by") or "",
        "model_id": profile.id,
        "revision": str(profile.raw.get("revision") or profile.raw.get("version") or ""),
        "modalities": _modalities(profile) if evidenced else ["text"],
        "reasoning": {
            "supported": profile.is_reasoning,
            "parameter": profile.reasoning_param or "",
        },
        "native_tool_calling": profile.native_tool_calling,
        "context_window": profile.context_window,
        "loaded_context_window": profile.loaded_context_window,
        "output_limit": profile.output_limit,
        "availability": "available" if evidenced else "candidate",
        "evidence": {
            "source": report.models_source,
            # WHEN the evidence was produced -- a persisted discovery run's own
            # timestamp for an overlay row, this run's clock for a live probe.
            # Reporting the read's wall clock over cached evidence made a stale
            # catalog look freshly generated.
            "generated_at": profile.evidence_generated_at
            or report.evidence_generated_at
            or report.generated_at,
            "read_at": report.generated_at,
            "evidenced": evidenced,
            # ``live`` now means what it says: this run probed the provider.
            "live": report.models_source == "live" and report.ok,
            "context_source": profile.context_source,
            "capability_evidence": profile.raw.get("capability_evidence") or {},
        },
        "failure": report.error or "",
    }


def _overlay_staleness(preset: LMProviderPreset) -> dict[str, Any]:
    """Return the typed staleness marker for this preset's discovery overlay.

    Read at SERVE time, so an entry that was fresh when written is re-examined as
    it ages instead of being handed out forever. A malformed overlay is not a
    staleness question — the handshake path reports that separately — so it
    yields no marker here rather than a fabricated one.
    """

    try:
        wire = model_discovery.overlay_models_wire(preset.id, preset.provider)
    except model_discovery.OverlayMalformedError:
        return {}
    staleness = (wire or {}).get("staleness")
    return staleness if isinstance(staleness, dict) else {}


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
    staleness = _overlay_staleness(preset)
    if preset.id == "codex" and report.models_source not in EVIDENCED_CATALOG_SOURCES:
        # Static Codex ids are compatibility candidates for legacy clients,
        # never evidence that the current account can actually select them.
        # This guard was DEAD while models_source was hardcoded to "live" for
        # every non-empty report; it fires again now that the zero-network CLI
        # handshake reports "static"/"overlay" honestly, and it must test
        # EVIDENCE (live probe or persisted discovery) rather than liveness --
        # Codex's whole catalog arrives through the persisted overlay.
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
            "generated_at": report.evidence_generated_at or report.generated_at or _now_iso(),
            "read_at": report.generated_at or _now_iso(),
            "source": report.models_source,
            **({"staleness": staleness} if staleness else {}),
        },
        "failure": failure or "",
        "models": [model_catalog_row(preset, report, model) for model in models],
    }


__all__ = ["EVIDENCED_CATALOG_SOURCES", "discover_provider", "model_catalog_row"]
