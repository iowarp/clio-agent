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

import importlib.util
import logging
from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.capabilities.link import deployment_model_key_fact
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
    modalities_from_capabilities,
    unknown,
)
from clio_agent.providers.handshake.base import ConnectivityResult, HandshakeContext
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    DiscoveredModel,
    DiscoveredModelFacts,
    raw_aliases,
)
from clio_agent.providers.handshake.noop import NoOpHandshake


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    #: (the Codex SDK's reported input modalities, the maintained claude_code
    #: catalog's declared capabilities), so this provider kind HAS a
    #: modality-evidence system -- absence of a modality here means "not
    #: evidenced yet", never "nobody could ask".
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
                    # Per-model reasoning efforts the discovery run recorded
                    # (Codex SDK catalog); the provider catalog derives the
                    # selectable thinking levels from them.
                    "supported_reasoning_efforts": list(m.get("supported_reasoning_efforts") or []),
                    "default_reasoning_effort": str(m.get("default_reasoning_effort") or ""),
                    # Claude Code CLI per-model effort evidence (initialize response).
                    "supported_effort_levels": list(m.get("supported_effort_levels") or []),
                    "cli_values": list(m.get("cli_values") or []),
                    "effort_evidence_failure": str(m.get("effort_evidence_failure") or ""),
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
    ) -> DiscoveredModelFacts:
        """Build :class:`DiscoveredModelFacts`, pre-filled from the overlay when available (D4).

        An overlay-sourced row (flagged by :meth:`discover_models`) already
        carries its context/output limit resolved at refresh time — real values
        OR a confirmed miss (``None``); either way, that information rides onto
        the model record here so :meth:`enrich_capabilities` can skip the
        cascade entirely (it checks the SAME ``_OVERLAY_CHECKED_KEY`` sentinel
        on the raw row, not whether a fact is known, so a confirmed miss is
        never mistaken for "not checked yet" and re-attempted). A
        static-catalog-sourced row (no overlay yet) falls through to the base
        :class:`NoOpHandshake` behavior unchanged (the cascade still runs for
        it — the pre-#1211 behavior, unaffected).
        """
        if not raw.get(_OVERLAY_CHECKED_KEY):
            return await super().discover_model_config(client, ctx, raw)
        model_id = str(raw.get("id", "")).strip()
        context_window = raw.get("context_window")
        output_limit = raw.get("output_limit")
        # The evidence's OWN timestamp (when the discovery run that produced
        # this overlay row happened), never the wall clock of this passive read.
        observed_at = str(ctx.extra.get(_OVERLAY_GENERATED_AT_KEY) or "") or _now_iso()
        caps = _overlay_capabilities(raw)
        detail = "persisted refresh-overlay evidence (clio_agent.providers.model_discovery.overlay)"
        model_key_fact = deployment_model_key_fact(model_id, observed_at=observed_at)
        model_key = model_key_fact.value or model_id
        model = ModelCapabilities(
            model_key=model_key,
            context_max=(
                Fact(
                    value=context_window,
                    source="server_report",
                    observed_at=observed_at,
                    detail=detail,
                )
                if isinstance(context_window, int) and context_window > 0
                else unknown()
            ),
            output_max=(
                Fact(
                    value=output_limit,
                    source="server_report",
                    observed_at=observed_at,
                    detail=detail,
                )
                if isinstance(output_limit, int) and output_limit > 0
                else unknown()
            ),
            input_modalities=(
                Fact(
                    value=modalities_from_capabilities(caps),
                    source="server_report",
                    observed_at=observed_at,
                    detail=detail,
                )
                if caps
                else unknown()
            ),
        )
        deployment = DeploymentCapabilities(
            provider_id=ctx.provider_id,
            api_base=ctx.api_base,
            model_id=model_id,
            model_key=model_key_fact,
        )
        discovered = DiscoveredModel(
            id=model_id,
            aliases=raw_aliases(raw),
            evidence_generated_at=str(ctx.extra.get(_OVERLAY_GENERATED_AT_KEY) or ""),
            raw=dict(raw),
        )
        return DiscoveredModelFacts(discovered=discovered, model=model, deployment=deployment)

    async def enrich_capabilities(
        self, facts: DiscoveredModelFacts, ctx: HandshakeContext
    ) -> DiscoveredModelFacts:
        """Skip the community-catalog cascade entirely for an overlay-checked row (D4).

        The base
        :meth:`~clio_agent.providers.handshake.base.ProviderHandshake.enrich_capabilities`
        re-runs the cascade whenever ``context_max``/``output_max`` is unknown
        -- which is indistinguishable from "never checked" unless the caller
        marks it. An overlay-sourced row WAS checked (at refresh time, by
        ``attach_context_limits``); an unknown fact here means a CONFIRMED
        miss, not an unresolved value, so re-running the cascade on every
        ambient handshake call would just repeat the same (possibly
        network-touching) miss forever. Returns ``facts`` unchanged for those;
        delegates to the base cascade for everything else.
        """
        if facts.discovered.raw.get(_OVERLAY_CHECKED_KEY):
            return facts
        return await super().enrich_capabilities(facts, ctx)


