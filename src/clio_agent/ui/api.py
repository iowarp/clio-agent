"""
REST API Server for ClioAgent

FastAPI application with SSE streaming, health checks, expert discovery,
and per-expert metrics. All errors are structured JSON (never raw tracebacks).

Endpoints:
    POST /query    -- Query the agent (JSON or SSE streaming response)
    GET  /health   -- Health check with provider info
    GET  /experts  -- List registered experts with capabilities
    GET  /metrics  -- Per-expert performance metrics

Usage:
    >>> # Start with uvicorn
    >>> uvicorn clio_agent.ui.api:app --host 0.0.0.0 --port 8000

    >>> # Or via CLI entry point
    >>> clio-agent-api --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sse_starlette.sse import EventSourceResponse

from clio_agent.config import load_config_from_env, load_project_env_file, setup_dspy
from clio_agent.errors import ClioError, format_error_response
from clio_agent.runtime.status import IntegrationState, collect_runtime_status

logger = logging.getLogger(__name__)


# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================


class QueryRequest(BaseModel):
    """Request body for POST /query."""

    question: str
    session_id: str = "default"
    stream: bool = False

    @field_validator("question", "session_id")
    @classmethod
    def _reject_blank_strings(cls, value: str) -> str:
        """Reject blank query fields before they reach the agent runtime."""
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class QueryResponse(BaseModel):
    """Response body for POST /query (non-streaming)."""

    answer: str
    selected_expert: str
    route_source: str = ""
    route_reason: str = ""
    session_id: str
    duration_ms: float
    error_info: dict[str, Any] | None = None


class HealthResponse(BaseModel):
    """Response body for GET /health."""

    status: str
    version: str = "0.2.0"
    provider: str = ""
    environment: str = "dev"
    overall_status: str = ""
    integrations: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class ExpertInfo(BaseModel):
    """Single expert entry in GET /experts response."""

    id: str
    description: str
    keywords: list[str]
    tools: list[str]


class ExpertsResponse(BaseModel):
    """Response body for GET /experts."""

    experts: list[ExpertInfo]


class MetricsResponse(BaseModel):
    """Response body for GET /metrics."""

    metrics: dict[str, Any]


# ============================================================================
# APPLICATION LIFECYCLE
# ============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: initialize agent on startup, shutdown on exit."""
    # Startup
    try:
        load_project_env_file()
        config = load_config_from_env()
        app.state.provider_config = config
        setup_dspy()

        from clio_agent.agent import ClioAgent

        startup_agent = ClioAgent()
        app.state.agent = startup_agent
        app.state.healthy = True
        logger.info("ClioAgent initialized (provider=%s)", config.provider)
    except Exception as e:
        logger.error("Failed to initialize ClioAgent: %s", e)
        app.state.agent = None
        app.state.healthy = False
        app.state.startup_error = str(e)
        # Store config even on failure for health endpoint
        try:
            app.state.provider_config = load_config_from_env()
        except Exception:
            from clio_agent.config import LMProviderConfig

            app.state.provider_config = LMProviderConfig()

    yield

    # Shutdown
    shutdown_agent = getattr(app.state, "agent", None)
    if shutdown_agent is not None:
        shutdown_agent.shutdown()
        logger.info("ClioAgent shut down")


app = FastAPI(
    title="CLIO Agent API",
    version="0.2.0",
    description="REST API for CLIO Agent -- Autonomous AI agent for scientific data",
    lifespan=lifespan,
)


# ============================================================================
# GLOBAL EXCEPTION HANDLER
# ============================================================================


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch all unhandled exceptions and return structured error JSON."""
    error_body = format_error_response(exc)
    status_code = 500
    if isinstance(exc, ClioError):
        status_code = 400
    return JSONResponse(status_code=status_code, content=error_body)


# ============================================================================
# ENDPOINTS
# ============================================================================


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Health check with provider info.

    Returns status "ok" when agent is initialized, "degraded" otherwise.
    """
    config = getattr(app.state, "provider_config", None)
    if getattr(app.state, "healthy", False):
        overall_status, integrations = _runtime_health_detail(IntegrationState.READY)
        return HealthResponse(
            status="ok",
            provider=config.provider if config else "",
            environment=config.environment if config else "dev",
            overall_status=overall_status,
            integrations=integrations,
        )
    error_msg = getattr(app.state, "startup_error", "Agent not initialized")
    overall_status, integrations = _runtime_health_detail(
        IntegrationState.DEGRADED,
        api_error=error_msg,
    )
    return HealthResponse(
        status="degraded",
        provider=config.provider if config else "",
        environment=config.environment if config else "dev",
        overall_status=overall_status,
        integrations=integrations,
        error=error_msg,
    )


