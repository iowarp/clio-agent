"""``CliCatalogHandshake`` — :class:`NoOpHandshake` + the #1211 model-catalog overlay.

Extends :class:`~clio_agent.providers.handshake.noop.NoOpHandshake` (zero network
calls; codex/claude_code have no HTTP ``/models`` surface) so ``discover_models``
also consults the persisted refresh overlay
(:mod:`clio_agent.providers.model_discovery`) written by ``POST
/v1/providers/models/refresh`` — an explicit, user-triggered action.

This handshake NEVER re-runs discovery itself: the handshake's read path is hit on
every connect/doctor/model-picker-open, and a live CLI probe there would mean every
one of those pays a real (for claude_code, BILLED) round-trip. It only reads
whatever the last refresh wrote, falling back to the static registry catalog (the
:class:`NoOpHandshake` behavior) when no overlay entry exists yet (fresh install) —
this is what keeps the #740 guarantee (a CLI provider's models always resolve a
context window) intact regardless of whether a refresh has ever run.

**Context/output limits (#1211 review D4).** ``model_discovery`` resolves each
discovered model's context/output limit ONCE, at explicit refresh time, and
persists the result (a hit OR a definitive miss) onto its overlay row — see
:func:`clio_agent.providers.model_discovery.overlay.attach_context_limits`. This
handshake reads that pre-filled value back in :meth:`discover_model_config` and
:meth:`enrich_capabilities` SKIPS the models.dev/litellm/local-DB cascade entirely
for an overlay-sourced row, whether the persisted value is a real number or a
confirmed miss. Without this, every passive/ambient handshake call (a bind, a
doctor probe, opening the model picker) would re-attempt the cascade for every
CLI-provider model on every call whenever its on-disk models.dev cache happens to
be stale — the real cost D4 identifies (verified live: a stale cache makes
``lookup_models_dev`` attempt a fresh network fetch, which is not instant even
when it succeeds, and can time out when it doesn't; this is NOT specific to any
one model id — a fully warm cache serves ANY id, novel or not, from a local dict
lookup with no network call at all).

**Residual (#1211 review S3, stated honestly).** D4's fix only applies once a
model is OVERLAY-sourced — i.e. after at least one successful refresh. A
fresh install (or a provider that has never had ``POST
/v1/providers/models/refresh`` run against it) still falls through to the
base :class:`NoOpHandshake` cascade on ITS first ambient handshake call, same
as pre-#1211: the very first bind/doctor-probe/model-picker-open for that
provider can still pay one un-amortized models.dev/litellm/local-DB lookup.
This is NOT fixed by this handshake — only the STEADY STATE (every call
after that first one, and every call once a refresh has run) is. Proactively
triggering a refresh at first-connect was considered and rejected: it would
turn a passive, ambient read path into an action that fires network calls
(BILLED ones, for claude_code) the user never asked for — the #1211 design
keeps refresh explicit and user-triggered (``/update-models``), so this
cold-cascade cost is an accepted, bounded (one-time-per-provider-per-cache-
staleness-window) trade-off, not an oversight.
"""

from __future__ import annotations

import logging
from typing import Any

from clio_agent.providers.handshake.base import HandshakeContext
from clio_agent.providers.handshake.model import ModelProfile
from clio_agent.providers.handshake.noop import NoOpHandshake

logger = logging.getLogger(__name__)

#: Marks a ``discover_models`` row as overlay-sourced (context/output limits
#: already resolved at refresh time) vs. static-catalog-sourced (never resolved
#: yet — the normal cascade must still run for it).
_OVERLAY_CHECKED_KEY = "_overlay_context_checked"

#: ``ctx.extra`` key carrying the overlay entry's OWN ``generated_at`` — when the
#: discovery run that produced this evidence actually happened, as distinct from
#: the wall clock of the passive read that served it.
_OVERLAY_GENERATED_AT_KEY = "overlay_generated_at"


def _overlay_capabilities(row: dict[str, Any]) -> tuple[str, ...]:
    """Return capability strings persisted by live CLI model discovery."""

    values = row.get("capabilities")
    if not isinstance(values, list):
        return ()
    return tuple(str(value).strip() for value in values if str(value).strip())


