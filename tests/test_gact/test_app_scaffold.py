"""CLIO-BBBBBBBBBB6: smoke tests for the GACT v0.2 scaffold.

Verifies the FastAPI app builds, its baseline routes (/v1/health +
/v1/capabilities) respond with the v0.2 shape, and every route we
stubbed returns a v0.2-shaped 501 error envelope.

Full endpoint behaviour gets tested as each route is wired in
follow-on iterations (CLIO-BBBBBBBBBB7+).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import app as gact_app
from clio_agent.gact.app import build_app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(build_app())


def test_module_exports_build_app_and_main() -> None:
    """``clio_agent.gact`` re-exports the public API — breaking
    change guard against accidental import-path churn."""

    assert callable(gact_app.build_app)
    assert callable(gact_app.main)


def test_health_returns_v0_2_shape(client: TestClient) -> None:
    resp = client.get("/v1/health")
    assert resp.status_code in {200, 503}
    body = resp.json()
    # The default build_app() has no agent + no ARC wired, so the
    # integrations table flags both as unavailable/degraded and the
    # overall_status collapses accordingly. The scaffold test just
    # pins the shape — behavioural tests in test_doctor_integrations
    # cover the per-row logic.
    assert body["overall_status"] in {"ready", "degraded", "unavailable"}
    assert isinstance(body["healthy"], bool)
    # v0.2 integrations[] — at least one row with name + status.
    integrations = body.get("integrations", [])
    assert len(integrations) > 0, "v0.2 health must include integrations[]"
    for row in integrations:
        assert row.get("name"), f"row missing name: {row}"
        assert row.get("status") in {"ready", "degraded", "unavailable"}, row


def test_capabilities_advertises_v0_2(client: TestClient) -> None:
    resp = client.get("/v1/capabilities")
    assert resp.status_code == 200
    body = resp.json()
    assert body["contract_version"] == "0.2"
    assert body["backend"]["name"] == "clio-agent-gact"
    caps = body["capabilities"]
    # Capabilities the scaffold honestly provides today:
    assert caps["structured_errors"] is True, (
        "every error response is wrapped in the v0.2 envelope, so this must be True"
    )
    assert caps["integration_health"] is True, (
        "/v1/health returns integrations[], so this must be True"
    )
    assert caps["x_clio_cancellation"] == "best_effort"
    assert caps["x_clio_executor_cancellation"] is False
    assert caps["x_clio_text_streaming"] == "best_effort_live"
    assert caps["x_clio_synthetic_posthoc_streaming"] is True
    # Landed capabilities.
    for flag in (
        "sessions",
        "agent_routing",
        "memory",
        "metrics",
        "session_branching",
        "search_messages",
        "cost_tracking",
        "files",
        "diffs",
        "permissions",
        "subagents",
        "tool_telemetry",
    ):
        assert caps[flag] is True, (
            f"{flag} implemented — must advertise True"
        )


def test_stubbed_routes_return_501_with_v0_2_envelope() -> None:
    """Every scaffolded-but-not-wired route MUST return the v0.2
    error envelope with a 501, not a plain FastAPI 404 or bare
    string body.

    The envelope shape (${error: {error, message, details,
    recoverable}}$) is what a v0.2 client expects — honest reporting
    means the client can disable the affordance rather than error
    out mid-request.

    The build_app() stub list drained as the surface filled in
    (/v1/tools moved into a real catalog endpoint in commit 6199e9e),
    so we exercise the *envelope-builder* directly + stand up a
    throwaway FastAPI app wired the same way build_app does. Future
    re-stubs are covered automatically without parametrize churn.
    """

    from fastapi import FastAPI  # noqa: PLC0415
    from fastapi.responses import JSONResponse  # noqa: PLC0415

    from clio_agent.gact.app import _not_implemented  # noqa: PLC0415

    # 1. Envelope-builder shape — this is what every stub route returns.
    envelope = _not_implemented("probe_capability").model_dump(exclude_none=True)
    assert "error" in envelope
    inner = envelope["error"]
    assert inner["error"] == "config_error"
    assert "capability not yet implemented" in inner["message"]
    assert inner["details"]["capability"] == "probe_capability"
    assert inner["recoverable"] is False

    # 2. Wire-format check — register a route via the same pattern
    # build_app() uses for stubs and verify the HTTP response shape.
    probe_app = FastAPI()

    async def _probe_stub() -> JSONResponse:
        body = _not_implemented("probe_capability").model_dump(exclude_none=True)
        return JSONResponse(status_code=501, content=body)

    probe_app.add_api_route(
        "/v1/_probe_stub", _probe_stub, methods=["GET"], include_in_schema=False
    )
    resp = TestClient(probe_app).get("/v1/_probe_stub")
    assert resp.status_code == 501
    body = resp.json()
    assert body["error"]["error"] == "config_error"
    assert "capability not yet implemented" in body["error"]["message"]


def test_unknown_route_uses_structured_error_envelope(client: TestClient) -> None:
    resp = client.get("/v1/does-not-exist")

    assert resp.status_code == 404
    body = resp.json()
    assert "detail" not in body
    assert body["error"]["error"] == "not_found"
    assert body["error"]["message"] == "Not Found"


def test_request_validation_uses_structured_error_envelope(
    client: TestClient,
) -> None:
    resp = client.get("/v1/workspaces/ws_default/files/read")

    assert resp.status_code == 422
    body = resp.json()
    assert "detail" not in body
    assert body["error"]["error"] == "validation_error"
    assert body["error"]["message"] == "Request validation failed."
    assert body["error"]["details"]["errors"][0]["loc"] == ["query", "path"]


def test_unhandled_exception_uses_structured_error_envelope() -> None:
    app = build_app()

    @app.get("/v1/_boom")
    def _boom() -> None:
        raise RuntimeError("boom probe")

    resp = TestClient(app, raise_server_exceptions=False).get("/v1/_boom")

    assert resp.status_code == 500
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert "detail" not in body
    assert body["error"]["error"] == "internal_error"
    assert body["error"]["message"] == "Unhandled server error."
    assert body["error"]["details"]["original_error"] == "RuntimeError"
