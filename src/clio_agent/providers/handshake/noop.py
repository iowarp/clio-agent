"""``NoOpHandshake`` — the handshake for CLI-driven providers with no HTTP surface.

Some providers (the ``codex`` and ``claude_code`` LiteLLM bridges) drive a local
CLI rather than an HTTP endpoint, so there is nothing to *probe*: no ``/models``
route, no auth header, no network at all. But they DO have a known model set —
declared in the provider catalog (:mod:`clio_agent.providers.catalog`) — and
those models still need their **context windows** resolved so budgeting and the
model picker work. So this handshake makes *zero* network calls yet still emits
the registry's candidate models as profiles; the base
:meth:`ProviderHandshake.enrich_capabilities` step then fills each model's
context window from the shared source cascade (provider-self-reported ->
models.dev -> litellm catalog -> local DB).

This is what makes a CLI provider's context discoverable on default config
(iowarp/clio-agent#740). Previously discovery returned ``[]`` so codex /
claude_code models reached the picker and the budgeter with NO context window.
"""

from __future__ import annotations

from typing import Any

from clio_agent.providers.handshake.base import (
    ConnectivityResult,
    HandshakeContext,
    ProviderHandshake,
)
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    ModelProfile,
)


class NoOpHandshake(ProviderHandshake):
    """A handshake that probes nothing — for CLI providers (codex, claude_code).

    No network is touched: connectivity is reported ``OK``/``NOT_REQUIRED`` (the
    CLI is local and manages its own auth), and ``discover_models`` returns the
    provider's registry-declared candidate model set instead of hitting an
    endpoint. The base enrichment step then resolves each model's context window
    from the shared cascade, so CLI providers carry context like any HTTP one.
    """

    def models_provenance(self, ctx: HandshakeContext) -> tuple[str, str]:
        """Report ``static``: these rows are the compiled-in registry catalog.

        This handshake makes zero network calls, so calling its output ``live``
        (the pre-fix behaviour, which stamped ``live`` whenever ANY model
        existed) claimed a probe that never happened -- and made a frozen
        snapshot of candidate ids indistinguishable from account evidence.
        """

        del ctx
        return "static", ""

    async def check_connectivity(self, client: Any, ctx: HandshakeContext) -> ConnectivityResult:
        """Report ``(OK, NOT_REQUIRED)`` without any network call.

        A CLI provider has no endpoint to reach and manages its own credentials.
        We report ``OK`` (not ``SKIPPED``) so the base flow proceeds to model
        discovery + context enrichment — there is nothing to fail on a local CLI,
        and binary presence is surfaced separately by the provider status check.
        """
        return ConnectivityResult(
            connectivity=ConnectivityState.OK,
            auth=AuthState.NOT_REQUIRED,
        )

    async def discover_models(self, client: Any, ctx: HandshakeContext) -> list[dict[str, Any]]:
        """Return the provider's registry-declared candidate models (no network).

        CLI providers expose no ``/models`` listing, but the registry declares the
        candidate model ids; surfacing them here lets the base enrichment resolve
        each one's context window from the cascade.
        """
        from clio_agent.providers.catalog import get_provider  # noqa: PLC0415

        provider = get_provider(ctx.provider_id)
        if provider is None:
            return []
        return [
            {"id": entry.id, "name": entry.name, "description": entry.description}
            for entry in provider.model_catalog
            if getattr(entry, "id", "")
        ]

    async def discover_model_config(
        self, client: Any, ctx: HandshakeContext, raw: dict[str, Any]
    ) -> ModelProfile:
        """Wrap a registry row as a :class:`ModelProfile` (no network access).

        Context/output limits are left ``None`` here; the base
        :meth:`ProviderHandshake.enrich_capabilities` step fills them from the
        source cascade.
        """
        return ModelProfile(id=str(raw.get("id", "")).strip(), raw=dict(raw))
