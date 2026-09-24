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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.handshake.model import HandshakeReport, ModelProfile
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

#: ModelProfile fields persisted per row so a served last-good profile keeps the
#: discovered context window and reasoning/tool facts, not just the id.
_PROFILE_FIELDS: tuple[str, ...] = (
    "context_window",
    "loaded_context_window",
    "native_context_window",
    "output_limit",
    "is_reasoning",
    "reasoning_param",
    "native_tool_calling",
    "tool_call_parser",
    "quantization",
    "arch",
    "context_source",
)


#: How stale the PERSISTED ``confirmed_at`` may get before a live confirmation
#: rewrites it. Keeps it accurate to the hour across restarts without a disk
#: write per discovery; this process's own value is always exact.
CONFIRMED_AT_PERSIST_INTERVAL_S = 3600.0

#: provider id -> ISO time of this process's latest live confirmation.
_CONFIRMED_IN_PROCESS: dict[str, str] = {}


@dataclass(frozen=True)
class LastGoodCatalog:
    """A persisted live model list, when it was discovered and last confirmed."""

    profiles: tuple[ModelProfile, ...]
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


def profile_rows(report: HandshakeReport) -> list[dict[str, Any]]:
    """Serialize a report's profiles to overlay rows (``id``/``name`` plus profile facts)."""

    rows: list[dict[str, Any]] = []
    for profile in report.models:
        if not profile.id:
            continue
        row: dict[str, Any] = {"id": profile.id, "name": profile.id, "description": ""}
        for name in _PROFILE_FIELDS:
            value = getattr(profile, name)
            if value is not None:
                row[name] = value
        row["capabilities"] = list(profile.capabilities)
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
    rows = profile_rows(report)
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


def _profile_from_row(row: dict[str, Any], generated_at: str) -> ModelProfile:
    capabilities = row.get("capabilities")
    return ModelProfile(
        id=str(row.get("id") or ""),
        context_window=_int_or_none(row.get("context_window")),
        loaded_context_window=_int_or_none(row.get("loaded_context_window")),
        native_context_window=_int_or_none(row.get("native_context_window")),
        output_limit=_int_or_none(row.get("output_limit")),
        is_reasoning=bool(row.get("is_reasoning")),
        reasoning_param=str(row.get("reasoning_param") or "") or None,
        native_tool_calling=bool(row.get("native_tool_calling")),
        tool_call_parser=str(row.get("tool_call_parser") or "") or None,
        quantization=str(row.get("quantization") or "") or None,
        arch=str(row.get("arch") or "") or None,
        capabilities=tuple(str(c) for c in capabilities) if isinstance(capabilities, list) else (),
        context_source=str(row.get("context_source") or LAST_GOOD_CATALOG_SOURCE),
        evidence_generated_at=generated_at,
        raw={"id": str(row.get("id") or ""), "last_good": True},
    )


def last_good_catalog(provider_id: str) -> LastGoodCatalog | None:
    """Return the provider's persisted last good live list, or ``None``.

    Looks up the EXACT provider id (never the provider kind: two ALCF clusters
    share a kind but not a model list) and only entries a live handshake wrote.
    A malformed overlay is logged with a typed reason and yields ``None``.
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
    profiles = tuple(
        _profile_from_row(row, generated_at)
        for row in rows
        if isinstance(row, dict) and str(row.get("id") or "")
    )
    if not profiles:
        return None
    confirmed_at = _latest(
        _CONFIRMED_IN_PROCESS.get(provider_id, ""),
        str(entry.get("confirmed_at") or ""),
        generated_at,
    )
    return LastGoodCatalog(
        profiles=profiles, generated_at=generated_at, confirmed_at=confirmed_at or generated_at
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
    "last_good_catalog",
    "last_good_staleness",
    "persist_live_catalog",
    "profile_rows",
]