def _runtime_health_detail(
    api_state: IntegrationState,
    *,
    api_error: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Build health integration detail without letting doctor probes break /health."""
    try:
        report = collect_runtime_status(
            api_state=api_state,
            api_error=api_error,
            lm_timeout=0.5,
        )
    except Exception as exc:
        logger.warning("Runtime status collection failed during health check: %s", exc)
        return IntegrationState.DEGRADED.value, []

    data = report.to_dict()
    return report.overall_status, data["integrations"]


@app.post("/query")
async def query(req: QueryRequest):
    """Query the agent. Returns JSON or SSE stream depending on stream flag."""
    agent = getattr(app.state, "agent", None)
    if agent is None:
        error_msg = getattr(app.state, "startup_error", "Agent not initialized")
        return JSONResponse(
            status_code=503,
            content=format_error_response(ClioError(error_msg, error_type="service_unavailable")),
        )

    if req.stream:
        return EventSourceResponse(_stream_response(agent, req))
    else:
        return await _json_response(agent, req)


async def _json_response(agent: Any, req: QueryRequest) -> JSONResponse:
    """Execute agent query and return JSON response."""
    try:
        start = time.time()
        result = await asyncio.to_thread(agent, question=req.question, session_id=req.session_id)
        duration_ms = (time.time() - start) * 1000

        return JSONResponse(
            content=QueryResponse(
                answer=result.answer,
                selected_expert=result.selected_expert,
                route_source=getattr(result, "route_source", ""),
                route_reason=getattr(result, "route_reason", ""),
                session_id=result.session_id,
                duration_ms=duration_ms,
                error_info=getattr(result, "error_info", None),
            ).model_dump()
        )
    except Exception as e:
        error_body = format_error_response(e)
        return JSONResponse(status_code=500, content=error_body)


async def _stream_response(agent: Any, req: QueryRequest):
    """SSE generator: routing -> chunk(s) -> done | error events."""
    try:
        start = time.time()
        result = await asyncio.to_thread(agent, question=req.question, session_id=req.session_id)
        duration_ms = (time.time() - start) * 1000

        # Event: routing
        yield {
            "event": "routing",
            "data": json.dumps(
                {
                    "selected_expert": result.selected_expert,
                    "route_source": getattr(result, "route_source", ""),
                    "route_reason": getattr(result, "route_reason", ""),
                }
            ),
        }

        # Event: chunk -- split answer into word chunks for SSE infrastructure
        words = result.answer.split()
        chunk_size = max(1, len(words) // 5) if words else 1
        for i in range(0, len(words), chunk_size):
            chunk_text = " ".join(words[i : i + chunk_size])
            yield {
                "event": "chunk",
                "data": json.dumps({"text": chunk_text}),
            }
            await asyncio.sleep(0.02)

        # Event: done
        yield {
            "event": "done",
            "data": json.dumps(
                {
                    "answer": result.answer,
                    "selected_expert": result.selected_expert,
                    "route_source": getattr(result, "route_source", ""),
                    "route_reason": getattr(result, "route_reason", ""),
                    "duration_ms": duration_ms,
                    "error_info": getattr(result, "error_info", None),
                }
            ),
        }
    except Exception as e:
        error_body = format_error_response(e)
        yield {
            "event": "error",
            "data": json.dumps(error_body),
        }


@app.get("/experts", response_model=ExpertsResponse)
async def experts() -> ExpertsResponse:
    """List registered experts with capabilities."""
    agent = getattr(app.state, "agent", None)
    if agent is None:
        return ExpertsResponse(experts=[])

    registry = agent.registry
    all_caps = registry.get_all_capabilities()
    expert_list = []
    for agent_id, caps in sorted(all_caps.items()):
        expert_list.append(
            ExpertInfo(
                id=agent_id,
                description=caps.description,
                keywords=caps.keywords,
                tools=caps.tools,
            )
        )
    return ExpertsResponse(experts=expert_list)


@app.get("/metrics", response_model=MetricsResponse)
async def metrics() -> MetricsResponse:
    """Per-expert performance metrics from MetricsAggregator."""
    agent = getattr(app.state, "agent", None)
    if agent is None:
        return MetricsResponse(metrics={})

    from clio_agent.optimizer.instrumentation import MetricsAggregator

    aggregator = MetricsAggregator(agent.arc)
    registry = agent.registry
    all_caps = registry.get_all_capabilities()

    metrics_dict: dict[str, Any] = {}
    for agent_id in all_caps:
        metrics_dict[agent_id] = aggregator.compute_expert_metrics(agent_id)

    return MetricsResponse(metrics=metrics_dict)


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================


def main() -> None:
    """CLI entry point: parse args and run uvicorn."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="CLIO Agent REST API")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    args = parser.parse_args()

    uvicorn.run(
        "clio_agent.ui.api:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
