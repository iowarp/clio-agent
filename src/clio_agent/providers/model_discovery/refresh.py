"""The refresh action: every configured provider, concurrently, deadline-bounded
(iowarp/clio-agent#1211). Also the ``refresh_provider_models`` agent tool
(#1211 review R6, expert-pool-primary doctrine)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from clio_agent.providers.catalog import Provider, iter_providers
from clio_agent.providers.model_discovery.claude_code import (
    ClaudeCodeCLIUnavailableError,
    discover_claude_code,
)
from clio_agent.providers.model_discovery.codex import discover_codex
from clio_agent.providers.model_discovery.http import discover_http
from clio_agent.providers.model_discovery.overlay import (
    OverlayMalformedError,
    ProviderDiscoveryResult,
    record_refresh,
    resolve_cloud_api_key,
)

logger = logging.getLogger(__name__)

#: Hard per-provider wall-clock cap for one ``refresh_all`` probe coroutine
#: (#1211 review R2/R3) — bounds the WHOLE refresh action's wall-clock (providers
#: run concurrently) regardless of a wedged subprocess or a slow network path.
REFRESH_PER_PROVIDER_DEADLINE_S = 90.0


def is_provider_configured(preset: Provider) -> bool:
    """Whether ``preset`` has usable auth/binary presence to attempt a refresh probe.

    A cheap, LOCAL check (no network, no subprocess) — distinct from the probe
    itself, which is what actually determines whether the provider answers.
    Filters :func:`refresh_all`'s default scan so an explicit refresh doesn't
    spend its (bounded) wall-clock on providers nobody has set up:

    * codex / claude_code: the CLI must be on PATH.
    * argonne: a stored Globus token must exist.
    * any other ``requires_api_key`` kind: its resolved API key must be non-empty.
    * local/no-auth kinds (lm_studio, ollama, local vLLM): always configured —
      the probe itself (a live connect attempt) is what tells you whether the
      local server is actually running.
    """
    if preset.provider_kind == "codex":
        from clio_agent.providers.codex_litellm import (  # noqa: PLC0415
            CodexCLIUnavailableError,
            _resolve_codex_binary,
        )

        try:
            _resolve_codex_binary()
            return True
        except CodexCLIUnavailableError:
            return False
    if preset.provider_kind == "claude_code":
        from clio_agent.providers.model_discovery.claude_code import (  # noqa: PLC0415
            _resolve_claude_binary,
        )

        try:
            _resolve_claude_binary()
            return True
        except ClaudeCodeCLIUnavailableError:
            return False
    if preset.provider_kind == "argonne":
        try:
            from clio_agent.providers import argonne_auth  # noqa: PLC0415

            return argonne_auth.tokens_exist()
        except Exception:  # noqa: BLE001 - argonne unavailability means "not configured"
            return False
    if preset.requires_api_key:
        return bool(resolve_cloud_api_key(preset.provider_kind))
    return True


async def refresh_all(
    presets: list[Provider] | None = None, *, only_configured: bool = True
) -> list[dict[str, Any]]:
    """Probe providers concurrently; merge each result into the overlay.

    ``presets=None`` (the default, driven by ``POST /v1/providers/models/refresh``
    with no body) scans the full catalog and — when ``only_configured`` — filters
    to :func:`is_provider_configured` providers only (#1211 review R2): a refresh
    doesn't spend its bounded wall-clock probing providers nobody has set up. An
    EXPLICIT ``presets`` list (the route's optional ``{"providers": [...]}`` body,
    #1211 review R3) is honored verbatim, un-filtered — the caller named exactly
    what they want probed.

    Runs one discovery coroutine per preset (CLI probes for codex/claude_code,
    the live handshake for everything else) via ``asyncio.gather`` so wall-clock
    is bounded by the SLOWEST single provider, not their sum. Each provider's
    coroutine is ADDITIONALLY capped at :data:`REFRESH_PER_PROVIDER_DEADLINE_S`
    (#1211 review R2/R3) — a wedged subprocess or a hung network call can never
    block the whole refresh past that ceiling; the record still gets a typed
    ``failed_reason`` for that provider, others unaffected. A crash inside one
    provider's coroutine (the ``discover_*`` functions are designed to never
    raise, but a defensive backstop) is caught the same way.

    ``record_refresh`` failures (a rare :class:`OverlayMalformedError`, e.g. a
    concurrent external write) are caught PER PROVIDER (#1211 review N2) so one
    provider's write failure never discards the other providers' already-recorded
    results.
    """
    all_presets = list(presets) if presets is not None else list(iter_providers())
    if presets is None and only_configured:
        all_presets = [p for p in all_presets if is_provider_configured(p)]

    async def _discover(preset: Provider) -> ProviderDiscoveryResult:
        if preset.provider_kind == "codex":
            return await asyncio.to_thread(discover_codex)
        if preset.provider_kind == "claude_code":
            return await asyncio.to_thread(discover_claude_code)
        return await discover_http(preset, api_key=resolve_cloud_api_key(preset.provider_kind))

    async def _one(preset: Provider) -> ProviderDiscoveryResult:
        try:
            return await asyncio.wait_for(
                _discover(preset), timeout=REFRESH_PER_PROVIDER_DEADLINE_S
            )
        except TimeoutError:
            return ProviderDiscoveryResult(
                provider=preset.id,
                discovered=[],
                source="error",
                failed_reason=f"refresh timed out after {REFRESH_PER_PROVIDER_DEADLINE_S}s",
            )
        except Exception as exc:  # noqa: BLE001 - one provider's crash must not sink the refresh
            logger.warning("model discovery crashed for provider=%s: %s", preset.id, exc)
            return ProviderDiscoveryResult(
                provider=preset.id,
                discovered=[],
                source="error",
                failed_reason=f"discovery crashed: {exc}",
            )

    results = await asyncio.gather(*(_one(p) for p in all_presets))
    recorded: list[dict[str, Any]] = []
    for r in results:
        try:
            recorded.append(record_refresh(r))
        except OverlayMalformedError as exc:
            logger.warning(
                "record_refresh failed for provider=%s: overlay malformed: %s", r.provider, exc
            )
            recorded.append(
                {
                    "provider": r.provider,
                    "discovered": [],
                    "source": r.source,
                    "default_model": "",
                    "generated_at": r.generated_at,
                    "added": [],
                    "removed": [],
                    "unchanged": [],
                    "failed_reason": f"overlay_malformed: {exc}",
                }
            )
    return recorded


def refresh_all_sync(
    presets: list[Provider] | None = None, *, only_configured: bool = True
) -> list[dict[str, Any]]:
    """Synchronous bridge for :func:`refresh_all` (mirrors ``handshake.run_handshake_sync``).

    Uses the established asyncio.run-or-threadpool bridge so it is safe whether
    or not an event loop is already running on the calling thread — the shape a
    ``dspy.Tool`` callable (a plain sync function) needs.
    """
    import concurrent.futures  # noqa: PLC0415

    async def _go() -> list[dict[str, Any]]:
        return await refresh_all(presets, only_configured=only_configured)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_go())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(_go())).result()


def build_refresh_provider_models_tool() -> Any:
    """Build the ``refresh_provider_models`` dspy.Tool (#1211 review R6).

    The sanctioned, in-band way for an expert to trigger a model-catalog
    refresh — no guessing at the server's own loopback port with a shell
    ``curl`` (the expert-pool-primary doctrine: a capability gets a real tool,
    not an instruction to reach for a generic escape hatch). Returns the SAME
    typed per-provider rows ``POST /v1/providers/models/refresh`` does
    (``provider``/``discovered``/``source``/``default_model``/``added``/
    ``removed``/``unchanged``/``failed_reason``/``rejected``), so the
    ``/update-models`` skill's report-the-delta instruction applies unchanged
    whether the caller went through the route or this tool.
    """
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    def refresh_provider_models() -> dict[str, Any]:
        """Refresh the LM provider model catalogs against each account's REAL
        current state (codex's live model list, claude_code's alias
        probe-validation, every configured HTTP backend's live models
        endpoint) and report what changed. Returns
        ``{"results": [{"provider", "discovered", "source", "default_model",
        "added", "removed", "unchanged", "failed_reason"?, "rejected"?}, ...]}``
        — one row per configured provider. A provider whose probe failed keeps
        its previous list (never silently cleared) and carries a typed
        ``failed_reason``."""

        return {"results": refresh_all_sync()}

    return native_tool(
        refresh_provider_models,
        name="refresh_provider_models",
        desc=refresh_provider_models.__doc__,
        title="Refresh Provider Models",
        args={},
    )


__all__ = [
    "REFRESH_PER_PROVIDER_DEADLINE_S",
    "build_refresh_provider_models_tool",
    "is_provider_configured",
    "refresh_all",
    "refresh_all_sync",
]
