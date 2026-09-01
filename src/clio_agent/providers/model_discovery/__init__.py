"""Provider model-catalog discovery + the refresh overlay (iowarp/clio-agent#1211).

The static per-provider model lists in :mod:`clio_agent.providers.catalog` are a
compiled-in snapshot; CLI-routed accounts (codex, claude_code) rotate their served
model ids independently of a clio release, so a snapshot goes stale (iowarp/clio-
agent#1184: the catalog offered ``gpt-5.5``/``gpt-5.5-codex``/``gpt-5.1`` after the
ChatGPT channel had moved on to ``gpt-5.6-sol``). This package is the single owner
of, one submodule per concern (kept split to respect the #775 file-size ratchet):

* :mod:`.overlay` — the refresh overlay: read/write/delta, malformed-vs-unreadable
  typed errors, the ``ProviderDiscoveryResult`` shape, and the context/output-limit
  enrichment persisted at refresh time (#1211 review D4).
* :mod:`.codex` — Codex discovery via the official Python SDK model catalog,
  verified live against codex-cli 0.147.0 (see :func:`discover_codex`).
* :mod:`.claude_code` — claude_code discovery via per-alias probe-validation (no
  enumeration endpoint exists for this channel) — a rejected alias comes back as
  a typed 404-shaped error in the CLI's own JSON envelope, the universal
  probe-validation oracle; a TRANSIENT probe failure (timeout/429/5xx/launch
  failure) is NEVER treated as a rejection (#1211 review D3).
* :mod:`.http` — HTTP-backed providers reuse the existing live handshake `/models`
  path.
* :mod:`.refresh` — the concurrent, deadline-bounded, configured-providers-only
  refresh action (#1211 review R2/R3) and the ``refresh_provider_models`` agent
  tool (#1211 review R6, expert-pool-primary doctrine).

``GET /v1/providers/{id}/models`` (:mod:`clio_agent.gact.routes.providers`) serves
the overlay ahead of the static fallback for the CLI provider kinds ONLY —
HTTP-backed providers always keep their live handshake path (#1211 review D5).
The passive handshake seam (:mod:`clio_agent.providers.handshake.cli_catalog`)
consults the overlay — never live-reprobes it.

No-silent-fallback (CLAUDE.md cleanup-program ground rule): a probe failure for
one provider NEVER clears that provider's existing overlay entry — the previous
good list plus a typed ``failed_reason`` are both recorded, and a malformed
on-disk overlay raises :class:`OverlayMalformedError` rather than silently
degrading to ``{}`` (the #1202 ``_read_mcp_yaml`` lesson).
"""

from __future__ import annotations

from clio_agent.providers.model_discovery.claude_code import (
    CLAUDE_CODE_ALIAS_CANDIDATES,
    CLAUDE_CODE_PROBE_TIMEOUT_S,
    ClaudeCodeCLIUnavailableError,
    discover_claude_code,
)
from clio_agent.providers.model_discovery.codex import discover_codex
from clio_agent.providers.model_discovery.http import discover_http
from clio_agent.providers.model_discovery.overlay import (
    CLAUDE_CODE_COST_DEFAULT_MODEL,
    CLAUDE_CODE_SOURCE,
    CODEX_SOURCE,
    HTTP_SOURCE,
    OverlayMalformedError,
    OverlayUnreadableError,
    ProviderDiscoveryResult,
    attach_context_limits,
    overlay_default_model,
    overlay_models_wire,
    overlay_path,
    read_overlay,
    record_refresh,
    resolve_cloud_api_key,
)
from clio_agent.providers.model_discovery.refresh import (
    REFRESH_PER_PROVIDER_DEADLINE_S,
    build_refresh_provider_models_tool,
    is_provider_configured,
    refresh_all,
    refresh_all_sync,
)

__all__ = [
    "CLAUDE_CODE_ALIAS_CANDIDATES",
    "CLAUDE_CODE_COST_DEFAULT_MODEL",
    "CLAUDE_CODE_PROBE_TIMEOUT_S",
    "CLAUDE_CODE_SOURCE",
    "CODEX_SOURCE",
    "HTTP_SOURCE",
    "REFRESH_PER_PROVIDER_DEADLINE_S",
    "ClaudeCodeCLIUnavailableError",
    "OverlayMalformedError",
    "OverlayUnreadableError",
    "ProviderDiscoveryResult",
    "attach_context_limits",
    "build_refresh_provider_models_tool",
    "discover_claude_code",
    "discover_codex",
    "discover_http",
    "is_provider_configured",
    "overlay_default_model",
    "overlay_models_wire",
    "overlay_path",
    "read_overlay",
    "record_refresh",
    "refresh_all",
    "refresh_all_sync",
    "resolve_cloud_api_key",
]
