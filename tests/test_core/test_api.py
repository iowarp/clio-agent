"""
Tests for clio_agent.ui.api module.

Tests FastAPI REST API endpoints with mocked ClioAgent to avoid LM Studio dependency.
"""

import json
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import dspy
import pytest
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
    """Create a mock ClioAgent with controlled forward() behaviour."""
    agent = MagicMock()

    if raise_error:
        agent.forward.side_effect = raise_error
    else:
        agent.forward.return_value = dspy.Prediction(
            answer=answer,
            selected_expert=selected_expert,
            session_id=session_id,
            duration_ms=42.0,
            error_info=None,
        )

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
        mock_agent.forward.return_value = dspy.Prediction(
            answer="session answer",
            selected_expert="data",
            session_id="sess-42",
            duration_ms=10.0,
            error_info=None,
        )
        resp = client.post(
            "/query", json={"question": "Analyze file", "session_id": "sess-42"}
        )
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "sess-42"

    def test_query_invalid_body(self, client):
        resp = client.post("/query", json={"wrong_field": "oops"})
        assert resp.status_code == 422

    def test_query_empty_body(self, client):
        resp = client.post("/query", content=b"{}")
        assert resp.status_code == 422

    def test_query_agent_unavailable(self, degraded_client):
        resp = degraded_client.post("/query", json={"question": "Hello"})
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"] == "service_unavailable"

    def test_query_agent_raises_error(self, client, mock_agent):
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
        resp = client.post(
            "/query", json={"question": "Stream me", "stream": True}
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

    def test_stream_events(self, client):
        resp = client.post(
            "/query", json={"question": "Stream me", "stream": True}
        )
        text = resp.text
        # Must contain routing, chunk, and done events
        assert "event: routing" in text
        assert "event: chunk" in text
        assert "event: done" in text

    def test_stream_error_event(self, client, mock_agent):
        mock_agent.forward.side_effect = RuntimeError("boom")
        resp = client.post(
            "/query", json={"question": "fail me", "stream": True}
        )
        text = resp.text
        assert "event: error" in text
        # Parse the error data
        for line in text.splitlines():
            if line.startswith("data:") and "internal_error" in line:
                data = json.loads(line[len("data:"):].strip())
                assert data["error"] == "internal_error"
                break


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