class CodexCatalogHandshake(CliCatalogHandshake):
    """Codex catalog handshake gated by verified subscription credentials."""

    async def check_connectivity(self, client: Any, ctx: HandshakeContext) -> ConnectivityResult:
        """Reject synthetic readiness until a fresh SDK catalog check exists."""

        del client
        if importlib.util.find_spec("openai_codex") is None:
            return ConnectivityResult(
                connectivity=ConnectivityState.UNREACHABLE,
                auth=AuthState.MISSING,
                error="official openai-codex Python SDK is not installed",
            )
        from clio_agent.providers import model_discovery  # noqa: PLC0415
        from clio_agent.providers.codex_credential_home import (  # noqa: PLC0415
            codex_credentials_present,
        )

        if not codex_credentials_present():
            return ConnectivityResult(
                connectivity=ConnectivityState.SKIPPED,
                auth=AuthState.MISSING,
                error="Codex sign-in is required on the connected agent",
            )
        try:
            overlay = model_discovery.overlay_models_wire(ctx.provider_id, ctx.provider_kind)
        except model_discovery.OverlayMalformedError as exc:
            return ConnectivityResult(
                connectivity=ConnectivityState.UNREACHABLE,
                auth=AuthState.DEFERRED,
                error=f"Codex model catalog is invalid: {exc}",
            )
        if overlay and overlay.get("models") and not overlay.get("staleness"):
            return ConnectivityResult(
                connectivity=ConnectivityState.OK,
                auth=AuthState.OK,
            )
        return ConnectivityResult(
            connectivity=ConnectivityState.SKIPPED,
            auth=AuthState.DEFERRED,
            error="Codex credentials are present but have not been validated",
        )


class ClaudeCodeCatalogHandshake(CliCatalogHandshake):
    """Claude Code handshake gated by installed SDK and a fresh live probe."""

    async def check_connectivity(self, client: Any, ctx: HandshakeContext) -> ConnectivityResult:
        """Reject synthetic readiness until Claude Code answers a live probe."""

        del client
        from clio_agent.providers.claude_code_errors import (  # noqa: PLC0415
            CLAUDE_CODE_NOT_INSTALLED_MESSAGE,
        )

        if importlib.util.find_spec("claude_agent_sdk") is None:
            return ConnectivityResult(
                connectivity=ConnectivityState.UNREACHABLE,
                auth=AuthState.MISSING,
                error=CLAUDE_CODE_NOT_INSTALLED_MESSAGE,
            )
        from clio_agent.providers import model_discovery  # noqa: PLC0415

        try:
            overlay = model_discovery.overlay_models_wire(ctx.provider_id, ctx.provider_kind)
        except model_discovery.OverlayMalformedError as exc:
            return ConnectivityResult(
                connectivity=ConnectivityState.UNREACHABLE,
                auth=AuthState.DEFERRED,
                error=f"Claude Code model catalog is invalid: {exc}",
            )
        if overlay and overlay.get("models") and not overlay.get("staleness"):
            return ConnectivityResult(
                connectivity=ConnectivityState.OK,
                auth=AuthState.OK,
            )
        return ConnectivityResult(
            connectivity=ConnectivityState.SKIPPED,
            auth=AuthState.DEFERRED,
            error="Claude Code is installed but has not been verified. Check the provider to sign in.",
        )


__all__ = ["ClaudeCodeCatalogHandshake", "CliCatalogHandshake", "CodexCatalogHandshake"]
