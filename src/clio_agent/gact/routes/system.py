"""System + observability routes for the GACT server (#714).

The "system" concern is the read-only operational surface the TUI polls to render
its connection/doctor/metrics affordances -- it never mutates session state:

* ``GET /v1/health`` -- per-subsystem status (api/sessions/agent/memory/lm) rolled
  up to a worst-case ``overall_status``; returns 503 when unavailable.
* ``GET /v1/capabilities`` -- the contract version + capability/transport/auth
  flags so clients can disable UI for surfaces this build does not provide.
* ``GET /v1/capability-gaps`` -- intentionally-unsupported / future rows, kept
  visible so "not supported yet" affordances do not have to be inferred.
* ``GET /v1/metrics`` -- aggregate runtime counters (sessions/messages/tokens/
  cost) plus real recorded tool-call latency percentiles (SPEC §6.16).
* ``GET /v1/memory/stats`` -- ARC cache counters + per-session retained-context
  pressure + global ARC totals (zeros are a valid signal when ARC is unwired).

The static catalogs these project (stream-fallback reasons, capability gaps, the
latency-stat helper) and the wire/limit constants live in the leaves
:mod:`clio_agent.gact.runtime.capabilities` / :mod:`clio_agent.gact.runtime.constants`
(single source, shared with the message-turn path in :mod:`clio_agent.gact.app`).
The per-session retention-estimate helpers are concern-private and live here. This
module imports only leaf packages (runtime, providers.config, types, stdlib) and
never loads :mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from clio_agent.errors import MCP_TASK_RECORD_STORE_ABSENT
from clio_agent.gact.composer_runtime import resource_capabilities
from clio_agent.gact.context_references import CONTEXT_REFERENCE_CAPABILITY
from clio_agent.gact.protocol_v3 import capabilities_to_v3, project_for_request
from clio_agent.gact.provenance.child_projection import CHILD_ACTIVITY_PROJECTION_CAPABILITY
from clio_agent.gact.relay_status import relay_capabilities
from clio_agent.gact.runtime.capabilities import (
    _capability_gap_metadata,
    _latency_stat,
    _stream_fallback_reason_capabilities,
)
from clio_agent.gact.runtime.constants import (
    _CTX_MAX_BYTES,
    CONTRACT_VERSION,
    GACT_BACKEND_VERSION,
)
from clio_agent.gact.runtime.context_tokens import _resolve_expert_context_window
from clio_agent.gact.types import (
    AuthInfo,
    BackendInfo,
    CacheStats,
    Capabilities,
    CapabilityFlags,
    GlobalMemoryStats,
    HealthResponse,
    Integration,
    MemoryStats,
    Message,
    Metrics,
    MetricsMessages,
    MetricsSessions,
    SessionMemoryStats,
    TransportFlags,
)
from clio_agent.runtime.status import (
    IntegrationState,
    IntegrationStatus,
    RuntimeReport,
    collect_runtime_status,
)
from clio_agent.tools.mcp_task_records import task_record_store_is_durable

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps

logger = logging.getLogger("clio_agent.gact.routes.system")


# The single doctor speaks five probe states; the v0.2 health wire only has
# three. Map the two extra states to the closest wire chip: SKIPPED (a
# deliberately-unprobed / not-required row) is not a problem -> ready; a
# MISCONFIGURED row needs attention but the server is up -> degraded. UNAVAILABLE
# is the only hard-down that trips the 503 contract (handled in the handler).
_PROBE_STATE_TO_WIRE: dict[str, Literal["ready", "degraded", "unavailable"]] = {
    IntegrationState.READY.value: "ready",
    IntegrationState.SKIPPED.value: "ready",
    IntegrationState.DEGRADED.value: "degraded",
    IntegrationState.MISCONFIGURED.value: "degraded",
    IntegrationState.UNAVAILABLE.value: "unavailable",
}


# --------------------------------------------------------------------------- #
# Orphan-scan fold (polled-endpoint latency).                                 #
#                                                                             #
# The ``child_parentage`` row is a full-box orphan scan: a psutil enumeration #
# of EVERY process on the host (~9s COLD on Windows, sub-50ms warm) to flag a #
# CLIO process orphaned from both roots (#900 PART B). /v1/health is POLLED    #
# (the TUI doctor modal hits it on a timer), so it MUST NOT pay that cold walk #
# inline. We serve that one row the same way this handler already serves the   #
# LM handshake: from a cache, refreshed in the background (stale-while-        #
# revalidate), and — before any scan has run — a typed "collecting" placeholder#
# instead of a blocking wait. The cheap reaper + child_processes rows stay     #
# synchronous; the CLI doctor still collects the orphan scan fresh             #
# (collect_runtime_status defaults include_process_census=True).              #
# --------------------------------------------------------------------------- #

_ORPHAN_SCAN_TTL_S = 15.0


def _orphan_scan_placeholder() -> IntegrationStatus:
    """A non-required 'collecting' row returned while the orphan-scan cache is cold
    — honest not-yet-run (mirrors the omitted-handshake pattern), never a block."""

    return IntegrationStatus(
        name="child_parentage",
        state=IntegrationState.READY,
        summary="Orphan scan collecting in the background (first poll after boot).",
        config_source="runtime:process_census",
        next_action="No action required.",
    )


def _kick_orphan_scan_refresh(app: "FastAPI") -> None:
    """Start ONE background daemon thread that fills the orphan-scan cache on
    ``app.state`` from :func:`live_orphan_scan_rows`. Guarded so concurrent polls
    never spawn more than one refresher. The full-box psutil walk runs off the
    event loop; a failure is recorded as a typed degraded row, never silent."""

    import threading  # noqa: PLC0415 - only the health poller needs it

    lock = getattr(app.state, "_orphan_scan_lock", None)
    if lock is None:
        lock = threading.Lock()
        app.state._orphan_scan_lock = lock
    with lock:
        if getattr(app.state, "orphan_scan_refreshing", False):
            return
        app.state.orphan_scan_refreshing = True

    def _fill() -> None:
        # The flag MUST be cleared no matter what (a stuck True permanently freezes
        # the cache and silently kills orphan detection — the guard above would
        # early-return forever). ``finally`` guarantees it even on an unexpected
        # error inside the fill.
        try:
            from clio_agent.runtime.process_tree import (  # noqa: PLC0415
                live_orphan_scan_rows,
            )

            try:
                rows = live_orphan_scan_rows()
            except Exception as exc:  # noqa: BLE001 - surfaced as a degraded row, not silent
                rows = [
                    IntegrationStatus(
                        name="child_parentage",
                        state=IntegrationState.DEGRADED,
                        summary=f"orphan scan collection failed: {exc!r}",
                        config_source="runtime:process_census",
                        next_action=(
                            "Inspect the gact server logs for the orphan-scan probe failure."
                        ),
                    )
                ]
            app.state.orphan_scan_rows = rows
            app.state.orphan_scan_at = time.time()
        finally:
            app.state.orphan_scan_refreshing = False

    try:
        threading.Thread(target=_fill, name="clio-orphan-scan", daemon=True).start()
    except RuntimeError:
        # Thread-start can fail under handle/thread exhaustion. Clear the flag so a
        # later poll retries (never wedge the guard True), and let the cache serve
        # its existing/placeholder rows.
        app.state.orphan_scan_refreshing = False


def _folded_orphan_scan_rows(app: "FastAPI") -> list[IntegrationStatus]:
    """Cached orphan-scan row(s) for the polled /v1/health, stale-while-revalidate.

    Returns the last cached rows immediately (kicking a background refresh when
    they are older than the TTL); before any scan has run, returns a single typed
    'collecting' placeholder and kicks the first fill. Never blocks the request on
    the cold psutil walk.
    """

    cached = getattr(app.state, "orphan_scan_rows", None)
    collected_at = getattr(app.state, "orphan_scan_at", 0.0)
    if cached is None:
        _kick_orphan_scan_refresh(app)
        return [_orphan_scan_placeholder()]
    if time.time() - collected_at > _ORPHAN_SCAN_TTL_S:
        _kick_orphan_scan_refresh(app)  # refresh in background; serve stale now
    return list(cached)


def _integration_to_wire(item: IntegrationStatus) -> Integration:
    """Project one probe :class:`IntegrationStatus` to the health wire row.

    Preserves the v0.2 ``name``/``status``/``detail`` triple (``detail`` mirrors
    the human ``summary`` for back-compat) and carries the richer probe fields so
    no doctor detail is lost on the gact surface.
    """

    return Integration(
        name=item.name,
        status=_PROBE_STATE_TO_WIRE.get(item.state.value, "degraded"),
        detail=item.summary,
        summary=item.summary,
        config_source=item.config_source or None,
        next_action=item.next_action or None,
        endpoint=item.endpoint,
    )


def _health_overall(report: RuntimeReport) -> Literal["ready", "degraded", "unavailable"]:
    """Collapse a runtime report to the v0.2 health wire status.

    Uses ``RuntimeReport.overall_status`` (the engine's ready/degraded/skipped
    required-row rollup) for the ready-vs-degraded decision instead of
    re-deriving it, then layers the finer wire distinction the engine's rollup
    deliberately does not encode: a *required* integration that is actually
    UNAVAILABLE is a hard-down and maps to ``unavailable`` so the gact ``/v1/health``
    503 contract still holds through the unified doctor.
    """

    hard_down = any(
        item.required and item.state is IntegrationState.UNAVAILABLE for item in report.integrations
    )
    if hard_down:
        return "unavailable"
    if report.overall_status == IntegrationState.DEGRADED.value:
        return "degraded"
    return "ready"


#: Severity rank for reconciling two views of the same integration. Higher == worse; the
#: three wire buckets collapse ready/skipped -> 0, degraded/misconfigured -> 1, unavailable -> 2.
_STATE_SEVERITY: dict[IntegrationState, int] = {
    IntegrationState.READY: 0,
    IntegrationState.SKIPPED: 0,
    IntegrationState.DEGRADED: 1,
    IntegrationState.MISCONFIGURED: 1,
    IntegrationState.UNAVAILABLE: 2,
}


def _fold_handshake_row(live: IntegrationStatus, enriched: IntegrationStatus) -> IntegrationStatus:
    """Reconcile the LIVE lm_provider probe with the CACHED handshake enrichment.

    The handshake row carries richer LM detail (capabilities / models / context window /
    config source) but it is a CACHE from an earlier successful bind — it must never mask a
    provider the live probe now finds down, or a stale ``ready`` handshake would flip a
    would-be 503 back to 200 and defeat the endpoint's unavailable contract. So when the live
    probe is STRICTLY worse than the handshake, the live row wins outright (its state and
    failure summary are the truth right now); otherwise the handshake's richer row is shown —
    its state is already at least as severe as the live one, so the 503 contract is preserved
    either way.
    """

    if _STATE_SEVERITY.get(live.state, 1) > _STATE_SEVERITY.get(enriched.state, 1):
        return live
    return enriched


def _estimate_message_context_tokens(message: Message) -> int:
    """Estimate the retained-context tokens one message contributes.

    Prefers the message's recorded token counters; falls back to a chars/4
    approximation over its text/thinking/path/diff payloads.
    """

    explicit = (
        int(getattr(message.tokens, "input", 0) or 0)
        + int(getattr(message.tokens, "output", 0) or 0)
        + int(getattr(message.tokens, "cache_read", 0) or 0)
        + int(getattr(message.tokens, "cache_write", 0) or 0)
    )
    if explicit > 0:
        return explicit
    chars = 0
    for part in message.parts:
        chars += len(part.text or "")
        chars += len(str(getattr(part, "thinking", "") or ""))
        chars += len(part.path or "")
        chars += len(part.unified_diff or "")
        chars += len(part.new_content or "")
    return max(1, chars // 4) if chars else 0


def _estimate_context_file_tokens(row: Mapping[str, Any]) -> int:
    """Estimate the retained tokens an attached context file contributes.

    Mirrors injection's inline cap (``_CTX_MAX_BYTES``) before the chars/4 estimate
    so the reported pressure matches what is actually inlined into a turn.
    """

    size = row.get("size")
    try:
        raw_size = int(size or 0)
    except (TypeError, ValueError):
        raw_size = 0
    # Context-file injection caps inlined bodies at _CTX_MAX_BYTES.
    retained_bytes = min(max(raw_size, 0), _CTX_MAX_BYTES)
    return retained_bytes // 4


def _context_pressure_state(
    tokens_retained: int,
    tokens_budget: int,
) -> tuple[float, Literal["empty", "normal", "warning", "critical"], bool]:
    """Map retained/budget tokens to (pressure, threshold_state, compact?)."""

    if tokens_budget <= 0:
        return 0.0, "empty" if tokens_retained == 0 else "normal", False
    pressure = min(1.0, tokens_retained / tokens_budget)
    if tokens_retained <= 0:
        return 0.0, "empty", False
    if pressure >= 0.9:
        return pressure, "critical", True
    if pressure >= 0.75:
        return pressure, "warning", True
    return pressure, "normal", False


def register_system_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the read-only system/observability routes on ``app``.

    Handlers close over the ``app`` argument (FastAPI's decorators need it) and
    read ``app.state`` directly; this concern needs no cross-concern seam from
    ``deps`` (it is accepted to match the uniform
    ``register_<concern>_routes(app, deps)`` factory signature). The static
    capability/metrics catalogs come from the runtime leaves; the per-session
    retention-estimate helpers are module-private to this concern.
    """

    @app.get("/v1/health", response_model=HealthResponse)
    async def health() -> HealthResponse | JSONResponse:
        """SPEC §3.4 — per-subsystem status feeds the TUI's /doctor modal.

        #800 collapsed the three divergent doctors into ONE: this endpoint now
        delegates to the same :func:`collect_runtime_status` probe engine the CLI
        renders, so the TUI/CLI read a single honest doctor. The rows are the rich
        real-deployment probes (lm_provider, arc/clio-core, gateway, data backends,
        file_policy, api, clio_core) instead of the old hand-rolled five.

        ``api_state=READY`` reports the API in-process — the probe must never
        re-HTTP this very endpoint. The collection runs in
        a worker thread so the gateway/backend probes never block the event loop,
        and never triggers an LM handshake (a polled endpoint must not block on
        OAuth/model discovery); the LM handshake is folded in from cache only.

        overall_status uses the engine's required-row rollup, mapped to the wire's
        ready/degraded/unavailable so the 503-on-``unavailable`` contract holds.
        """

        uptime = int(time.time() - app.state.started_at)

        try:
            report = await asyncio.to_thread(
                collect_runtime_status,
                api_state=IntegrationState.READY,
                lm_timeout=0.5,
                # The full-box process census is served from a background cache
                # below — a polled endpoint must not pay the ~10s cold psutil walk.
                include_process_census=False,
            )
            integrations = list(report.integrations)
        except Exception as exc:  # noqa: BLE001 - surfaced as a degraded doctor row (see comment)
            # No silent fallback (cleanup ground rule): a probe engine failure is
            # surfaced as a structured degraded doctor row, not a bare 200.
            fallback = IntegrationStatus(
                name="doctor",
                state=IntegrationState.DEGRADED,
                summary=f"runtime status collection failed: {exc!r}",
                config_source="in-process:collect_runtime_status",
                next_action="Inspect the gact server logs for the doctor probe failure.",
            )
            report = RuntimeReport(integrations=[fallback])
            integrations = list(report.integrations)

        # Fold the CACHED LM handshake into the lm_provider row. NEVER run a
        # handshake here — that would block a polled endpoint on OAuth/model
        # discovery (mirrors the "never block a bind on a handshake" rule in
        # routes/providers.py). Absent a cached report the enrichment is simply
        # omitted (not-yet-run, not a silent failure).
        handshake = getattr(app.state, "lm_handshake_report", None)
        if handshake is not None:
            try:
                enriched = handshake.to_integration_status()
            except Exception as exc:  # noqa: BLE001 - enrichment failure surfaced to logs/trace (see comment)
                # No silent fallback: the enrichment is additive, but a failure to build
                # it must reach the logs/trace rather than vanish (mirrors the sibling
                # doctor-probe failure branch above).
                logger.warning("lm handshake enrichment failed: %r", exc)
                enriched = None
            if enriched is not None:
                integrations = [
                    _fold_handshake_row(item, enriched) if item.name == "lm_provider" else item
                    for item in integrations
                ]
                report = RuntimeReport(integrations=integrations)

        # Fold the background-refreshed orphan scan (child_parentage) that the
        # synchronous collect above deliberately skipped, so the polled endpoint
        # stays fast even on a cold psutil box (the cheap reaper + child_processes
        # rows were already collected inline).
        integrations = integrations + _folded_orphan_scan_rows(app)
        report = RuntimeReport(integrations=integrations)

        overall = _health_overall(report)
        rows = [_integration_to_wire(item) for item in integrations]

        response = HealthResponse(
            healthy=overall != "unavailable",
            uptime_s=uptime,
            overall_status=overall,
            integrations=rows,
            # #772: surface the tool-runtime hooks flag so a failed permission-gate
            # install (ungated/unobserved tools) is visible, not silent.
            tool_hooks_installed=getattr(app.state, "tool_hooks_installed", None),
        )
        if overall == "unavailable":
            content = response.model_dump(mode="json", exclude_none=True)
            # The hooks flag is tri-state (True / False / None = "agent not
            # constructed yet") — the deferred-boot window reports exactly
            # None over this 503 path, so exclude_none must not drop it (#772).
            content["tool_hooks_installed"] = response.tool_hooks_installed
            return JSONResponse(status_code=503, content=content)
        return response

    @app.get("/v1/capabilities", response_model=Capabilities)
    async def capabilities(request: Request) -> Capabilities | JSONResponse:
        bearer_enabled = bool(getattr(app.state, "bearer_token", None))
        task_store_durable = task_record_store_is_durable()
        response = Capabilities(
            contract_version=CONTRACT_VERSION,
            backend=BackendInfo(
                name="clio-agent-gact",
                version=GACT_BACKEND_VERSION,
                vendor="iowarp",
                homepage="https://github.com/iowarp/clio-agent",
            ),
            capabilities=CapabilityFlags(
                # v0.1 baseline — flipped on as each surface lands.
                # Honest reporting lets the TUI disable UI for
                # capabilities we don't actually provide.
                sessions=True,  # BBB8 — /v1/sessions CRUD
                workspaces=True,  # CLIO-WS — /v1/workspaces CRUD
                metrics=True,  # BBB15 — /v1/metrics returns SPEC §6.16 envelope
                session_branching=True,  # BBB26 — POST /sessions/{sid}/fork
                search_messages=True,  # BBB27 — GET /sessions/{sid}/messages/search
                cost_tracking=True,  # BBB24 — Message.tokens + Session.cost_usd rollup
                files=True,  # BBB22 — /v1/sessions/{sid}/context/files CRUD
                diffs=True,  # BBB21 — file_diff parts + /diffs/apply,reject
                permissions=True,  # BBB23 — /v1/permissions + permission.* events
                # BBB25 — child agents. #948 S4 retired the nanoagent subsessions +
                # subagent.* events; the capability is now provided by the spawn
                # substrate: children run as real agent-task sessions
                # (session_type=agent_task) surfaced via blueprint.delegation.* events.
                subagents=True,
                session_export=True,  # #16 — /v1/sessions/{sid}/export + import
                # #760: no /summarize or /attachments routes exist — advertising
                # them made the TUI 404 (paired gact-tui issue #224). Flip back
                # to True only when the routes land.
                session_summary=False,
                attachments_upload=False,
                multimodal_image_parts=True,  # #528 — preserve image parts + provider gate
                mcp=True,  # #13 — /v1/mcp/servers exposes the gateway namespaces
                providers=True,  # #15 — /v1/providers catalogs the LM presets
                commands=True,  # #14 — /v1/commands + dispatch
                thinking_blocks=True,  # #17 — DSPy reasoning trace as thinking Parts
                session_tasks=True,  # #18 — per-session todo CRUD
                plan_mode=True,  # session.mode=plan blocks destructive tools
                edit_modes=True,  # session.edit_mode toggles diff/whole/patch
                agent_write=True,  # #19 — POST/PUT/DELETE /v1/agents
                hooks=True,  # #20 — pre/post_tool + pre/post_message hooks
                scheduled_sessions=True,  # #21 — cron schedules
                session_sharing=True,  # #22 — share tokens
                skills_extraction=True,  # #23 — POST /v1/agents/extract
                # v0.2 additions — advertised when the scaffold
                # actually emits them. Turned on piecewise as the
                # follow-on items land.
                agent_routing=True,  # BBB10 — /v1/agents?tier= + tier-2 catalog
                memory=True,  # BBB11 — /v1/memory/stats backed by ARC
                structured_errors=True,  # always — we return the envelope for every error
                integration_health=True,  # /v1/health above carries it
                tool_telemetry=True,  # BBB18 — tool.call.started/completed events
                x_clio_cancellation="best_effort",
                x_clio_executor_cancellation=True,  # #1116 — MCP request task -> wire cancel
                x_clio_text_streaming="best_effort_live",
                x_clio_synthetic_posthoc_streaming=False,
                x_clio_stream_fallback_reasons=_stream_fallback_reason_capabilities(),
                x_clio_direct_delete_permissions=True,
                x_clio_prompt_registry=True,
                x_clio_expert_packs=True,
                x_clio_agent_blueprints=True,
                x_clio_user_questions=True,
                x_clio_interactions=True,
                x_clio_retry_attempts=True,
                x_clio_context_frames=True,
                x_clio_semantic_events=True,
                x_clio_context_references=CONTEXT_REFERENCE_CAPABILITY,
                x_clio_artifacts=True,  # #968 — /v1/artifacts + artifact.* + resource_link
                x_clio_child_activity_projection=CHILD_ACTIVITY_PROJECTION_CAPABILITY,
                x_clio_document_artifacts={
                    "protocol_version": "0.1.0",
                    "profiles": [
                        "markdown",
                        "pdf",
                        "latex",
                        "html-static",
                        "ooxml-word",
                        "ooxml-sheet",
                        "ooxml-slides",
                        "odf-text",
                        "odf-sheet",
                        "odf-slides",
                    ],
                    "anchors": [
                        "text-quote",
                        "pdf-quad",
                        "dom",
                        "sheet-range",
                        "slide-shape",
                        "native-comment",
                        "source-map",
                    ],
                    "review_parts": True,
                    "floating_comments": True,
                    "immutable_revisions": True,
                    "native_working_copies": True,
                    "native_comment_trigger": "@clio",
                    "embedded_editors": ["onlyoffice", "collabora"],
                    "static_html_scripts": "blocked",
                    "executable_html_transition": "live-web",
                },
                x_clio_semantic_trace_backend=getattr(
                    app.state.semantic_trace_backend,
                    "name",
                    "",
                ),
                x_clio_semantic_trace_detail=app.state.semantic_trace_detail_level,
                x_clio_hook_backend=str(
                    (getattr(app.state, "runtime_hook_registry_metadata", {}) or {}).get(
                        "backend", ""
                    )
                ),
                x_clio_hook_events=dict(
                    (getattr(app.state, "runtime_hook_registry_metadata", {}) or {}).get(
                        "handler_counts", {}
                    )
                ),
                x_clio_capability_gaps=_capability_gap_metadata(),
                x_clio_task_record_store={
                    "durable": task_store_durable,
                    "reason": None if task_store_durable else MCP_TASK_RECORD_STORE_ABSENT,
                },
                # Composer lanes. Unconditionally true: composer_runtime
                # registers all three surfaces at build_app, so a running
                # server always serves them. The resource block carries the
                # limits a client must respect before it starts an upload.
                x_clio_message_delivery=True,
                x_clio_pending_steers=True,
                x_clio_queued_messages=True,
                x_clio_resources=resource_capabilities(app),
            ),
            transports=TransportFlags(events_sse=True, events_websocket=False),
            auth=AuthInfo(
                schemes=["trust_socket", "bearer"] if bearer_enabled else ["trust_socket"],
                current="bearer" if bearer_enabled else "trust_socket",
            ),
            relay=relay_capabilities(
                getattr(app.state, "relay_tool_status", None)
                or getattr(app.state, "relay_runtime_status", None)
            ),
        )
        return project_for_request(
            request,
            v3=lambda: JSONResponse(
                content=capabilities_to_v3(
                    app,
                    response.capabilities,
                    replay_retention=app.state.bus.history_capacity,
                )
            ),
            v2=lambda: response,
        )

    @app.get("/v1/hooks")
    async def hooks_inspect() -> dict[str, Any]:
        """Read-only inspection of every LOADED hook (P2.7 #1075).

        The debugging entry point (the ``/hooks``-command analog) that REPLACES the
        CRUD deleted in P2.1: it never mutates hook state. Lists each loaded hook with
        its stable ``id``, the events it runs ``on``, its ``match`` predicate, its
        source scope label (user/project/managed), its content ``trust`` state, and
        whether it is ``enabled`` — plus the bounded recent per-invocation audit
        records (the same ``hook.invoked`` records carried on the semantic highway) so
        an operator can see what fired and how it decided without reading the trace.
        """

        from clio_agent.gact.hooks import (  # noqa: PLC0415 - lazy: keep system.py leaf-only
            get_global_dispatcher,
            recent_hook_invocations,
        )

        dispatcher = get_global_dispatcher()
        # Prefer the LIVE dispatcher (a test may install one after build_app); fall back
        # to the boot-time metadata stamped on app.state for /v1/capabilities parity.
        if dispatcher is not None:
            metadata = dispatcher.metadata()
            hooks = dispatcher.inspect()
        else:
            metadata = getattr(app.state, "runtime_hook_registry_metadata", {}) or {}
            hooks = []
        return {
            "backend": str(metadata.get("backend", "")),
            "enabled": bool(metadata.get("enabled", False)),
            "hooks": hooks,
            "recent_invocations": recent_hook_invocations(),
        }

    @app.get("/v1/capability-gaps")
    async def capability_gaps() -> dict[str, Any]:
        """Return intentionally unsupported or future CLIO capability rows.

        This keeps "not supported yet" affordances visible as ideas without
        making clients infer support from missing routes or failed commands.
        """

        return {"capability_gaps": _capability_gap_metadata()}

    @app.get("/v1/metrics", response_model=Metrics)
    async def metrics() -> Metrics:
        """Aggregate runtime metrics — SPEC §6.16.

        Today: counters synthesised from the session + in-memory
        message logs. ARC-backed per-expert latency/success-rate
        rollups come in when we reshape `ARCMemory.get_metrics()`
        into this envelope (tracked in the v0.3 roadmap); for now
        the endpoint returns the wire-compatible skeleton with zero
        tokens/cost/latencies so the TUI's Metrics tab renders
        rather than falling back to a permanent "n/a".
        """

        uptime = max(0, int(time.time() - app.state.started_at))

        all_sessions = app.state.sessions.list()
        by_status: dict[str, int] = {}
        active = 0
        for s in all_sessions:
            by_status[s.status] = by_status.get(s.status, 0) + 1
            if s.status in {"running", "idle"}:
                active += 1

        # #770 C3: the message-derived rollups (total, by-role, and the
        # iowarp/clio-agent#655 per-tool + overall "tool_call" latency buckets)
        # are maintained incrementally at the session_store write seam
        # (app.state.metrics_counters), so this handler reads a running counter
        # instead of re-walking every message of every session on each poll. The
        # reported values are byte-identical to the old full walk: _latency_stat
        # sorts its samples, so accumulation order does not matter.
        counters = app.state.metrics_counters
        message_total = counters.message_total
        role_counts = counters.role_counts()
        latencies = {key: _latency_stat(vals) for key, vals in counters.latency_samples.items()}

        # tokens + cost rollup across every
        # session's cumulative counters.
        from clio_agent.gact.types import MetricsCost, MetricsTokens  # noqa: PLC0415

        tokens_input = sum(s.tokens_input for s in all_sessions)
        tokens_output = sum(s.tokens_output for s in all_sessions)
        cost_total = sum(s.cost_usd for s in all_sessions)

        return Metrics(
            uptime_s=uptime,
            sessions=MetricsSessions(
                total=len(all_sessions),
                active=active,
                by_status=by_status,
            ),
            messages=MetricsMessages(
                total=message_total,
                by_role=role_counts,
            ),
            tokens=MetricsTokens(
                input_total=tokens_input,
                output_total=tokens_output,
            ),
            cost=MetricsCost(total_usd=cost_total),
            latencies=latencies,
        )

    # ---- /v1/memory/stats (BBB11) ------------------------------------
    # Returns cache counters + per-session context retention + global
    # ARC totals. When ARC isn't wired (tests, smoke-boot scenarios)
    # returns zeros per SPEC §6.19 ("zeros are a valid signal").

    @app.get(
        "/v1/memory/stats",
        response_model=MemoryStats,
        response_model_by_alias=True,
    )
    async def memory_stats(session_id: Optional[str] = None) -> MemoryStats:
        if app.state.arc is not None:
            raw = app.state.arc.get_cache_stats()
            cache = CacheStats(
                hits=int(raw.get("hits", 0)),
                misses=int(raw.get("misses", 0)),
                hit_rate=float(raw.get("hit_rate", 0.0)),
                capacity=int(raw.get("capacity", 0)),
            )
            # ARC tracks conversation + invocation counts via the
            # index sizes it reports alongside the cache. Future: if
            # the numbers start diverging from what operators expect
            # we can call dedicated getters; for now the index sizes
            # are a good-faith approximation.
            global_stats = GlobalMemoryStats(
                conversations_total=int(raw.get("conv_index_size", 0)),
                invocations_total=int(raw.get("inv_index_size", 0)),
            )
        else:
            cache = CacheStats()
            global_stats = GlobalMemoryStats()

        session_block: Optional[SessionMemoryStats] = None
        metadata: dict[str, Any] = {
            "retained_context_source": "visible_gact_transcript",
            "token_estimate": "message_tokens_or_chars_div_4",
        }
        if session_id:
            sess_rec = app.state.sessions.get(session_id)
            if sess_rec is not None:
                messages = list(app.state.messages.get(session_id, []))
                context_files = list((app.state.context_files.get(session_id, {}) or {}).values())
                context_files_by_mode: dict[str, int] = {"edit": 0, "pin": 0, "read": 0}
                for row in context_files:
                    mode = str(row.get("mode") or "read")
                    context_files_by_mode[mode] = context_files_by_mode.get(mode, 0) + 1
                transcript_tokens = sum(_estimate_message_context_tokens(m) for m in messages)
                context_file_tokens = sum(
                    _estimate_context_file_tokens(row) for row in context_files
                )
                tokens_retained = transcript_tokens + context_file_tokens
                cfg = getattr(app.state.agent, "_provider_config", None)
                tokens_budget = _resolve_expert_context_window(cfg)
                metadata["tokens_budget_source"] = (
                    "handshake_window" if tokens_budget > 0 else "unknown"
                )
                pressure, threshold_state, compact_recommended = _context_pressure_state(
                    tokens_retained,
                    tokens_budget,
                )
                compact_summaries = sum(
                    1
                    for m in messages
                    if m.metadata.get("synthetic") == "compact_summary"
                    or any(p.metadata.get("synthetic") == "compact_summary" for p in m.parts)
                )
                session_block = SessionMemoryStats(
                    session_id=session_id,
                    messages_retained=len(messages),
                    tokens_retained=tokens_retained,
                    tokens_budget=tokens_budget,
                    profiles_attached=0,
                    context_files_attached=len(context_files),
                    context_files_by_mode=context_files_by_mode,
                    compact_summaries=compact_summaries,
                    token_pressure=pressure,
                    threshold_state=threshold_state,
                    compaction_recommended=compact_recommended,
                )
                metadata["session"] = {
                    "transcript_tokens": transcript_tokens,
                    "context_file_tokens": context_file_tokens,
                    "recorded_lifetime_tokens": sess_rec.tokens_input + sess_rec.tokens_output,
                }
            else:
                # Unknown session: return an empty block rather than
                # a 404. The TUI's footer chip handles zero stats
                # gracefully; a 404 would spam the logs on every
                # mis-timed fetch.
                session_block = SessionMemoryStats(session_id=session_id)

        return MemoryStats(
            cache=cache,
            session=session_block,
            global_=global_stats,  # type: ignore[call-arg]  # Pydantic alias "global"
            metadata=metadata,
        )
