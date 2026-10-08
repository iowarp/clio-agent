"""Normalized provider/model discovery for resource-aware clients.

The legacy provider routes remain available for older clients. This module
projects their underlying handshake facts into an explicit catalog whose
availability and modalities carry freshness and evidence instead of optimistic
static guesses.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from clio_agent.gact.catalog_context import context_wire
from clio_agent.gact.modality_evidence import DOCUMENTED_MODALITY_REASONS
from clio_agent.gact.types import LMProviderPreset
from clio_agent.lm import dialect_wire
from clio_agent.providers import model_discovery
from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.capabilities.accepted_parameters import accepted_parameters_for
from clio_agent.providers.capabilities.accessor import get_effective_capabilities
from clio_agent.providers.capabilities.endpoint import dialect_for_provider
from clio_agent.providers.capabilities.facts_wire import model_facts
from clio_agent.providers.capabilities.records import (
    role_for_task,
)
from clio_agent.providers.capabilities.tags import capability_tags
from clio_agent.providers.catalog import get_provider
from clio_agent.providers.handshake import HandshakeContext, HandshakeReport, run_handshake
from clio_agent.providers.handshake.model import (
    AuthState,
    DiscoveredModel,
)
from clio_agent.providers.identity import deployment_key

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


#: The key a keyless OpenAI-compatible server is probed with. A server that
#: needs no key (a local vLLM / llama.cpp, a custom self-hosted endpoint)
#: accepts any value, while the OpenAI-compatible handshake skips a probe that
#: carries none -- so without this a keyless preset of kind ``openai`` was
#: never actually asked ("no API key provided").
KEYLESS_PROBE_API_KEY = "EMPTY"


def probe_api_key(preset: LMProviderPreset) -> str:
    """The API key a probe of ``preset`` sends: its stored one, else the keyless placeholder.

    Keyed by provider_id, never provider_kind (Part 3). A sign-in provider
    (oauth/subscription) gets none: its handshake reads the credential its own
    sign-in stored -- the placeholder made ALCF send ``Bearer EMPTY``.
    """
    if preset.auth_method in {"oauth", "subscription"}:
        return ""
    stored = model_discovery.resolve_cloud_api_key(preset.id)
    if stored or preset.requires_api_key:
        return stored
    return KEYLESS_PROBE_API_KEY


def _api_key_for(preset: LMProviderPreset) -> str:
    return probe_api_key(preset)


async def _ensure_codex_live_catalog(preset: LMProviderPreset) -> str:
    """Populate a fresh install from the Codex backend's live model list.

    This is deliberately restricted to Codex. Its catalog read (the Direct
    transport's ``GET /backend-api/codex/models`` with CLIO's own credential)
    is authenticated and does not create a model turn. Claude Code discovery
    validates aliases with real provider calls and therefore remains an
    explicit user action.

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
#: run (the Codex backend model-list read / the claude_code alias probe) —
#: both were produced by asking the provider. ``static`` is the frozen registry
#: snapshot and is never evidence.
EVIDENCED_CATALOG_SOURCES: frozenset[str] = frozenset({"live", "overlay"})

#: Provider kinds whose catalog is the discovery overlay itself (no HTTP probe).
#: Every other kind is probed live and keeps a last-good list for empty probes.
_CLI_CATALOG_KINDS: frozenset[str] = frozenset({"codex", "claude_code"})


#: Codex reasoning-effort vocabulary -> CLIO level, for reading a raw
#: discovered row's own ``default_reasoning_effort`` back into CLIO
#: vocabulary in :func:`_reasoning_wire_block` (the catalog's ``reasoning.
#: default`` field) -- the SAME table :mod:`.capabilities.dialects.codex`
#: uses to build the model's ``ThinkingSpec`` in the first place.
def _codex_to_level() -> dict[str, str]:
    from clio_agent.providers.capabilities.dialects.codex import CODEX_TO_LEVEL  # noqa: PLC0415

    return CODEX_TO_LEVEL


#: The one level an on/off thinking model offers besides "off". The message
#: contract has no "on" token, so the catalog marks the block ``control:
#: "toggle"`` and the picker shows this level as "On".
ON_OFF_LEVEL = "high"


