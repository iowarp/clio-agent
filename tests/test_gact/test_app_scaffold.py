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
    assert resp.status_code == 200
    body = resp.json()
    assert body["healthy"] is True
    assert body["overall_status"] == "ready"
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
    # Landed capabilities.
    for flag in ("sessions", "agent_routing", "memory"):
        assert caps[flag] is True, (
            f"{flag} implemented — must advertise True"
        )
    # Capabilities not yet wired — advertised False so the TUI
    # doesn't try to render them against this backend.
    for flag in ("tool_telemetry",):
        assert caps[flag] is False, (
            f"{flag} not yet implemented; must advertise False until wired"
        )


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/v1/workspaces"),
        ("GET", "/v1/sessions/abc/messages"),
        ("GET", "/v1/sessions/abc/events"),
        ("GET", "/v1/tools"),
        ("GET", "/v1/commands"),
        ("GET", "/v1/metrics"),
    ],
)
def test_stubbed_routes_return_501_with_v0_2_envelope(
    client: TestClient, method: str, path: str
) -> None:
    """Every scaffolded-but-not-wired route MUST return the v0.2
    error envelope with a 501, not a plain FastAPI 404 or bare
    string body.

    The envelope shape (${error: {error, message, details,
    recoverable}}$) is what a v0.2 client expects — honest reporting
    means the client can disable the affordance rather than error
    out mid-request."""

    resp = client.request(method, path, json={} if method == "POST" else None)
    assert resp.status_code == 501, f"{method} {path}: status {resp.status_code}"
    body = resp.json()
    assert "error" in body, f"{method} {path}: missing `error` wrapper: {body}"
    inner = body["error"]
    assert isinstance(inner, dict), f"{method} {path}: error is not an object: {inner}"
    for required in ("error", "message"):
        assert required in inner, f"{method} {path}: error missing {required}: {inner}"
    # The taxonomy is a typed string; not_implemented stubs use
    # "config_error" (the endpoint is configurable / not ready).
    assert inner["error"] == "config_error"
    assert "capability not yet implemented" in inner["message"]
