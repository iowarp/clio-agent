"""Last-good live model catalogs for HTTP-backed providers.

An HTTP provider's passive handshake can come back empty for reasons that say
nothing about which models it serves: ALCF's stored Globus sign-in cannot mint
an access token yet at boot, the gateway is briefly unreachable, a local server
has not started. Serving nothing until someone presses "Check provider" made a
signed-in ALCF account look empty after every restart.

The last good LIVE discovery is therefore persisted in the existing refresh
overlay (:mod:`.overlay`, ``model_catalog.json`` — no new store), keyed by the
exact provider id with ``source == HTTP_SOURCE``. When a later live probe yields
no models, the catalog serves that list marked with a typed
``last_good_catalog_served`` staleness carrying WHEN it was discovered and WHY the
live probe did not replace it. Rows served this way are never evidence of
current availability (the catalog marks them ``candidate``); the next live probe
that answers replaces them.

Per the model-capabilities brief, what gets persisted is now the
:class:`~clio_agent.providers.capabilities.records.ModelCapabilities` /
:class:`~clio_agent.providers.capabilities.records.DeploymentCapabilities`
facts each model carried (a compact snapshot: context/output limits, tools,
modalities) alongside the bare
:class:`~clio_agent.providers.handshake.model.DiscoveredModel` identity — not
the deleted flat ``ModelProfile``. Reloading a last-good catalog writes that
snapshot straight back into the shared capability store
(:mod:`clio_agent.providers.capabilities.invalidation`) with
``source="server_report"`` (the evidence really did come from a server, just
not THIS run), so every downstream capability question still routes through
the one accessor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
    unknown,
)
from clio_agent.providers.handshake.model import DiscoveredModel, HandshakeReport
from clio_agent.providers.model_discovery.overlay import (
    HTTP_SOURCE,
    OverlayMalformedError,
    ProviderDiscoveryResult,
    read_overlay,
    record_refresh,
    update_entry_fields,
)

logger = logging.getLogger(__name__)

#: The catalog ``freshness.source`` / evidence ``source`` for a served last-good list.
LAST_GOOD_CATALOG_SOURCE = "last_good"

#: Typed staleness reason, in the ``stream_fallback`` reason-catalog style.
LAST_GOOD_REASONS: dict[str, str] = {
    "last_good_catalog_served": (
        "the live provider check returned no models, so the models this provider "
        "served at its last successful check are shown instead; they are prior "
        "evidence, not proof the models are available now"
    ),
}

#: How stale the PERSISTED ``confirmed_at`` may get before a live confirmation
#: rewrites it. Keeps it accurate to the hour across restarts without a disk
#: write per discovery; this process's own value is always exact.
CONFIRMED_AT_PERSIST_INTERVAL_S = 3600.0

#: provider id -> ISO time of this process's latest live confirmation.
_CONFIRMED_IN_PROCESS: dict[str, str] = {}


@dataclass(frozen=True)
class LastGoodCatalog:
    """A persisted live model list, when it was discovered and last confirmed."""

    models: tuple[DiscoveredModel, ...]
    generated_at: str
    #: When a live check last answered with this list (>= ``generated_at``).
    confirmed_at: str = ""


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _latest(*values: str) -> str:
    dated = [(parsed, value) for value in values if value and (parsed := _parse(value))]
    return max(dated)[1] if dated else ""


def _note_confirmation(provider_id: str, previous: dict[str, Any], confirmed_at: str) -> None:
    """Record a live confirmation: exact in memory, persisted at most hourly."""

    _CONFIRMED_IN_PROCESS[provider_id] = _latest(
        confirmed_at, _CONFIRMED_IN_PROCESS.get(provider_id, "")
    )
    stored = _parse(str(previous.get("confirmed_at") or previous.get("generated_at") or ""))
    now = _parse(confirmed_at)
    if stored is not None and now is not None:
        if (now - stored).total_seconds() < CONFIRMED_AT_PERSIST_INTERVAL_S:
            return
    try:
        update_entry_fields(provider_id, {"confirmed_at": confirmed_at})
    except OverlayMalformedError as exc:
        logger.warning(
            "last-good confirmation not persisted: reason=overlay_malformed provider=%s error=%s",
            provider_id,
            exc,
        )


def _capability_snapshot(report: HandshakeReport, model_id: str) -> dict[str, Any]:
    """A compact, JSON-safe snapshot of one model's currently-known facts.

    Reads straight from the capability store (whatever this handshake run just
    wrote there) rather than recomputing anything — persistence is a pure
    passthrough of what was already evidenced.
    """

    from clio_agent.providers.identity import deployment_key  # noqa: PLC0415

    deployment = invalidation.get_deployment_capabilities(
        deployment_key(report.provider_id, report.api_base, model_id)
    )
    model_key = deployment.model_key.value if deployment and deployment.model_key.known else None
    model = invalidation.get_model_capabilities(model_key) if model_key else None

    snapshot: dict[str, Any] = {}
    if model is not None:
        if model.context_max.known:
            snapshot["context_max"] = model.context_max.value
        if model.output_max.known:
            snapshot["output_max"] = model.output_max.value
        if model.tools.known:
            snapshot["tools"] = model.tools.value
        if model.input_modalities.known:
            snapshot["input_modalities"] = sorted(model.input_modalities.value or ())
    if deployment is not None:
        if deployment.context_served.known:
            snapshot["context_served"] = deployment.context_served.value
        if deployment.output_max.known:
            snapshot["deployment_output_max"] = deployment.output_max.value
        if deployment.tools_enabled.known:
            snapshot["tools_enabled"] = deployment.tools_enabled.value
        if deployment.reasoning_enabled.known:
            snapshot["reasoning_enabled"] = deployment.reasoning_enabled.value
    return snapshot


def discovered_model_rows(report: HandshakeReport) -> list[dict[str, Any]]:
    """Serialize a report's discovered models to overlay rows.

    Each row carries the bare :class:`~clio_agent.providers.handshake.model.
    DiscoveredModel` identity plus a compact capability snapshot
    (:func:`_capability_snapshot`) so a later reload can rebuild usable
    capability records, not just an id.
    """

    rows: list[dict[str, Any]] = []
    for row_model in report.models:
        if not row_model.id:
            continue
        row: dict[str, Any] = {
            "id": row_model.id,
            "name": row_model.id,
            "description": "",
            "is_loaded": row_model.is_loaded,
        }
        if row_model.aliases:
            row["cli_values"] = list(row_model.aliases)
        capabilities = row_model.raw.get("capabilities")
        if isinstance(capabilities, list):
            row["capabilities"] = list(capabilities)
        for passthrough in ("quantization", "arch"):
            value = row_model.raw.get(passthrough)
            if value:
                row[passthrough] = value
        row["capability_snapshot"] = _capability_snapshot(report, row_model.id)
        rows.append(row)
    return rows


def persist_live_catalog(provider_id: str, report: HandshakeReport) -> bool:
    """Persist ``report``'s models as the provider's last good list; return whether written.

    The overlay is rewritten only when the model list actually changed.

    Only a report that really probed the provider and answered with models is
    persisted. An overlay that cannot be read-modify-written is logged with a
    typed reason and left untouched (the live answer is still served this time).
    """

    if not (report.ok and report.models and report.models_source == "live"):
        return False
    rows = discovered_model_rows(report)
    if not rows:
        return False
    try:
        previous = read_overlay().get(provider_id)
    except OverlayMalformedError:
        previous = None  # record_refresh below surfaces the malformed file, typed
    if (
        isinstance(previous, dict)
        and previous.get("source") == HTTP_SOURCE
        and previous.get("models") == rows
        and not previous.get("failed_reason")
    ):
        # Unchanged list: no rewrite (the overlay file is shared by every
        # provider); only its confirmation time moves forward.
        _note_confirmation(
            provider_id,
            previous,
            report.generated_at or datetime.now(timezone.utc).isoformat(),
        )
        return False
    result = ProviderDiscoveryResult(provider=provider_id, discovered=rows, source=HTTP_SOURCE)
    if report.generated_at:
        # The probe's own clock, so the served timestamp is when it was evidenced.
        result.generated_at = report.generated_at
    try:
        record_refresh(result)
        update_entry_fields(provider_id, {"confirmed_at": result.generated_at})
    except OverlayMalformedError as exc:
        logger.warning(
            "last-good catalog not persisted: reason=overlay_malformed provider=%s error=%s",
            provider_id,
            exc,
        )
        return False
    _CONFIRMED_IN_PROCESS[provider_id] = result.generated_at
    return True


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _seed_capability_store(
    provider_id: str, api_base: str, model_id: str, snapshot: dict[str, Any], generated_at: str
) -> None:
    """Write a persisted snapshot back into the shared capability store.

    ``source="server_report"``: the evidence really came from a live server at
    some point, it just isn't from THIS run -- exactly the same honesty
    :meth:`~clio_agent.providers.handshake.base.ProviderHandshake.models_provenance`
    already applies at the report level for ``last_good``/``overlay`` rows.
    """

    if not snapshot:
        return
    model = ModelCapabilities(
        model_key=model_id,
        context_max=(
            Fact(value=snapshot["context_max"], source="server_report", observed_at=generated_at)
            if "context_max" in snapshot
            else unknown()
        ),
        output_max=(
            Fact(value=snapshot["output_max"], source="server_report", observed_at=generated_at)
            if "output_max" in snapshot
            else unknown()
        ),
        tools=(
            Fact(value=snapshot["tools"], source="server_report", observed_at=generated_at)
            if "tools" in snapshot
            else unknown()
        ),
        input_modalities=(
            Fact(
                value=frozenset(snapshot["input_modalities"]),
                source="server_report",
                observed_at=generated_at,
            )
            if "input_modalities" in snapshot
            else unknown()
        ),
    )
    deployment = DeploymentCapabilities(
        provider_id=provider_id,
        api_base=api_base,
        model_id=model_id,
        model_key=Fact(value=model_id, source="server_report", observed_at=generated_at),
        context_served=(
            Fact(value=snapshot["context_served"], source="server_report", observed_at=generated_at)
            if "context_served" in snapshot
            else unknown()
        ),
        output_max=(
            Fact(
                value=snapshot["deployment_output_max"],
                source="server_report",
                observed_at=generated_at,
            )
            if "deployment_output_max" in snapshot
            else unknown()
        ),
        tools_enabled=(
            Fact(value=snapshot["tools_enabled"], source="server_report", observed_at=generated_at)
            if "tools_enabled" in snapshot
            else unknown()
        ),
        reasoning_enabled=(
            Fact(
                value=snapshot["reasoning_enabled"],
                source="server_report",
                observed_at=generated_at,
            )
            if "reasoning_enabled" in snapshot
            else unknown()
        ),
    )
    invalidation.record_model_capabilities(model)
    invalidation.record_deployment_capabilities(deployment)


def _discovered_from_row(row: dict[str, Any], generated_at: str) -> DiscoveredModel:
    capabilities = row.get("capabilities")
    aliases = row.get("cli_values")
    raw: dict[str, Any] = {"id": str(row.get("id") or ""), "last_good": True}
    if isinstance(capabilities, list):
        raw["capabilities"] = list(capabilities)
    for passthrough in ("quantization", "arch"):
        if row.get(passthrough):
            raw[passthrough] = row[passthrough]
    return DiscoveredModel(
        id=str(row.get("id") or ""),
        is_loaded=bool(row.get("is_loaded")),
        aliases=tuple(str(a) for a in aliases) if isinstance(aliases, list) else (),
        evidence_generated_at=generated_at,
        raw=raw,
    )


def last_good_catalog(provider_id: str, *, api_base: str = "") -> LastGoodCatalog | None:
    """Return the provider's persisted last good live list, or ``None``.

    Looks up the EXACT provider id (never the provider kind: two ALCF clusters
    share a kind but not a model list) and only entries a live handshake wrote.
    Also re-seeds the shared capability store from each row's persisted
    snapshot, so a served last-good row still answers real capability
    questions through the one accessor. ``api_base`` is the caller's
    currently-configured endpoint (the overlay file itself does not persist
    one) -- passing it lets the reseeded deployment records key correctly;
    omitting it only means the reseed is inert until a live probe runs, never
    an error. A malformed overlay is logged with a typed reason and yields
    ``None``.
    """

    try:
        overlay = read_overlay()
    except OverlayMalformedError as exc:
        logger.warning(
            "last-good catalog unavailable: reason=overlay_malformed provider=%s error=%s",
            provider_id,
            exc,
        )
        return None
    entry = overlay.get(provider_id)
    if not isinstance(entry, dict) or entry.get("source") != HTTP_SOURCE:
        return None
    rows = entry.get("models")
    if not isinstance(rows, list):
        return None
    generated_at = str(entry.get("generated_at") or "")
    models: list[DiscoveredModel] = []
    for row in rows:
        if not isinstance(row, dict) or not str(row.get("id") or ""):
            continue
        models.append(_discovered_from_row(row, generated_at))
        snapshot = row.get("capability_snapshot")
        if isinstance(snapshot, dict):
            _seed_capability_store(provider_id, api_base, str(row["id"]), snapshot, generated_at)
    if not models:
        return None
    confirmed_at = _latest(
        _CONFIRMED_IN_PROCESS.get(provider_id, ""),
        str(entry.get("confirmed_at") or ""),
        generated_at,
    )
    return LastGoodCatalog(
        models=tuple(models), generated_at=generated_at, confirmed_at=confirmed_at or generated_at
    )


def last_good_staleness(catalog: LastGoodCatalog, report: HandshakeReport) -> dict[str, Any]:
    """The typed staleness marker for a served last-good list."""

    live_failure = report.error or (
        f"connectivity={report.connectivity.value} auth={report.auth.value}"
    )
    return {
        "reason": "last_good_catalog_served",
        "description": LAST_GOOD_REASONS["last_good_catalog_served"],
        "generated_at": catalog.generated_at,
        # When a live check last answered with this list -- the date a person reads.
        "confirmed_at": catalog.confirmed_at or catalog.generated_at,
        "live_failure": live_failure,
    }


__all__ = [
    "LAST_GOOD_CATALOG_SOURCE",
    "LAST_GOOD_REASONS",
    "LastGoodCatalog",
    "discovered_model_rows",
    "last_good_catalog",
    "last_good_staleness",
    "persist_live_catalog",
]
