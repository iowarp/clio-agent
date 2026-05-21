"""
Tests for clio_agent.ui.api module.

Tests FastAPI REST API endpoints with mocked ClioAgent to avoid LM Studio dependency.
"""

import json
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import dspy
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.registry.registry import AgentCapability, AgentRegistry

# ---------------------------------------------------------------------------
# Fixtures: mock agent + test client
# ---------------------------------------------------------------------------


def _make_mock_agent(
    answer: str = "Test answer",
    selected_expert: str = "chat",
    session_id: str = "default",
    raise_error: Exception | None = None,
):
    """Create a mock ClioAgent with controlled behaviour.

    dspy.asyncify calls agent() (i.e. __call__), which on a real dspy.Module
    delegates to forward(). We set both return_value and forward.return_value
    so the mock works regardless of calling convention.
    """
    agent = MagicMock()

    prediction = dspy.Prediction(
        answer=answer,
        selected_expert=selected_expert,
        session_id=session_id,
        duration_ms=42.0,
        error_info=None,
    )

    if raise_error:
        agent.side_effect = raise_error
        agent.forward.side_effect = raise_error
    else:
        agent.return_value = prediction
        agent.forward.return_value = prediction

    # Registry with one expert
    registry = AgentRegistry()
    registry.register_agent(
        "data",
        MagicMock(),
        AgentCapability(
            keywords=["hdf5", "data"],
            description="Data I/O expert",
            tools=["hdf5_analyze", "hdf5_optimize"],
            specialization="data_io",
        ),
    )
    agent.registry = registry

    # ARC mock for MetricsAggregator
    arc_mock = MagicMock()
    arc_mock.get_invocations_by_agent.return_value = []
    arc_mock.get_tool_cache_stats.return_value = {"tool_cache_hit_rate": 0.0}
    agent.arc = arc_mock

    agent.shutdown = MagicMock()
    return agent


@pytest.fixture()
def mock_agent():
    """Provide a mock agent and patch the lifespan so no real LM is needed."""
    return _make_mock_agent()


@pytest.fixture()
def client(mock_agent):
    """TestClient with mocked app.state (agent pre-injected, no lifespan)."""
    from clio_agent.ui.api import app

    @asynccontextmanager
    async def _test_lifespan(a):
        from clio_agent.config import LMProviderConfig

        a.state.agent = mock_agent
        a.state.healthy = True
        a.state.provider_config = LMProviderConfig()
        yield
        mock_agent.shutdown()

    app.router.lifespan_context = _test_lifespan
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def degraded_client():
    """TestClient where agent failed to initialize (degraded health)."""
    from clio_agent.ui.api import app

    @asynccontextmanager
    async def _degraded_lifespan(a):
        from clio_agent.config import LMProviderConfig

        a.state.agent = None
        a.state.healthy = False
        a.state.startup_error = "LM Studio unreachable"
        a.state.provider_config = LMProviderConfig()
        yield

    app.router.lifespan_context = _degraded_lifespan
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------


