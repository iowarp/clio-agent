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

from clio_agent.gact.types import LMProviderPreset
from clio_agent.providers import model_discovery
from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.capabilities.accessor import get_effective_capabilities
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
    modalities_from_capabilities,
    role_for_task,
    unknown,
)
from clio_agent.providers.capabilities.tags import capability_tags
from clio_agent.providers.catalog import get_provider
from clio_agent.providers.handshake import HandshakeContext, HandshakeReport, run_handshake
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    DiscoveredModel,
    DiscoveredModelFacts,
)
from clio_agent.providers.identity import deployment_key
from clio_agent.providers.reasoning_levels import model_reasoning

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

    Keyed by provider_id, never provider_kind (Part 3): nine presets share the
    kind "openai", and a kind-keyed lookup previously resolved
    openrouter/nvidia_nim to the literal OpenAI provider's env var.
    """
    stored = model_discovery.resolve_cloud_api_key(preset.id)
    if stored or preset.requires_api_key:
        return stored
    return KEYLESS_PROBE_API_KEY


def _api_key_for(preset: LMProviderPreset) -> str:
    return probe_api_key(preset)


async def _ensure_codex_live_catalog(preset: LMProviderPreset) -> str:
    """Populate a fresh install from the maintained Codex model catalog.

    This is deliberately restricted to Codex. Its catalog read (the
    maintained document plus a credential-store sign-in check) is local/
    authenticated and does not create a model turn. Claude Code discovery
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
#: run (the maintained Codex catalog read / the claude_code alias probe) —
#: both were produced by asking the provider. ``static`` is the frozen registry
#: snapshot and is never evidence.
EVIDENCED_CATALOG_SOURCES: frozenset[str] = frozenset({"live", "overlay"})

