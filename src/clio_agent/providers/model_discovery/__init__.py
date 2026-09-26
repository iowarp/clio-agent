"""Provider model-catalog discovery + the refresh overlay (iowarp/clio-agent#1211).

The static per-provider model lists in :mod:`clio_agent.providers.catalog` are a
compiled-in snapshot; CLI-routed accounts (codex, claude_code) rotate their served
model ids independently of a clio release, so a snapshot goes stale (iowarp/clio-
agent#1184: the catalog offered ``gpt-5.5``/``gpt-5.5-codex``/``gpt-5.1`` after the
Codex channel had moved on to ``gpt-5.6-sol``). This package is the single owner
of, one submodule per concern (kept split to respect the #775 file-size ratchet):

* :mod:`.overlay` — the refresh overlay: read/write/delta, malformed-vs-unreadable
  typed errors, the ``ProviderDiscoveryResult`` shape, and the context/output-limit
  enrichment persisted at refresh time (#1211 review D4).
* :mod:`.codex_catalog` / :mod:`.codex` — the maintained Codex model
  catalog document plus a credential-store sign-in check (see
  :func:`discover_codex`) -- the direct provider has no account model-
  enumeration RPC the way the deleted ``openai_codex`` SDK offered.
* :mod:`.claude_code_catalog` — the maintained GitHub catalog document
  (:data:`~clio_agent.providers.model_discovery.claude_code_catalog.CLAUDE_CODE_CATALOG_URL`):
  the single source of Claude Code model ids, per-model input-modality
  capabilities, and the account default. Never a bundled fallback.
* :mod:`.claude_code` — claude_code discovery: trusts the maintained catalog
  for everything model-shaped, and separately runs exactly one ``auth status``
  CLI call to learn whether Claude Code is installed and signed in on this
  machine (no per-model probing, no bare-default probe, no multimodal probe).
* :mod:`.last_good` — the last good LIVE model list of an HTTP-backed provider,
  persisted in the same overlay and served (typed-stale) when a later passive
  probe comes back empty.
* :mod:`.http` — HTTP-backed providers reuse the existing live handshake `/models`
  path.
* :mod:`.refresh` — the concurrent, deadline-bounded, configured-providers-only
  refresh action (#1211 review R2/R3) and the ``refresh_provider_models`` agent
  tool (#1211 review R6, expert-pool-primary doctrine).

At service startup and on explicit checks, Codex refreshes through its SDK and
Claude Code refreshes through the maintained GitHub catalog plus one CLI
sign-in check. ``GET /v1/providers/{id}/models``
(:mod:`clio_agent.gact.routes.providers`) reads that verified state without
launching new checks. HTTP-backed providers keep their live handshake path
(#1211 review D5).
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
    CLAUDE_CODE_AUTH_STATUS_TIMEOUT_S,
    ClaudeCodeCLIUnavailableError,
    discover_claude_code,
)
from clio_agent.providers.model_discovery.codex import discover_codex
from clio_agent.providers.model_discovery.http import discover_http
from clio_agent.providers.model_discovery.last_good import (
    LAST_GOOD_CATALOG_SOURCE,
    LAST_GOOD_REASONS,
    LastGoodCatalog,
    last_good_catalog,
    last_good_staleness,
    persist_live_catalog,
)
from clio_agent.providers.model_discovery.modality_evidence import (
    MODALITY_EVIDENCE_REASONS,
    MODALITY_SOURCES,
    UnknownModalityReasonError,
    modality_evidence,
    reported_modalities,
)
from clio_agent.providers.model_discovery.overlay import (
    CLAUDE_CODE_SOURCE,
    CODEX_SOURCE,
    HTTP_SOURCE,
    OVERLAY_STALENESS_REASONS,
    OverlayMalformedError,
    OverlayUnreadableError,
    ProviderDiscoveryResult,
    attach_context_limits,
    entry_staleness,
    overlay_default_model,
    overlay_models_wire,
    overlay_path,
    overlay_staleness_ttl_s,
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
    "CODEX_SOURCE",
    "CLAUDE_CODE_AUTH_STATUS_TIMEOUT_S",
    "CLAUDE_CODE_SOURCE",
    "HTTP_SOURCE",
    "LAST_GOOD_CATALOG_SOURCE",
    "LAST_GOOD_REASONS",
    "LastGoodCatalog",
    "MODALITY_EVIDENCE_REASONS",
    "MODALITY_SOURCES",
    "OVERLAY_STALENESS_REASONS",
    "REFRESH_PER_PROVIDER_DEADLINE_S",
    "ClaudeCodeCLIUnavailableError",
    "OverlayMalformedError",
    "OverlayUnreadableError",
    "ProviderDiscoveryResult",
    "UnknownModalityReasonError",
    "attach_context_limits",
    "build_refresh_provider_models_tool",
    "discover_codex",
    "discover_claude_code",
    "discover_http",
    "entry_staleness",
    "is_provider_configured",
    "last_good_catalog",
    "last_good_staleness",
    "modality_evidence",
    "overlay_default_model",
    "overlay_models_wire",
    "overlay_path",
    "overlay_staleness_ttl_s",
    "persist_live_catalog",
    "read_overlay",
    "record_refresh",
    "refresh_all",
    "refresh_all_sync",
    "reported_modalities",
    "resolve_cloud_api_key",
]