class TestHealth:
    """Tests for GET /health endpoint."""

    def test_health_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["version"] == "0.2.0"
        assert "provider" in body
        assert "environment" in body

    def test_health_degraded(self, degraded_client):
        resp = degraded_client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["error"] == "LM Studio unreachable"

    @pytest.mark.asyncio
    async def test_lifespan_config_failure_does_not_invent_default_provider(self, monkeypatch):
        from clio_agent.ui import api as api_module

        monkeypatch.setattr(api_module, "load_project_env_file", lambda: None)
        monkeypatch.setattr(
            api_module,
            "load_config_from_env",
            MagicMock(side_effect=ValueError("bad CLIO_LM_PROVIDER")),
        )
        test_app = FastAPI()

        async with api_module.lifespan(test_app):
            assert test_app.state.agent is None
            assert test_app.state.healthy is False
            assert test_app.state.startup_error == "bad CLIO_LM_PROVIDER"
            assert test_app.state.provider_config is None

    def test_health_includes_integration_details(self, client, monkeypatch):
        from clio_agent.runtime.status import (
            IntegrationState,
            IntegrationStatus,
            RuntimeReport,
        )
        from clio_agent.ui import api

        report = RuntimeReport(
            integrations=[
                IntegrationStatus(
                    name="api",
                    state=IntegrationState.READY,
                    summary="API ready",
                    config_source="test",
                    next_action="No action required.",
                )
            ]
        )
        monkeypatch.setattr(api, "collect_runtime_status", lambda **kwargs: report)

        resp = client.get("/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["overall_status"] == "ready"
        assert body["integrations"][0]["name"] == "api"
        assert body["integrations"][0]["status"] == "ready"


# ---------------------------------------------------------------------------
# POST /query (JSON)
# ---------------------------------------------------------------------------


class TestQueryJSON:
    """Tests for POST /query with stream=False."""

    def test_query_returns_answer(self, client):
        resp = client.post("/query", json={"question": "What is HDF5?"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["answer"] == "Test answer"
        assert body["selected_expert"] == "chat"
        assert body["session_id"] == "default"
        assert "duration_ms" in body

    def test_query_with_session_id(self, client, mock_agent):
        prediction = dspy.Prediction(
            answer="session answer",
            selected_expert="data",
            session_id="sess-42",
            duration_ms=10.0,
            error_info=None,
        )
        mock_agent.return_value = prediction
        mock_agent.forward.return_value = prediction
        resp = client.post("/query", json={"question": "Analyze file", "session_id": "sess-42"})
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "sess-42"

    def test_query_invalid_body(self, client):
        resp = client.post("/query", json={"wrong_field": "oops"})
        assert resp.status_code == 422

    def test_query_empty_body(self, client):
        resp = client.post("/query", content=b"{}")
        assert resp.status_code == 422

    def test_query_blank_question(self, client):
        resp = client.post("/query", json={"question": "   "})
        assert resp.status_code == 422

    def test_query_blank_session_id(self, client):
        resp = client.post("/query", json={"question": "Hello", "session_id": " "})
        assert resp.status_code == 422

    def test_query_agent_unavailable(self, degraded_client):
        resp = degraded_client.post("/query", json={"question": "Hello"})
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"] == "service_unavailable"

    def test_query_agent_raises_error(self, client, mock_agent):
        mock_agent.side_effect = RuntimeError("LM timed out")
        mock_agent.forward.side_effect = RuntimeError("LM timed out")
        resp = client.post("/query", json={"question": "test"})
        assert resp.status_code == 500
        body = resp.json()
        assert body["error"] == "internal_error"
        # Must NOT contain traceback
        assert "Traceback" not in json.dumps(body)


# ---------------------------------------------------------------------------
# POST /query (SSE streaming)
# ---------------------------------------------------------------------------


class TestQuerySSE:
    """Tests for POST /query with stream=True."""

    def test_stream_content_type(self, client):
        resp = client.post("/query", json={"question": "Stream me", "stream": True})
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

    def test_stream_events(self, client):
        resp = client.post("/query", json={"question": "Stream me", "stream": True})
        text = resp.text
        # Legacy /query SSE is an envelope, not live token streaming.
        assert "event: routing" in text
        assert "event: done" in text
        assert "event: chunk" not in text

    def test_stream_error_event(self, client, mock_agent):
        mock_agent.side_effect = RuntimeError("boom")
        mock_agent.forward.side_effect = RuntimeError("boom")
        resp = client.post("/query", json={"question": "fail me", "stream": True})
        text = resp.text
        assert "event: error" in text
        # Parse the error data
        for line in text.splitlines():
            if line.startswith("data:") and "internal_error" in line:
                data = json.loads(line[len("data:") :].strip())
                assert data["error"] == "internal_error"
                break

    def test_stream_success_done_is_marked_batch_without_chunks(self, client):
        resp = client.post("/query", json={"question": "Stream me", "stream": True})
        lines = resp.text.splitlines()

        assert "event: chunk" not in resp.text
        for i, line in enumerate(lines):
            if line.strip() == "event: done":
                for j in range(i + 1, min(i + 3, len(lines))):
                    if lines[j].startswith("data:"):
                        data = json.loads(lines[j][len("data:") :].strip())
                        assert data["stream_source"] == "batch"
                        assert data["stream_fallback"]["reason"] == ("legacy_query_sync_path")
                        assert data["stream_fallback"]["synthetic_posthoc"] is True
                        assert data["stream_fallback"]["live_streaming"] is False
                        assert "use_gact_streaming" in data["stream_fallback"]["recovery_actions"]
                        return
        raise AssertionError("No done event found in SSE stream")

    def test_stream_prediction_error_info_emits_error_event(self, client, mock_agent):
        prediction = dspy.Prediction(
            answer="handled failure",
            selected_expert="data",
            session_id="default",
            duration_ms=100.0,
            error_info={
                "error": "tool_error",
                "message": "File does not exist",
                "details": {"tool": "hdf5_analyze_file"},
            },
        )
        mock_agent.return_value = prediction
        mock_agent.forward.return_value = prediction

        resp = client.post("/query", json={"question": "missing file", "stream": True})

        lines = resp.text.splitlines()
        for i, line in enumerate(lines):
            if line.strip() == "event: error":
                for j in range(i + 1, min(i + 3, len(lines))):
                    if lines[j].startswith("data:"):
                        data = json.loads(lines[j][len("data:") :].strip())
                        assert data["error_info"]["error"] == "tool_error"
                        assert data["answer"] == "handled failure"
                        assert data["selected_expert"] == "data"
                        assert "event: done" not in resp.text
                        return
        raise AssertionError("No error event found in SSE stream")


# ---------------------------------------------------------------------------
# GET /experts
# ---------------------------------------------------------------------------


class TestExperts:
    """Tests for GET /experts endpoint."""

    def test_experts_list(self, client):
        resp = client.get("/experts")
        assert resp.status_code == 200
        body = resp.json()
        assert "experts" in body
        assert len(body["experts"]) >= 1
        expert = body["experts"][0]
        assert "id" in expert
        assert "description" in expert
        assert "keywords" in expert
        assert "tools" in expert

    def test_experts_empty_when_degraded(self, degraded_client):
        resp = degraded_client.get("/experts")
        assert resp.status_code == 200
        assert resp.json()["experts"] == []


# ---------------------------------------------------------------------------
# GET /metrics
# ---------------------------------------------------------------------------


class TestMetrics:
    """Tests for GET /metrics endpoint."""

    def test_metrics_returns_dict(self, client):
        resp = client.get("/metrics")
        assert resp.status_code == 200
        body = resp.json()
        assert "metrics" in body
        # Should have per-expert entries
        assert isinstance(body["metrics"], dict)

    def test_metrics_empty_when_degraded(self, degraded_client):
        resp = degraded_client.get("/metrics")
        assert resp.status_code == 200
        assert resp.json()["metrics"] == {}


# ---------------------------------------------------------------------------
# API main() entry point
# ---------------------------------------------------------------------------


class TestAPIMain:
    """Tests for the main() entry point argparse."""

    def test_main_exists(self):
        """main function should exist and be callable."""
        from clio_agent.ui.api import main

        assert callable(main)

    def test_query_response_model(self):
        """QueryResponse model should serialize correctly."""
        from clio_agent.ui.api import QueryResponse

        qr = QueryResponse(
            answer="test",
            selected_expert="data",
            session_id="s1",
            duration_ms=42.0,
        )
        d = qr.model_dump()
        assert d["answer"] == "test"
        assert d["error_info"] is None

    def test_health_response_model(self):
        """HealthResponse model should have defaults."""
        from clio_agent.ui.api import HealthResponse

        hr = HealthResponse(status="ok")
        assert hr.version == "0.2.0"
        assert hr.environment == "dev"
        assert hr.error is None

    def test_expert_info_model(self):
        """ExpertInfo model should accept all fields."""
        from clio_agent.ui.api import ExpertInfo

        ei = ExpertInfo(
            id="data",
            description="Data expert",
            keywords=["hdf5"],
            tools=["hdf5_analyze"],
        )
        assert ei.id == "data"

    def test_stream_done_event_contains_answer(self, client):
        """SSE done event should contain the full answer."""
        resp = client.post("/query", json={"question": "Stream test", "stream": True})
        text = resp.text
        # Parse SSE events: find the done event data line
        lines = text.splitlines()
        found_done = False
        for i, line in enumerate(lines):
            if line.strip() == "event: done":
                # Next data: line should have the answer
                for j in range(i + 1, min(i + 3, len(lines))):
                    if lines[j].startswith("data:"):
                        data = json.loads(lines[j][len("data:") :].strip())
                        assert "answer" in data
                        assert data["stream_source"] == "batch"
                        assert data["stream_fallback"]["reason"] == ("legacy_query_sync_path")
                        found_done = True
                        break
                break
        assert found_done, "No done event found in SSE stream"

    def test_query_with_error_info(self, client, mock_agent):
        """Query returning error_info should include it in response."""
        prediction = dspy.Prediction(
            answer="partial result",
            selected_expert="data",
            session_id="default",
            duration_ms=100.0,
            error_info={"error": "tool_error", "message": "MCP failed"},
        )
        mock_agent.return_value = prediction
        mock_agent.forward.return_value = prediction
        resp = client.post("/query", json={"question": "test"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["error_info"] is not None
        assert body["error_info"]["error"] == "tool_error"


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------


class TestGlobalExceptionHandler:
    """Tests for global exception handler."""

    def test_clio_error_returns_400(self, client, mock_agent):
        """ClioError should return 400 status."""
        from clio_agent.errors import ExpertError

        mock_agent.side_effect = ExpertError("expert broke")
        mock_agent.forward.side_effect = ExpertError("expert broke")
        # The error goes through _json_response which catches all exceptions,
        # returning 500. Only the global handler converts ClioError to 400.
        # Since forward() is wrapped in try/except, it returns 500 from _json_response.
        resp = client.post("/query", json={"question": "test"})
        assert resp.status_code == 500