#: Provider kinds whose catalog is the discovery overlay itself (no HTTP probe).
#: Every other kind is probed live and keeps a last-good list for empty probes.
_CLI_CATALOG_KINDS: frozenset[str] = frozenset({"codex", "claude_code"})

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
        and capability_evidence.get("reason") == "modality_documented"
    )
    effective = get_effective_capabilities(report.provider_id, report.api_base, profile.id)
    deployment = invalidation.get_deployment_capabilities(
        deployment_key(report.provider_id, report.api_base, profile.id)
    )
    loaded_context_window = (
        deployment.context_served.value if deployment and deployment.context_served.known else None
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
        # no source states them. ``pricing`` is per token, as the provider
        # states it, with "variable" for a price that depends on the routed
        # model (never 0).
        "output_modalities": (
            sorted(effective.output_modalities.value or ())
            if effective.output_modalities.known
            else None
        ),
        "structured_output": effective.structured_output.value,
        "free": effective.free.value,
        "router": effective.router.value,
        "pricing": dict(effective.pricing.value or {}) if effective.pricing.known else None,
        # Every tag the picker renders and filters on, each with its evidence
        # (clio_schemas.ModelCapabilityTags). Absent tag = no source stated it.
        "capability_tags": capability_tags(
            effective, model_key=effective.model_key or profile.id
        ).model_dump(mode="json"),
        # The levels a person can actually choose for THIS model, derived from
        # provider truth and restricted to what resolve_thinking maps.
        "reasoning": model_reasoning(
            preset.provider,
            profile,
            is_reasoning=effective.thinking.known,
            reasoning_param=effective.thinking.control or "",
        ),
        "native_tool_calling": bool(effective.tools.value),
        "context_window": effective.context.value,
        "loaded_context_window": loaded_context_window,
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
            "pricing": _provenance_row(effective.pricing),
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


#: Overlay key the SDK transport's discovery is recorded under. Deliberately
#: NOT ``"codex"`` -- that key belongs to the direct transport's own catalog
#: (``providers.codex.credentials``/``codex_catalog.py``) and must never be
#: overwritten by the SDK's live probe result.
_CODEX_SDK_OVERLAY_KEY = "codex_sdk"

#: The SDK transport's own endpoint identity (the ``codex://sdk`` pseudo-scheme
#: the runtime status probe already names). The direct transport is the preset's
#: ``codex://direct``; keying the SDK's capability records apart keeps one
#: transport's facts from overwriting the other's for a shared model id.
_CODEX_SDK_API_BASE = "codex://sdk"

#: A reason prefix meaning "we simply have not asked yet" -- the least
#: actionable of all possible unavailable reasons, so a transport that HAS
#: been asked (and failed) is preferred when picking which reason to surface.
_UNCHECKED_REASON_PREFIX = "codex_sdk_not_checked"


async def _codex_sdk_transport_row(preset: LMProviderPreset, *, refresh: bool) -> dict[str, Any]:
    """Build the ``sdk`` transport row.

    The SDK probe is a real subprocess/JSON-RPC round trip (unlike the direct
    transport's cheap local credential-store check), so it is never run on an
    ordinary passive catalog read (``GET /v1/provider-catalog``, polled on
    every session load) -- only on an explicit ``refresh`` (a "check this
    provider" action, or the startup bootstrap in
    ``refresh_subscription_catalogs_at_startup``). A passive read serves
    whatever the last explicit check recorded, through the SAME overlay
    machinery the direct transport already uses (no-silent-fallback: a failed
    re-check keeps the previous good list rather than going blank).
    """
    from clio_agent.providers.codex import constants as codex_constants

    now = _now_iso()
    checked = True
    if refresh:
        from clio_agent.providers.codex.sdk_discovery import discover_codex_sdk_async

        result = await discover_codex_sdk_async()
        try:
            model_discovery.record_refresh(result)
        except model_discovery.OverlayMalformedError as exc:
            logger.warning("codex sdk transport overlay write failed: %s", exc)
        discovered = result.discovered
        failed_reason = result.failed_reason or ""
        evidence_generated_at = result.generated_at
    else:
        try:
            wire = model_discovery.overlay_models_wire(
                _CODEX_SDK_OVERLAY_KEY, _CODEX_SDK_OVERLAY_KEY
            )
        except model_discovery.OverlayMalformedError as exc:
            wire = None
            logger.warning("codex sdk transport overlay read failed: %s", exc)
        if wire is None:
            # No overlay entry at all means "never asked", not "asked and
            # empty" -- distinct from a real zero-model result, which
            # _resolve_health's generic reason already explains correctly.
            checked = False
            discovered, failed_reason, evidence_generated_at = [], "", ""
        else:
            discovered = list(wire.get("models") or [])
            evidence_generated_at = str(wire.get("generated_at") or "")
            staleness = wire.get("staleness")
            failed_reason = (
                str(staleness.get("failed_reason") or "") if isinstance(staleness, dict) else ""
            )
    if checked:
        health, failure = _resolve_health(
            ok=not failed_reason,
            models=discovered,
            error=failed_reason,
            error_code="",
        )
    else:
        health, failure = (
            "unavailable",
            (f"{_UNCHECKED_REASON_PREFIX}: run an explicit provider check to ask the Codex SDK"),
        )
    profiles = tuple(
        _record_sdk_model(preset.id, row, observed_at=evidence_generated_at or now)
        for row in discovered
        if isinstance(row, dict) and row.get("id")
    )
    sdk_report = HandshakeReport(
        provider_id=preset.id,
        provider_kind=preset.provider,
        api_base=_CODEX_SDK_API_BASE,
        connectivity=ConnectivityState.OK if health == "ready" else ConnectivityState.UNREACHABLE,
        auth=AuthState.OK if health == "ready" else AuthState.MISSING,
        error=failure or None,
        models=profiles,
        models_source=(
            "live"
            if refresh and health == "ready"
            else "overlay"
            if health == "ready"
            else "unavailable"
        ),
        generated_at=now,
        evidence_generated_at=evidence_generated_at or now,
    )
    models = [model_catalog_row(preset, sdk_report, profile) for profile in profiles]
    for row in models:
        row["transport"] = codex_constants.TRANSPORT_SDK
    return {
        "id": codex_constants.TRANSPORT_SDK,
        "label": codex_constants.TRANSPORT_LABELS[codex_constants.TRANSPORT_SDK],
        "health": health,
        "reason": failure,
        "models": models,
    }


def _record_sdk_model(
    provider_id: str, row: dict[str, Any], *, observed_at: str
) -> DiscoveredModel:
    """Record one SDK-discovered model's facts and return its bare identity.

    The SDK transport is its own endpoint (:data:`_CODEX_SDK_API_BASE`), so its
    records never overwrite the direct transport's for the same model id. The
    row's ``capabilities`` list is the SDK's own report of the model's input
    modalities; with no list, modalities stay unknown.
    """
    model_id = str(row["id"])
    capabilities = row.get("capabilities")
    detail = "Codex SDK model/list capabilities"
    key_fact = Fact(value=model_id, source="server_report", observed_at=observed_at, detail=detail)
    facts = DiscoveredModelFacts(
        discovered=DiscoveredModel(id=model_id, evidence_generated_at=observed_at, raw=dict(row)),
        model=ModelCapabilities(
            model_key=model_id,
            input_modalities=(
                Fact(
                    value=modalities_from_capabilities(capabilities),
                    source="server_report",
                    observed_at=observed_at,
                    detail=detail,
                )
                if isinstance(capabilities, list) and capabilities
                else unknown()
            ),
        ),
        deployment=DeploymentCapabilities(
            provider_id=provider_id,
            api_base=_CODEX_SDK_API_BASE,
            model_id=model_id,
            model_key=key_fact,
        ),
    )
    invalidation.record_model_capabilities(facts.model)
    invalidation.record_deployment_capabilities(facts.deployment)
    return facts.discovered


def _codex_direct_transport_row(
    preset: LMProviderPreset,
    *,
    report: HandshakeReport,
    models: tuple[DiscoveredModel, ...],
    health: str,
    failure: str,
) -> dict[str, Any]:
    """Build the ``direct`` transport row from the already-run generic handshake.

    The direct transport IS what :func:`discover_provider`'s generic pipeline
    (the maintained catalog + :class:`~clio_agent.providers.codex.credentials.
    CodexCredentialStore`, refreshed through the overlay) has always computed
    for the ``codex`` provider -- this just relabels that result as one of the
    two transports rather than the whole provider.
    """
    # Lazy: provider_auth -> provider_catalog_snapshot -> this module.
    from clio_agent.gact.routes.provider_auth import supports_logout  # noqa: PLC0415
    from clio_agent.providers.codex import constants as codex_constants

    rows = [model_catalog_row(preset, report, model) for model in models]
    for row in rows:
        row["transport"] = codex_constants.TRANSPORT_DIRECT
    return {
        "id": codex_constants.TRANSPORT_DIRECT,
        "label": codex_constants.TRANSPORT_LABELS[codex_constants.TRANSPORT_DIRECT],
        "health": health,
        "reason": failure,
        # `logout` comes from the SAME registry POST .../auth {action: logout}
        # dispatches on, so the client never infers "Sign out" on its own.
        "auth": {"method": "oauth", "logout": supports_logout(preset.provider)},
        "models": rows,
    }


#: Health rows are folded into ONE provider-level health with this precedence
#: (owner requirement: "it is also green if any of them are available").
_TRANSPORT_HEALTH_PRECEDENCE: tuple[str, ...] = ("ready", "needs_install", "unavailable")


def _combine_transport_health(transports: list[dict[str, Any]]) -> tuple[str, str]:
    """Fold per-transport health into the provider's overall ``(health, failure)``.

    READY if ANY transport is ready. Otherwise the best remaining state (a
    transport offering an install action outranks a plain failure), carrying
    that transport's own reason so the row is never "unavailable" with an
    unexplained empty ``failure`` -- preferring a transport that was actually
    CHECKED over one that reports only "not checked yet" when both share a
    health bucket.
    """
    by_health: dict[str, dict[str, Any]] = {}
    for transport in transports:
        health = str(transport.get("health"))
        current = by_health.get(health)
        if current is None:
            by_health[health] = transport
            continue
        current_reason = str(current.get("reason") or "")
        if current_reason.startswith(_UNCHECKED_REASON_PREFIX):
            by_health[health] = transport
    for health in _TRANSPORT_HEALTH_PRECEDENCE:
        if health in by_health:
            reason = "" if health == "ready" else str(by_health[health].get("reason") or "")
            return health, reason
    return "unavailable", "no codex transport reported a health state"


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
    if preset.provider == "codex":
        # Two transports of the SAME catalog entry (S1b): the direct/OAuth
        # transport this pipeline already evidenced above, and the restored
        # local SDK transport (probed live only on an explicit ``refresh``;
        # a passive read serves its last recorded overlay entry). The
        # provider is READY when EITHER is (owner requirement); duplicate
        # model ids across the two transports are expected and each carries
        # its own ``transport`` tag so a selection routes end to end.
        direct_row = _codex_direct_transport_row(
            preset, report=report, models=models, health=health, failure=failure
        )
        sdk_row = await _codex_sdk_transport_row(preset, refresh=refresh)
        transports = [sdk_row, direct_row]
        combined_health, combined_failure = _combine_transport_health(transports)
        payload["transports"] = transports
        payload["health"] = combined_health
        payload["failure"] = combined_failure
        payload["models"] = [*sdk_row["models"], *direct_row["models"]]
    return payload


__all__ = [
    "EVIDENCED_CATALOG_SOURCES",
    "NEEDS_INSTALL_ERROR_CODES",
    "discover_provider",
    "model_catalog_row",
]
