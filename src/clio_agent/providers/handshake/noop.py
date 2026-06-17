"""``NoOpHandshake`` — the handshake for CLI-driven providers with no HTTP surface.

Some providers (the ``codex`` and ``claude_code`` LiteLLM bridges) drive a local
CLI rather than an HTTP endpoint, so there is nothing to probe: no ``/models``
route, no auth header, no network at all. This handshake satisfies the
:class:`ProviderHandshake` contract while making *zero* network calls — every
phase short-circuits to a benign, terminal result so the dispatcher can treat
these providers uniformly with the HTTP ones.
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

    All three phases return immediately without touching ``client``: connectivity
    is ``(SKIPPED, NOT_REQUIRED)``, discovery yields no models, and per-model
    config (never reached, since discovery is empty) is a trivial passthrough.
    """

    async def check_connectivity(self, client: Any, ctx: HandshakeContext) -> ConnectivityResult:
        """Report ``(SKIPPED, NOT_REQUIRED)`` without any network call.

        A CLI provider has no endpoint to reach and manages its own credentials, so
        connectivity is deliberately skipped and auth is not required.
        """
        return ConnectivityResult(
            connectivity=ConnectivityState.SKIPPED,
            auth=AuthState.NOT_REQUIRED,
        )

    async def discover_models(self, client: Any, ctx: HandshakeContext) -> list[dict[str, Any]]:
        """Return no models — a CLI provider exposes no listing endpoint."""
        return []

    async def discover_model_config(
        self, client: Any, ctx: HandshakeContext, raw: dict[str, Any]
    ) -> ModelProfile:
        """Trivially wrap a raw row as a :class:`ModelProfile` (never called in practice).

        Discovery returns no rows, so this is only here to satisfy the abstract
        contract; it performs no network access.
        """
        return ModelProfile(id=str(raw.get("id", "")).strip(), raw=dict(raw))
