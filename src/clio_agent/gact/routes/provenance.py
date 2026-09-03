"""Provider-neutral execution-provenance query routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import FastAPI, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from clio_agent import conf
from clio_agent.gact.agent_tasks import descendant_session_ids
from clio_agent.gact.provenance.child_projection import project_child_execution
from clio_agent.gact.provenance.normalization import normalize_semantic_events
from clio_agent.gact.provenance.protocol import ExecutionProvenanceReader
from clio_agent.gact.semantic_events import semantic_event_from_events_content
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def _typed_error(
    *, status_code: int, error: str, message: str, details: dict[str, Any]
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error=error,
                message=message,
                details=details,
                recoverable=status_code >= 500,
            )
        ).model_dump(exclude_none=True),
    )


#: The ARC event plane refused to hand over one session's segments. Reported per
#: session rather than allowed to fail the whole query: one unreadable child must
#: not cost the caller its parent's timeline.
CHILD_EVENTS_UNREADABLE = "child_events_unreadable"
#: No ARC event plane is configured at all, so the native provider has nothing to
#: read. Previously an empty list indistinguishable from "this session did nothing".
NATIVE_EVENT_PLANE_ABSENT = "native_event_plane_absent"


def _child_session_ids(app: FastAPI, sid: str) -> list[str]:
    """Return every descendant session, delegated children and user forks alike.

    Both substrates, one walk (:func:`descendant_sessions`): the task registry
    cannot see a fork and the session store cannot say which task owns a child.
    The lineage rows carry which is which, so the projection attributes them
    honestly rather than this walk deciding one of them does not exist.
    """

    return descendant_session_ids(app, sid)


def _native_events(
    app: FastAPI, session_ids: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Read each session's semantic segments, naming any the plane refuses."""

    arc = getattr(app.state, "arc", None)
    live = getattr(arc, "_live", None)
    iterator = getattr(live, "iter_session_event_segments", None)
    if arc is None or not callable(iterator):
        return [], [
            {
                "reason": NATIVE_EVENT_PLANE_ABSENT,
                "session_id": session_ids[0] if session_ids else "",
                "detail": "no ARC live event plane is installed; the native timeline is empty",
            }
        ]
    events: list[dict[str, Any]] = []
    degradations: list[dict[str, str]] = []
    for session_id in session_ids:
        try:
            segments = list(iterator(session_id))
        except Exception as exc:  # noqa: BLE001 - one child must not fail the parent
            degradations.append(
                {
                    "reason": CHILD_EVENTS_UNREADABLE,
                    "session_id": session_id,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        for segment in segments:
            event = semantic_event_from_events_content(
                segment.content,
                session_id=session_id,
                turn_id=str(segment.turn_id or ""),
            )
            events.append(event.to_dict())
    return events, degradations


def register_provenance_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register provider discovery and normalized execution queries."""
    del deps

    @app.get("/v1/provenance/providers")
    async def list_provenance_providers() -> dict[str, Any]:
        backend = getattr(app.state, "semantic_trace_backend", None)
        health_method = getattr(backend, "health", None)
        rows = health_method() if callable(health_method) else []
        native_row = next((row for row in rows if row.get("name") == "jsonl"), None)
        providers = [
            {
                "name": "native",
                "configured": True,
                "queryable": True,
                "durable": bool(native_row and native_row.get("durable")),
                "status": str((native_row or {}).get("status") or "ready"),
                "source": "arc+jsonl" if native_row else "arc",
                "health": native_row or {},
            }
        ]
        for row in rows:
            if row.get("name") == "flowcept":
                providers.append(
                    {
                        "name": "flowcept",
                        "configured": True,
                        "queryable": bool(row.get("queryable")),
                        "durable": False,
                        "status": str(row.get("status") or "ready"),
                        "source": "flowcept",
                        "health": row,
                    }
                )
        default_provider = (
            conf.resolve(
                "provenance.agentic.query_default",
                env="CLIO_PROVENANCE_QUERY_DEFAULT",
                default="native",
                cast=conf.as_str,
            )
            .strip()
            .lower()
        )
        artifact_backend = getattr(app.state, "artifact_provenance_backend", None)
        artifact_health = getattr(artifact_backend, "health", None)
        artifact_row = artifact_health() if callable(artifact_health) else {}
        artifact_provider = str(getattr(artifact_backend, "provider_name", "native") or "native")
        return {
            "schema_version": "clio.provenance_providers.v1",
            "default_provider": default_provider,
            "providers": providers,
            "artifact": {
                "provider": artifact_provider,
                "queryable": bool(artifact_row.get("queryable", True)),
                "durable": bool(artifact_row.get("durable", True)),
                "status": str(artifact_row.get("status") or "ready"),
                "health": artifact_row,
            },
        }

    @app.get("/v1/sessions/{sid}/provenance/execution")
    async def get_execution_provenance(
        sid: str,
        provider: str = "native",
        include_children: bool = True,
        limit: Annotated[int, Query(ge=1, le=10000)] = 2000,
    ) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise _typed_error(
                status_code=404,
                error="not_found",
                message=f"session not found: {sid}",
                details={"session_id": sid},
            )
        selected = provider.strip().lower() or "native"
        child_ids = _child_session_ids(app, sid) if include_children else []
        backend = getattr(app.state, "semantic_trace_backend", None)
        flush = getattr(backend, "flush", None)
        if callable(flush):
            await run_in_threadpool(flush)

        if selected == "native":
            reader_method = getattr(backend, "reader", None)
            reader = reader_method("jsonl") if callable(reader_method) else None
            # JSONL is the native provider's durable, complete view. ARC may
            # already have released older event segments after the downstream
            # durability receipt, so returning ARC merely because it still has
            # *some* rows produces a silently truncated timeline. Use ARC only
            # when no native durable reader is configured.
            if not isinstance(reader, ExecutionProvenanceReader):
                events, read_degradations = await run_in_threadpool(
                    _native_events, app, [sid, *child_ids]
                )
                normalized = normalize_semantic_events(
                    events,
                    provider="native",
                    session_id=sid,
                    limit=limit,
                )
                normalized["degradations"] = [
                    *(normalized.get("degradations") or []),
                    *read_degradations,
                ]
                return project_child_execution(
                    app,
                    sid,
                    normalized,
                    include_children=include_children,
                )
        else:
            reader_method = getattr(backend, "reader", None)
            reader = reader_method(selected) if callable(reader_method) else None

        if not isinstance(reader, ExecutionProvenanceReader):
            raise _typed_error(
                status_code=503,
                error="provenance_provider_unavailable",
                message=f"execution provenance provider is not configured: {selected}",
                details={"session_id": sid, "provider": selected},
            )
        try:
            result = await run_in_threadpool(
                reader.query_execution,
                session_id=sid,
                child_session_ids=child_ids,
                limit=limit,
            )
            return project_child_execution(app, sid, result, include_children=include_children)
        except Exception as exc:
            raise _typed_error(
                status_code=503,
                error="provenance_query_failed",
                message=f"execution provenance query failed for provider {selected}",
                details={
                    "session_id": sid,
                    "provider": selected,
                    "reason": f"{type(exc).__name__}: {exc}",
                },
            ) from exc
