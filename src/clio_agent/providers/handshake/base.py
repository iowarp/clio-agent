"""``ProviderHandshake`` — the async phase template every provider handshake runs.

The base class owns the cross-cutting concerns (an ``httpx.AsyncClient`` with
per-phase timeouts, wall-clock latency, and turning *any* exception into a typed
:class:`HandshakeReport` rather than raising). Subclasses implement only the
provider-specific phases:

    connectivity + auth  ->  discover models  ->  per-model config  ->  enrich capabilities

The enrich step resolves a missing ``context_window`` through the pluggable
context-source factory (provider metadata first, then models.dev, then the
marketplace DB) — see :mod:`clio_agent.providers.handshake.sources`.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    HandshakeReport,
    ModelProfile,
)


@dataclass
class ConnectivityResult:
    """Outcome of the connectivity + auth phase."""

    connectivity: ConnectivityState
    auth: AuthState
    error: str | None = None
    # Auth material resolved during the probe, reused by later phases (e.g. a
    # bearer token) so we authenticate once.
    auth_header: dict[str, str] = field(default_factory=dict)


@dataclass
class HandshakeContext:
    """Inputs to a handshake.

    ``auth_mode`` gates credential acquisition: ``passive`` (health/doctor) must
    never trigger an interactive flow or a network call when no credential is
    present; ``active`` (explicit bind) may refresh a stored token but still must
    not pop a browser. ``allow_external_sources`` enables the models.dev /
    marketplace fallback for context windows; ``mutate_runtime`` permits a side
    effect like an LM Studio reload (set only on explicit bind).
    """

    provider_id: str
    provider_kind: str
    api_base: str
    api_key: str = ""
    target_model: str = ""
    auth_mode: str = "passive"  # "passive" | "active"
    allow_external_sources: bool = True
    mutate_runtime: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class ProviderHandshake(abc.ABC):
    """Abstract per-provider handshake. Subclass and implement the phase methods."""

    #: per-phase HTTP timeouts (seconds); subclasses may override.
    timeout_connect: float = 4.0
    timeout_models: float = 8.0
    timeout_model_config: float = 8.0

    def __init__(self, provider: Any) -> None:
        #: the registry ``Provider`` row this handshake serves.
        self.provider = provider

    async def handshake(self, ctx: HandshakeContext) -> HandshakeReport:
        """Run the full phase sequence, never raising.

        Returns a :class:`HandshakeReport`; connectivity/auth failures short-circuit
        with ``models=()`` and an actionable ``error``.
        """
        started = time.monotonic()
        try:
            client = await self._open_client(ctx)
        except Exception as exc:  # client construction should not fail, but be safe
            return self._report(
                ctx,
                ConnectivityState.UNREACHABLE,
                AuthState.MISSING,
                error=f"client init failed: {exc}",
                started=started,
            )
        try:
            conn = await self.check_connectivity(client, ctx)
            if conn.connectivity != ConnectivityState.OK:
                return self._report(
                    ctx, conn.connectivity, conn.auth, error=conn.error, started=started
                )
            # thread the auth material resolved during connectivity to later phases
            if conn.auth_header:
                ctx.extra["auth_header"] = conn.auth_header
            try:
                raw_models = await self.discover_models(client, ctx)
            except Exception as exc:
                return self._report(
                    ctx,
                    ConnectivityState.OK,
                    conn.auth,
                    error=f"model discovery failed: {exc}",
                    started=started,
                )
            profiles: list[ModelProfile] = []
            for raw in raw_models:
                try:
                    profile = await self.discover_model_config(client, ctx, raw)
                    profile = await self.enrich_capabilities(profile, ctx)
                except Exception:
                    # one bad model row must not sink the whole report
                    continue
                profiles.append(profile)
            return self._report(
                ctx,
                ConnectivityState.OK,
                conn.auth,
                models=tuple(profiles),
                started=started,
            )
        except Exception as exc:  # final backstop — never raise out of a handshake
            return self._report(
                ctx,
                ConnectivityState.UNREACHABLE,
                AuthState.MISSING,
                error=f"handshake error: {exc}",
                started=started,
            )
        finally:
            await self._close_client(client)

    # ------------------------------------------------------------------ phases
    @abc.abstractmethod
    async def check_connectivity(self, client: Any, ctx: HandshakeContext) -> ConnectivityResult:
        """One cheap authenticated probe; the gate for everything downstream."""

    @abc.abstractmethod
    async def discover_models(self, client: Any, ctx: HandshakeContext) -> list[dict[str, Any]]:
        """List the provider's models as raw provider-shaped rows."""

    @abc.abstractmethod
    async def discover_model_config(
        self, client: Any, ctx: HandshakeContext, raw: dict[str, Any]
    ) -> ModelProfile:
        """Build a :class:`ModelProfile` from one raw row's self-reported fields."""

    async def enrich_capabilities(
        self, profile: ModelProfile, ctx: HandshakeContext
    ) -> ModelProfile:
        """Fill a missing ``context_window`` and ``output_limit`` from the factory.

        If the provider already reported a window keep it; otherwise consult
        models.dev -> marketplace -> static (when ``allow_external_sources``). The
        ``output_limit`` (the max output cap) is only tracked by models.dev and is
        resolved independently, since a provider may report context but not output.
        """
        if not ctx.allow_external_sources:
            return profile
        from clio_agent.providers.handshake.sources import (  # noqa: PLC0415
            resolve_context,
            resolve_output_limit,
        )

        updates: dict[str, Any] = {}
        if profile.context_window is None:
            window, source = resolve_context(profile.id, ctx.provider_kind)
            if window is not None:
                updates["context_window"] = window
                updates["context_source"] = source
        if profile.output_limit is None:
            output = resolve_output_limit(profile.id, ctx.provider_kind)
            if output is not None:
                updates["output_limit"] = output
        if not updates:
            return profile
        from dataclasses import replace  # noqa: PLC0415

        return replace(profile, **updates)

    # ------------------------------------------------------------------ helpers
    async def _open_client(self, ctx: HandshakeContext) -> Any:
        import httpx  # noqa: PLC0415

        timeout = httpx.Timeout(
            connect=self.timeout_connect,
            read=max(self.timeout_models, self.timeout_model_config),
            write=self.timeout_connect,
            pool=self.timeout_connect,
        )
        return httpx.AsyncClient(timeout=timeout)

    async def _close_client(self, client: Any) -> None:
        try:
            await client.aclose()
        except Exception:
            pass

    def _report(
        self,
        ctx: HandshakeContext,
        connectivity: ConnectivityState,
        auth: AuthState,
        *,
        models: tuple[ModelProfile, ...] = (),
        error: str | None = None,
        started: float | None = None,
    ) -> HandshakeReport:
        latency = None if started is None else (time.monotonic() - started) * 1000.0
        return HandshakeReport(
            provider_id=ctx.provider_id,
            provider_kind=ctx.provider_kind,
            connectivity=connectivity,
            auth=auth,
            latency_ms=latency,
            error=error,
            models=models,
            models_source="live" if models else ("unavailable" if error else "live"),
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
