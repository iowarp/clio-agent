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
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse

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

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


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
        real-deployment probes (lm_provider, arc/CTE, gateway, data backends,
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
            )
            integrations = list(report.integrations)
        except Exception as exc:
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
            except Exception:
                enriched = None
            if enriched is not None:
                integrations = [
                    _fold_handshake_row(item, enriched) if item.name == "lm_provider" else item
                    for item in integrations
                ]
                report = RuntimeReport(integrations=integrations)

        overall = _health_overall(report)
        rows = [_integration_to_wire(item) for item in integrations]

        response = HealthResponse(
            healthy=overall != "unavailable",
            uptime_s=uptime,
            overall_status=overall,
            integrations=rows,
        )
        if overall == "unavailable":
            return JSONResponse(
                status_code=503,
                content=response.model_dump(mode="json", exclude_none=True),
            )
        return response

    @app.get("/v1/capabilities", response_model=Capabilities)
    async def capabilities() -> Capabilities:
        return Capabilities(
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
                subagents=True,  # BBB25 — nanoagent subsessions + subagent.* events
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
                x_clio_executor_cancellation=False,
                x_clio_text_streaming="best_effort_live",
                x_clio_synthetic_posthoc_streaming=False,
                x_clio_stream_fallback_reasons=_stream_fallback_reason_capabilities(),
                x_clio_direct_delete_permissions=True,
                x_clio_prompt_registry=True,
                x_clio_expert_packs=True,
                x_clio_agent_blueprints=True,
                x_clio_user_questions=True,
                x_clio_retry_attempts=True,
                x_clio_context_frames=True,
                x_clio_semantic_events=True,
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
            ),
            transports=TransportFlags(events_sse=True, events_websocket=False),
            auth=AuthInfo(schemes=["trust_socket"], current="trust_socket"),
        )

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

        # CLIO-BBBBBBBBBB24: tokens + cost rollup across every
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
                tokens_budget = 4000
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