class CliCatalogHandshake(NoOpHandshake):
    """:class:`NoOpHandshake` variant whose model list prefers the refresh overlay."""

    #: The overlay carries the capabilities an explicit discovery run evidenced
    #: (the Codex SDK's reported input modalities, the claude_code native probe),
    #: so this provider kind HAS a modality-evidence system -- absence of a
    #: modality here means "not evidenced yet", never "nobody could ask".
    reports_input_modalities = True

    async def discover_models(self, client: Any, ctx: HandshakeContext) -> list[dict[str, Any]]:
        """Return the overlay's discovered models when present, else the static catalog.

        A malformed on-disk overlay must not break this passive/ambient path (it
        runs on hot paths like connect/doctor, and the GACT server must always
        work — RULE 2); it degrades to the static catalog, same as a missing
        overlay. The corruption is still surfaced loudly on the diagnostic
        ``GET /v1/providers/{id}/models`` route and the refresh response, which
        read the overlay directly and let the error propagate; here it is logged
        (#1211 review R5) so the degrade is never silent even though it is
        deliberately non-fatal.
        """
        from clio_agent.providers import model_discovery  # noqa: PLC0415

        try:
            wire = model_discovery.overlay_models_wire(ctx.provider_id, ctx.provider_kind)
        except model_discovery.OverlayMalformedError as exc:
            logger.warning(
                "cli_catalog: overlay malformed for provider=%s (falling back to the "
                "static registry catalog): %s",
                ctx.provider_id,
                exc,
            )
            wire = None
        if wire and wire.get("models"):
            # Remember WHEN this evidence was produced. Without it the report
            # stamps the read's wall clock, so a months-old cached catalog is
            # served as if it had just been generated.
            ctx.extra[_OVERLAY_GENERATED_AT_KEY] = str(wire.get("generated_at") or "")
            return [
                {
                    "id": str(m.get("id") or ""),
                    "name": str(m.get("name") or ""),
                    "description": str(m.get("description") or ""),
                    "context_window": m.get("context_window"),
                    "output_limit": m.get("output_limit"),
                    "context_source": m.get("context_source"),
                    "capabilities": list(_overlay_capabilities(m)),
                    # The typed modality provenance discovery recorded for this
                    # row (why a modality is present OR absent). Forwarded so
                    # the capability negative stays queryable downstream instead
                    # of arriving as an anonymous empty list.
                    "capability_evidence": m.get("capability_evidence") or {},
                    _OVERLAY_CHECKED_KEY: True,
                }
                for m in wire["models"]
                if isinstance(m, dict) and m.get("id")
            ]
        return await super().discover_models(client, ctx)

    def models_provenance(self, ctx: HandshakeContext) -> tuple[str, str]:
        """Report ``overlay`` + the discovery run's own timestamp, else ``static``.

        The overlay is real evidence — a Codex SDK catalog read or a claude_code
        alias probe actually ran — but it is not THIS run's evidence, and it can
        be arbitrarily old. Both facts are reported rather than collapsed into
        the ``live`` the base class used to stamp unconditionally. With no
        overlay entry the rows are the static registry catalog, so
        :class:`NoOpHandshake`'s answer stands.
        """

        generated_at = str(ctx.extra.get(_OVERLAY_GENERATED_AT_KEY) or "")
        if _OVERLAY_GENERATED_AT_KEY not in ctx.extra:
            return super().models_provenance(ctx)
        return "overlay", generated_at

    async def discover_model_config(
        self, client: Any, ctx: HandshakeContext, raw: dict[str, Any]
    ) -> ModelProfile:
        """Build a :class:`ModelProfile`, pre-filled from the overlay when available (D4).

        An overlay-sourced row (flagged by :meth:`discover_models`) already
        carries its context/output limit resolved at refresh time — real values
        OR a confirmed miss (``None``); either way, that information rides onto
        the profile here so :meth:`enrich_capabilities` can skip the cascade
        entirely. A static-catalog-sourced row (no overlay yet) falls through to
        the base :class:`NoOpHandshake` behavior unchanged (context left unset,
        so the cascade still runs for it — the pre-#1211 behavior, unaffected).
        """
        if not raw.get(_OVERLAY_CHECKED_KEY):
            return await super().discover_model_config(client, ctx, raw)
        context_window = raw.get("context_window")
        output_limit = raw.get("output_limit")
        return ModelProfile(
            id=str(raw.get("id", "")).strip(),
            context_window=context_window
            if isinstance(context_window, int) and context_window > 0
            else None,
            output_limit=output_limit
            if isinstance(output_limit, int) and output_limit > 0
            else None,
            capabilities=_overlay_capabilities(raw),
            context_source=str(raw.get("context_source") or "overlay"),
            evidence_generated_at=str(ctx.extra.get(_OVERLAY_GENERATED_AT_KEY) or ""),
            raw=dict(raw),
        )

    async def enrich_capabilities(
        self, profile: ModelProfile, ctx: HandshakeContext
    ) -> ModelProfile:
        """Skip the context-source cascade entirely for an overlay-checked profile (D4).

        The base :meth:`ProviderHandshake.enrich_capabilities` re-runs
        ``resolve_context``/``resolve_output_limit`` whenever ``context_window``/
        ``output_limit`` is ``None`` — which is indistinguishable from "never
        checked" unless the caller marks it. An overlay-sourced profile WAS
        checked (at refresh time, by ``attach_context_limits``); a ``None`` here
        means a CONFIRMED miss, not an unresolved value, so re-running the
        cascade on every ambient handshake call would just repeat the same
        (possibly network-touching) miss forever. Returns the profile unchanged
        for those; delegates to the base cascade for everything else.
        """
        if profile.raw.get(_OVERLAY_CHECKED_KEY):
            return profile
        return await super().enrich_capabilities(profile, ctx)


__all__ = ["CliCatalogHandshake"]
