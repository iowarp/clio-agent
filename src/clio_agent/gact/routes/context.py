"""ARC live-context-plane routes for the GACT server (#714).

The "context" concern exposes CLIO's per-session, per-scope live working set --
the ARC segment store projected as a model-window budget plus the editable
segments behind it. The gact-tui context panel (and the agent-run paths that
inspect/compact context) read and mutate it through this surface:

* ``GET /v1/sessions/{sid}/context/policy`` -- CLIO's effective context
  compartment policy (cross-session/global read defaults) for a session.
* ``GET /v1/sessions/{sid}/context/state`` -- the live ARC context-plane state
  for a ``(session, scope)``: model-window % used (segment-attributed +
  model-grounded), per-category token breakdown, auto-compaction threshold and
  the current render.
* ``POST /v1/sessions/{sid}/context/ops`` -- apply ONE live-context operation
  (append/insert/delete/summarize); a validated passthrough to the sanctioned
  ``apply_segment_op`` seam (clio carries the op, the caller chooses it).
* ``POST /v1/sessions/{sid}/context/compact`` -- LLM-summarize a scope's live
  working set NOW into one summary segment (same summarizer the in-turn
  auto-compactor uses), then return the fresh state.
* ``GET /v1/sessions/{sid}/context/search`` -- semantic discovery over a
  session's scopes ("which scope knows about X").

Everything these handlers need is a module-level leaf import: the segment-token
arithmetic + window resolution live in :mod:`clio_agent.gact.runtime.context_tokens`,
the live-summary call in :mod:`clio_agent.gact.agents.runtime`, the compartment
metadata in :mod:`clio_agent.gact.workspace_scope`, and the session-not-found
envelope in :mod:`clio_agent.gact.app`'s leaf helper (re-exported here as a
module import, NOT a ``build_app`` closure). The ARC-unavailable ``503`` pattern
and the ``_build_context_state`` / ``_context_window_for_state`` helpers shared
by the state + compact routes are concern-private and live here. The module
imports only leaf packages and never loads :mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import msgspec
from fastapi import FastAPI, HTTPException

from clio_agent.gact.agents import runtime as agents_runtime
from clio_agent.gact.runtime.context_tokens import (
    _autocompact_threshold,
    _bucket_context_categories,
    _estimate_text_tokens,
    _last_prompt_tokens,
    _resolve_expert_context_window,
)
from clio_agent.gact.types import (
    ContextOpRequest,
    ContextOpResponse,
    ContextSearchHit,
    ContextSearchResponse,
    ContextStateResponse,
    ErrorEnvelope,
    ErrorInfo,
    SessionContextPolicy,
)
from clio_agent.gact.workspace_scope import workspace_scope

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def register_context_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the ARC live-context-plane routes on ``app``.

    Handlers close over the ``app`` argument (FastAPI's decorators need it) and
    reach the live ARC + sessions through ``app.state``; this concern needs no
    cross-concern seam from ``deps`` (it is accepted to match the uniform
    ``register_<concern>_routes(app, deps)`` factory signature). The
    ARC-unavailable ``503`` envelope and the state-assembly helpers shared by the
    state + compact routes are defined here as closures over ``app``.
    """

    def _session_not_found(sid: str) -> HTTPException:
        return HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="internal_error",
                    message=f"session not found: {sid}",
                    details={"session_id": sid},
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    def _arc_unavailable(sid: str) -> HTTPException:
        return HTTPException(
            status_code=503,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="arc_unavailable",
                    message="ARC memory is not enabled for this deployment",
                    details={"session_id": sid},
                    recoverable=True,
                )
            ).model_dump(exclude_none=True),
        )

    def _context_window_for_state() -> int:
        agent = getattr(app.state, "agent", None)
        cfg = getattr(agent, "_provider_config", None)
        return _resolve_expert_context_window(cfg) if cfg is not None else 0

    def _build_context_state(
        sid: str, scope: str, as_of: int | None = None
    ) -> ContextStateResponse:
        """Assemble the ARC live-context-plane view for a (session, scope). Shared by the
        GET state endpoint and the POST compact endpoint so both report identically.
        Combines the segment-store attribution (``live_tokens`` / editable ``categories``)
        with the model-grounded reading (``used_tokens`` from the last LM call) + the
        auto-compaction threshold."""
        arc = app.state.arc
        tokens_by_kind = arc.segment_tokens_by_kind(sid, scope)
        segments = arc.render_segments(sid, scope, as_of=as_of)
        live_tokens = sum(tokens_by_kind.values())
        window = _context_window_for_state()
        used = _last_prompt_tokens()  # model-grounded: last LM call's real prompt tokens
        return ContextStateResponse(
            session_id=sid,
            scope=scope,
            as_of=as_of,
            window_tokens=window,
            live_tokens=live_tokens,
            pct_used=(live_tokens / window) if window else None,
            used_tokens=used or None,
            used_pct=(used / window) if (window and used) else None,
            autocompact_pct=_autocompact_threshold(),
            live_block_count=len(segments),
            tokens_by_kind=tokens_by_kind,
            categories=_bucket_context_categories(tokens_by_kind, used, live_tokens),
            segments=[msgspec.to_builtins(s) for s in segments],
            render_text=arc.render_segment_text(sid, scope, as_of=as_of),
            render_keys=arc.render_segments_keys(sid, scope, as_of=as_of),
        )

    @app.get("/v1/sessions/{sid}/context/policy", response_model=SessionContextPolicy)
    async def get_session_context_policy(sid: str) -> SessionContextPolicy:
        """Return CLIO's effective context compartment policy for one session."""

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        ws = app.state.workspaces.get(sess.workspace_id)
        scope_meta = workspace_scope(ws).to_wire() if ws is not None else {}
        return SessionContextPolicy(
            session_id=sid,
            cross_session_read_available=True,
            cross_session_read_endpoint=f"/v1/sessions/{sid}/memory/tools/search-sessions",
            notes=[
                "Conversation retrieval and writes are scoped to the active session.",
                "Cross-session memory tools require explicit user intent or policy.",
                "Same-workspace memory can be searched through bounded, provenance-bearing tools.",
                "Other-workspace memory is denied by default.",
            ],
            metadata={
                "source": "clio_backend_default",
                "session_mode": sess.mode,
                "routing_mode": sess.routing_mode,
                "arc_wired": app.state.arc is not None,
                "workspace": scope_meta,
                "cross_session_default": "deny_without_user_intent",
                "global_scope_default": "deny_without_global_intent",
            },
        )

    @app.get("/v1/sessions/{sid}/context/state", response_model=ContextStateResponse)
    async def get_context_state(
        sid: str, scope: str, as_of: int | None = None
    ) -> ContextStateResponse:
        """Live ARC context-plane state for a (session, scope): model-window % used (both
        segment-attributed ``pct_used`` and model-grounded ``used_pct``), the per-category
        token breakdown, the auto-compaction threshold, and the current render."""
        if app.state.sessions.get(sid) is None:
            raise _session_not_found(sid)
        if app.state.arc is None:
            raise _arc_unavailable(sid)
        return _build_context_state(sid, scope, as_of)

    @app.post("/v1/sessions/{sid}/context/ops", response_model=ContextOpResponse)
    async def post_context_op(sid: str, req: ContextOpRequest) -> ContextOpResponse:
        """Apply one live-context operation (append/insert/delete/summarize) to a
        scope. A validated passthrough to the sanctioned apply_segment_op seam —
        clio does not choose the op, the caller does. The op auto-emits an arc.op
        Trace event (and an SSE frame)."""
        if app.state.sessions.get(sid) is None:
            raise _session_not_found(sid)
        arc = app.state.arc
        if arc is None:
            raise _arc_unavailable(sid)
        # Build only the kwargs relevant to req.op.
        if req.op in ("append", "insert"):
            kwargs: dict[str, Any] = {
                "kind": req.kind,
                "content": req.content or {},
                "step": req.step,
                "token_count": req.token_count,
                "trace_ref": req.trace_ref,
            }
            if req.op == "insert":
                kwargs["position"] = req.position
        elif req.op == "delete":
            kwargs = {"ids": req.ids or []}
        else:  # summarize
            kwargs = {
                "ids": req.ids or [],
                "summary_content": req.summary_content or {},
                "token_count": req.token_count,
                "trace_ref": req.trace_ref,
            }
        try:
            result = app.state.arc.apply_segment_op(req.op, sid, req.scope, **kwargs)
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="invalid_request",
                        message=str(exc),
                        details={"op": req.op, "scope": req.scope},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc
        tombstoned = result if req.op == "delete" else None
        result_dict = None if req.op == "delete" else msgspec.to_builtins(result)
        tokens_by_kind = arc.segment_tokens_by_kind(sid, req.scope)
        live_tokens = sum(tokens_by_kind.values())
        window = _context_window_for_state()
        return ContextOpResponse(
            session_id=sid,
            scope=req.scope,
            op=req.op,
            applied=True,
            result=result_dict,
            tombstoned_count=tombstoned,
            live_block_count=len(arc.render_segments(sid, req.scope)),
            tokens_by_kind=tokens_by_kind,
            pct_used=(live_tokens / window) if window else None,
        )

    @app.post("/v1/sessions/{sid}/context/compact", response_model=ContextStateResponse)
    async def post_context_compact(sid: str, scope: str) -> ContextStateResponse:
        """Manually compact a scope NOW (fire-and-forget). LLM-summarizes the scope's live
        working-set into ONE summary segment — the SAME summarizer the in-turn
        auto-compactor uses — via the sanctioned ``summarize`` op, then returns the fresh
        context state. The caller chooses WHEN to compact; clio chooses WHAT to keep (a
        faithful summary). 409 if nothing live; 503 if no LM is bound / the summary fails."""
        if app.state.sessions.get(sid) is None:
            raise _session_not_found(sid)
        arc = app.state.arc
        if arc is None:
            raise _arc_unavailable(sid)
        live = arc.render_segments(sid, scope)
        ids = [s.id for s in live]
        if not ids:
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="nothing_to_compact",
                        message=f"scope {scope!r} has no live segments to compact",
                        details={"scope": scope},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        summary = agents_runtime._summarize_segments_llm(live)
        if not summary:
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="compaction_unavailable",
                        message="summary LM call failed or no LM is bound",
                        details={"scope": scope},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        arc.apply_segment_op(
            "summarize",
            sid,
            scope,
            ids=ids,
            summary_content={"text": summary},
            token_count=_estimate_text_tokens(summary),
        )
        return _build_context_state(sid, scope)

    @app.get("/v1/sessions/{sid}/context/search", response_model=ContextSearchResponse)
    async def search_context(
        sid: str, q: str, scope_prefix: str = "", k: int = 10
    ) -> ContextSearchResponse:
        """Semantic discovery over a session's scopes — 'which expert/scope knows
        about X'. BM25 on the clio-core CTE backend, naive word-overlap on LocalFS."""
        if app.state.sessions.get(sid) is None:
            raise _session_not_found(sid)
        arc = app.state.arc
        if arc is None:
            raise _arc_unavailable(sid)
        hits = arc.search_segment_scopes(sid, q, scope_prefix=scope_prefix, k=k)
        return ContextSearchResponse(
            session_id=sid,
            query=q,
            semantic=arc.segment_search_is_semantic(),
            hits=[ContextSearchHit(scope=s, score=score) for s, score in hits],
        )