def _reasoning_wire_block(
    effective_thinking: Any, profile: DiscoveredModel, *, dialect: str = ""
) -> dict[str, Any]:
    """Build the catalog's ``reasoning`` wire block straight off the effective
    ``ThinkingDecision`` (model-capabilities brief 5.5) -- the levels a person
    can actually choose for THIS model, never a second, provider-name-keyed
    mapping table (the deleted ``providers.reasoning_levels.model_reasoning``).

    ``default``/``default_source`` prefer a model's own shipped default
    (``profile.raw["shipped_default_effort"]`` -- data, e.g. claude_code's
    maintained catalog) over a provider-reported default
    (``profile.raw["default_reasoning_effort"]`` -- codex's own reported default,
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
    else:
        # "off" only where the request builder sends it (dialect_wire.off_sendable).
        # A toggle has no strengths: one level stands for "on" (every level above
        # off sends the same switch, F017), so no ladder is shown (DIRECTIVES 15,
        # F039). Budgets without per-model levels keep CLIO's generic ladder.
        if spec.mechanism == "on_off":
            ladder = [ON_OFF_LEVEL]
        else:
            ladder = [x for x in spec.levels if x != "off"] or ["low", "medium", "high"]
        levels = [*(["off"] if dialect_wire.off_sendable(dialect, spec) else []), *ladder]

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
        # A known spec is not a reasoning model: mechanism "none" (a template
        # scan that found no thinking) must read as unsupported.
        "supported": bool(levels) or spec.mechanism == "always_on",
        "parameter": effective_thinking.control or "",
        "levels": levels,
        "default": default,
        "default_source": default_source,
        "source": effective_thinking.decided_by,
    }
    if spec.mechanism == "on_off":
        # Render as an Off/On switch: ON_OFF_LEVEL is the wire value for "on".
        block["control"] = "toggle"
    failure = str(profile.raw.get("effort_evidence_failure") or "")
    if failure:
        block["reason"] = failure
    return block


#: Typed :class:`~clio_agent.providers.handshake.model.HandshakeReport.error_code`
#: values that mean "a missing OPTIONAL dependency", never a generic failure --
#: the UI can offer an Install action instead of just reporting broken. Kept
#: here (not on the handshake itself) because it is a catalog-presentation
#: decision: the handshake only reports the fact, this module decides what
#: health state that fact renders as.
NEEDS_INSTALL_ERROR_CODES: frozenset[str] = frozenset({"argonne_sdk_missing"})


def _resolve_health(*, ok: bool, models: Any, error: str, error_code: str) -> tuple[str, str]:
    """Resolve ``(health, failure)`` for one provider (or transport) row.

    READY requires REAL evidence: connectivity+auth ok AND at least one
    discovered model. A recorded failure, or a report that claims success but
    returned zero models, is never "ready" -- a live-verified bug (#1446
    follow-up) had ``argonne_metis`` reporting ``health: "ready"`` with
    ``connectivity: "ok"``, ``models: []`` and an EMPTY failure string,
    because the old check was ``report.ok`` alone. ``error`` is returned
    unchanged when non-empty; a claimed-success-with-zero-models report is
    given a synthesized reason so ``failure`` is never silently empty either.
    """
    if error_code in NEEDS_INSTALL_ERROR_CODES:
        return "needs_install", error or error_code
    if not ok or error:
        return "unavailable", error or "provider connectivity or authentication check failed"
    if not models:
        return "unavailable", "provider discovery reported success but returned zero models"
    return "ready", ""


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
        and capability_evidence.get("reason") in DOCUMENTED_MODALITY_REASONS
    )
    effective = get_effective_capabilities(report.provider_id, report.api_base, profile.id)
    deployment = invalidation.get_deployment_capabilities(
        deployment_key(report.provider_id, report.api_base, profile.id)
    )
    # Three-valued: a row whose discovery never established its modalities says
    # so (``modalities: []`` + ``modality_evidenced: false`` + the provenance
    # reason) instead of presenting "text" -- a gateway /models listing that
    # carries no modality fields is no proof of a text-only model.
    modality_evidenced = (evidenced or modality_evidenced) and effective.input_modalities.known
    modalities = sorted(effective.input_modalities.value or ()) if modality_evidenced else []
    task = effective.task.value if effective.task.known else None
    role = role_for_task(task)
    # Surrogates (embedding, rerank, classification, generation, ...) are
    # first-class catalog rows with their real availability; only choosing one
    # as the CHAT model is refused (typed ``surrogate_model_not_chat``). An
    # unknown role stays selectable -- a chat endpoint offered the model.
    chat_selectable = role != "surrogate"
    availability = "available" if evidenced else "candidate"
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
        # task: the Hugging Face pipeline_tag spelling (text-generation,
        # feature-extraction, text-to-image, text-classification, ...); role:
        # "general" (a chat model) or "surrogate" (everything else). Both null
        # when no source states them.
        "task": task,
        "role": role,
        "chat_selectable": chat_selectable,
        # Evidenced facts the picker renders as tags and filters on; null when
        # no source states them.
        "output_modalities": (
            sorted(effective.output_modalities.value or ())
            if effective.output_modalities.known
            else None
        ),
        "structured_output": effective.structured_output.value,
        "free": effective.free.value,
        "router": effective.router.value,
        # Every tag the picker renders and filters on, each with its evidence
        # (clio_schemas.ModelCapabilityTags). Absent tag = no source stated it.
        "capability_tags": capability_tags(
            effective, model_key=effective.model_key or profile.id
        ).model_dump(mode="json"),
        # Description, release date (+ serve-time ``recent``), pricing per 1M
        # tokens and parameter count, each with its evidence (facts_wire).
        "model_facts": model_facts(effective, model_key=effective.model_key or profile.id),
        # The levels a person can actually choose for THIS model, derived
        # directly from the effective capabilities' own ThinkingDecision --
        # never a second, provider-name-keyed mapping table.
        "reasoning": _reasoning_wire_block(
            effective.thinking,
            profile,
            dialect=dialect_for_provider(preset.provider, preset.litellm_prefix, preset.id),
        ),
        # ONLY the request settings this model accepts, each with its evidence
        # (clio_schemas.AcceptedParameter) -- the same set the request builder sends.
        "accepted_parameters": accepted_parameters_for(
            preset.id, preset.provider, report.provider_id, report.api_base, profile.id, effective
        ),
        "native_tool_calling": bool(effective.tools.value),
        # context_window + loaded/native + the basis it rests on (catalog_context).
        **context_wire(effective, deployment),
        "output_limit": effective.output_max.value,
        "availability": availability,
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
            "modality_evidenced": modality_evidenced,
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
            "task": _provenance_row(effective.task),
            "output_modalities": _provenance_row(effective.output_modalities),
            "free": _provenance_row(effective.free),
            "router": _provenance_row(effective.router),
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
    if report.auth == AuthState.REJECTED:
        # A refused credential is fresher, definitive evidence: the last-good
        # list must not come back dated as "maybe still available" beside it.
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
    health, failure = _resolve_health(
        ok=report.ok, models=models, error=failure, error_code=report.error_code
    )
    payload: dict[str, Any] = {
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
        "health": health,
        "freshness": {
            "generated_at": report.evidence_generated_at or report.generated_at or _now_iso(),
            "read_at": report.generated_at or _now_iso(),
            "source": report.models_source,
            **({"staleness": staleness} if staleness else {}),
        },
        "failure": failure,
        "models": [model_catalog_row(preset, report, model) for model in models],
    }
    if preset.provider == "claude_code":
        # Which CLI the SDK transport runs, as a typed fact on the row (Codex
        # runs none: its one transport is direct).
        from clio_agent.providers.components.client_binary import (  # noqa: PLC0415
            provider_client_fact,
        )

        payload["client"] = await asyncio.to_thread(
            provider_client_fact, preset.provider, refresh=refresh
        )
    return payload


__all__ = [
    "EVIDENCED_CATALOG_SOURCES",
    "NEEDS_INSTALL_ERROR_CODES",
    "discover_provider",
    "model_catalog_row",
]
