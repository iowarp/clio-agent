"""Normalized provider/model discovery for resource-aware clients.

The legacy provider routes remain available for older clients. This module
projects their underlying handshake facts into an explicit catalog whose
availability and modalities carry freshness and evidence instead of optimistic
static guesses.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from clio_agent.gact.types import LMProviderPreset
from clio_agent.providers import model_discovery
from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.capabilities.accessor import get_effective_capabilities
from clio_agent.providers.catalog import get_provider
from clio_agent.providers.handshake import HandshakeContext, HandshakeReport, run_handshake
from clio_agent.providers.handshake.model import DiscoveredModel
from clio_agent.providers.identity import deployment_key


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _api_key_for(preset: LMProviderPreset) -> str:
    # Keyed by provider_id, never provider_kind (Part 3): nine presets share
    # the kind "openai", and a kind-keyed lookup here previously resolved
    # openrouter/nvidia_nim to the literal OpenAI provider's env var.
    return model_discovery.resolve_cloud_api_key(preset.id)


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

#: Provider kinds whose catalog is the discovery overlay itself (no HTTP probe).
#: Every other kind is probed live and keeps a last-good list for empty probes.
_CLI_CATALOG_KINDS: frozenset[str] = frozenset({"codex", "claude_code"})

#: capability_evidence reasons (providers/model_discovery/modality_evidence.py)
#: that count as a real, non-guessed "this row documents its own modalities"
#: claim -- mirrors gact/resource_delivery.py's own constant of the same name;
#: kept here too since this module reads capability_evidence independently.
_DOCUMENTED_MODALITY_REASONS: frozenset[str] = frozenset(
    {"modality_documented", "modality_cataloged"}
)


#: Codex SDK reasoning-effort vocabulary -> CLIO level, for reading a raw
#: discovered row's own ``default_reasoning_effort`` back into CLIO
#: vocabulary in :func:`_reasoning_wire_block` (the catalog's ``reasoning.
#: default`` field) -- the SAME table :mod:`.capabilities.dialects.codex`
#: uses to build the model's ``ThinkingSpec`` in the first place.
def _codex_to_level() -> dict[str, str]:
    from clio_agent.providers.capabilities.dialects.codex import CODEX_TO_LEVEL  # noqa: PLC0415

    return CODEX_TO_LEVEL


def _reasoning_wire_block(effective_thinking: Any, profile: DiscoveredModel) -> dict[str, Any]:
    """Build the catalog's ``reasoning`` wire block straight off the effective
    ``ThinkingDecision`` (model-capabilities brief 5.5) -- the levels a person
    can actually choose for THIS model, never a second, provider-name-keyed
    mapping table (the deleted ``providers.reasoning_levels.model_reasoning``).

    ``default``/``default_source`` prefer a model's own shipped default
    (``profile.raw["shipped_default_effort"]`` -- data, e.g. claude_code's
    maintained catalog) over a provider-reported default
    (``profile.raw["default_reasoning_effort"]`` -- codex's own SDK default,
    translated through its own vocabulary table), and are empty when neither
    applies or the value isn't among this model's own effective levels.
    """

    spec = effective_thinking.spec
    if spec is None:
        return {
            "supported": False,
            "parameter": "",
            "levels": [],
            "default": "",
            "default_source": "",
            "source": "no_thinking_record",
        }
    if spec.mechanism in ("none", "always_on"):
        levels: list[str] = []
    elif spec.levels:
        levels = ["off", *spec.levels]
    else:
        # budget_tokens with no explicit per-model levels: CLIO's own generic
        # ladder is what the request builder actually offers (dialect_wire.py).
        levels = ["off", "low", "medium", "high"]

    default = ""
    default_source = ""
    shipped = str(profile.raw.get("shipped_default_effort") or "")
    if shipped and shipped in levels:
        default, default_source = shipped, "clio_shipped"
    else:
        raw_default = str(profile.raw.get("default_reasoning_effort") or "")
        mapped = _codex_to_level().get(raw_default, "") if raw_default else ""
        if mapped and mapped in levels:
            default, default_source = mapped, "provider"

    block: dict[str, Any] = {
        "supported": bool(levels) or effective_thinking.known,
        "parameter": effective_thinking.control or "",
        "levels": levels,
        "default": default,
        "default_source": default_source,
        "source": effective_thinking.decided_by,
    }
    failure = str(profile.raw.get("effort_evidence_failure") or "")
    if failure:
        block["reason"] = failure
    return block


def model_catalog_row(
    preset: LMProviderPreset,
    report: HandshakeReport,
    profile: DiscoveredModel,
) -> dict[str, Any]:
    """Return one normalized model row with explicit discovery evidence.

    Every capability field (``modalities``, ``native_tool_calling``,
    ``context_window``, ``output_limit``, the ``reasoning`` block) is read from
    the effective capabilities (model-capabilities brief 5.5) for
    ``(provider_id, api_base, model_id)`` -- never a flat per-provider profile
    field. ``capabilities_provenance`` adds the source/observed_at/decided_by
    each effective value carries, for the P7 UI (show provenance, don't tell
    it) -- the wire contract's existing keys are otherwise unchanged.
    """

    evidenced = report.models_source in EVIDENCED_CATALOG_SOURCES and report.ok
    capability_evidence = profile.raw.get("capability_evidence") or {}
    modality_evidenced = (
        isinstance(capability_evidence, dict)
        and capability_evidence.get("reason") in _DOCUMENTED_MODALITY_REASONS
    )
    effective = get_effective_capabilities(report.provider_id, report.api_base, profile.id)
    deployment = invalidation.get_deployment_capabilities(
        deployment_key(report.provider_id, report.api_base, profile.id)
    )
    loaded_context_window = (
        deployment.context_served.value if deployment and deployment.context_served.known else None
    )
    modalities = (
        sorted(effective.input_modalities.value or ())
        if (evidenced or modality_evidenced) and effective.input_modalities.known
        else ["text"]
    )
    return {
        "provider_id": preset.id,
        "provider_kind": preset.provider,
        "endpoint": preset.api_base,
        "deployment": profile.raw.get("deployment") or profile.raw.get("owned_by") or "",
        "model_id": profile.id,
        "revision": str(profile.raw.get("revision") or profile.raw.get("version") or ""),
        # The CLI values (e.g. claude_code's "sonnet") that select this row --
        # the same resolution the provider itself uses (claude_code_effort.py),
        # never a hand-typed table. Empty for providers with no alias concept.
        "aliases": [str(a) for a in profile.raw.get("cli_values") or [] if str(a).strip()],
        "modalities": modalities,
        # The levels a person can actually choose for THIS model, derived
        # directly from the effective capabilities' own ThinkingDecision --
        # never a second, provider-name-keyed mapping table.
        "reasoning": _reasoning_wire_block(effective.thinking, profile),
        "native_tool_calling": bool(effective.tools.value),
        "context_window": effective.context.value,
        "loaded_context_window": loaded_context_window,
        "output_limit": effective.output_max.value,
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
            "modality_evidenced": evidenced or modality_evidenced,
            # ``live`` now means what it says: this run probed the provider.
            "live": report.models_source == "live" and report.ok,
            "context_source": effective.context.decided_by,
            "capability_evidence": capability_evidence,
        },
        "capabilities_provenance": {
            "context_window": _provenance_row(effective.context),
            "output_limit": _provenance_row(effective.output_max),
            "native_tool_calling": _provenance_row(effective.tools),
            "modalities": _provenance_row(effective.input_modalities),
            "reasoning": _provenance_row(effective.thinking),
        },
        "failure": report.error or "",
    }


def _provenance_row(decision: Any) -> dict[str, Any]:
    """One effective value's provenance, for the P7 UI (source/observed_at/decided_by)."""

    return {
        "source": getattr(decision, "source", "") or "",
        "observed_at": getattr(decision, "observed_at", "") or "",
        "decided_by": getattr(decision, "decided_by", "unknown"),
        "reason": getattr(decision, "reason", ""),
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


async def _with_last_good(
    preset: LMProviderPreset, report: HandshakeReport
) -> tuple[HandshakeReport, dict[str, Any]]:
    """Persist a live answer as last-good, or serve the last-good list for an empty probe.

    Returns the report to project (unchanged, or carrying the last-good profiles
    under ``models_source="last_good"``) and the typed staleness marker for the
    latter. Last-good rows are never ``evidenced`` -- ``report.ok`` is false for an
    empty probe -- so they surface as candidates until a live probe answers.
    """

    if preset.provider in _CLI_CATALOG_KINDS:
        return report, {}
    if report.models:
        await asyncio.to_thread(model_discovery.persist_live_catalog, preset.id, report)
        return report, {}
    last_good = await asyncio.to_thread(
        model_discovery.last_good_catalog, preset.id, api_base=preset.api_base
    )
    if last_good is None:
        return report, {}
    served = replace(
        report,
        models=last_good.models,
        models_source=model_discovery.LAST_GOOD_CATALOG_SOURCE,
        evidence_generated_at=last_good.generated_at,
    )
    return served, model_discovery.last_good_staleness(last_good, report)


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
    report, staleness = await _with_last_good(preset, report)
    models = report.models
    failure = report.error or bootstrap_failure
    if not staleness:
        staleness = _overlay_staleness(preset) if preset.provider in _CLI_CATALOG_KINDS else {}
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
        # The one canonical display name; the sign-in service is separate detail.
        "auth_method": preset.auth_method,
        "auth_label": preset.auth_label,
        # The client's real provider-settings route (a query, not a path segment).
        "configuration_url": f"/settings/providers?provider={preset.id}",
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
