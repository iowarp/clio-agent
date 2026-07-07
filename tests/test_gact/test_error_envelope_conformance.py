"""Error-envelope conformance sweep across the gact negative routes (#773).

Pins the §14 error taxonomy contract enforced centrally by the three global
exception handlers in :mod:`clio_agent.gact.app` (``_http_exception_handler``,
``_validation_exception_handler``, ``_unhandled_exception_handler``): every 4xx/5xx
response — whether raised by a handler as an explicit ``ErrorEnvelope`` or produced
by FastAPI/Starlette itself — must deserialize to ``{"error": {"error": <tag>,
"message": <str>, "recoverable": <bool>, ...}}`` with a machine-readable taxonomy
tag. These are negative-path tests: unknown-session 404s across routes, malformed
bodies 422, agent-not-ready 503, generic route 404/405, and a monkeypatched 500.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


def _assert_envelope(payload: Any, *, tag: str) -> dict[str, Any]:
    """Assert ``payload`` is a §14 error envelope carrying taxonomy ``tag``.

    Returns the inner ``ErrorInfo`` dict so callers can make extra assertions.
    """

    assert isinstance(payload, dict), f"envelope must be an object, got {type(payload)}"
    assert set(payload.keys()) == {"error"}, f"envelope must have exactly one 'error' key: {payload}"
    info = payload["error"]
    assert isinstance(info, dict), f"error must wrap an ErrorInfo object: {info!r}"
    assert info.get("error") == tag, f"expected taxonomy tag {tag!r}, got {info.get('error')!r}"
    assert isinstance(info.get("message"), str) and info["message"], "message must be non-empty str"
    assert isinstance(info.get("recoverable"), bool), "recoverable must be a bool"
    # retry_after_s is Optional and excluded when None (exclude_none=True); if
    # present it must be an int.
    if "retry_after_s" in info:
        assert isinstance(info["retry_after_s"], int)
    return info


UNKNOWN = "sess_does_not_exist"

# (method, path, json body) — every one must 404 with a not_found envelope for an
# unknown session id. Spans several route modules (sessions.py, messages.py).
_UNKNOWN_SESSION_ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", f"/v1/sessions/{UNKNOWN}", None),
    ("GET", f"/v1/sessions/{UNKNOWN}/messages", None),
    ("POST", f"/v1/sessions/{UNKNOWN}/undo", {"count": 1}),
    ("POST", f"/v1/sessions/{UNKNOWN}/rewind", {"message_id": "msg_x"}),
    ("POST", f"/v1/sessions/{UNKNOWN}/messages", {"text": "hi"}),
]


@pytest.mark.parametrize(("method", "path", "body"), _UNKNOWN_SESSION_ROUTES)
def test_unknown_session_returns_not_found_envelope(
    tmp_path: Path, method: str, path: str, body: dict[str, Any] | None
) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        resp = client.request(method, path, json=body)
    assert resp.status_code == 404, resp.text
    info = _assert_envelope(resp.json(), tag="not_found")
    assert info.get("recoverable") is False


# Routes with a typed (pydantic) request body: a broken payload trips
# RequestValidationError -> the global 422 validation envelope. (undo/rewind read
# an *optional* free-form body and treat a malformed one as ``{}`` by design, so
# they are covered by the micro-fix's own caplog test, not here.)
@pytest.mark.parametrize(
    "path",
    [
        "/v1/sessions",
        "/v1/sessions/{sid}/messages",
    ],
)
def test_malformed_body_returns_validation_envelope(tmp_path: Path, path: str) -> None:
    """A syntactically broken JSON payload must surface a 422 validation envelope,
    not a 500, on routes with a typed body."""

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "envelope"}).json()["id"]
        resp = client.post(
            path.format(sid=sid),
            content=b"{not valid json",
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 422, resp.text
    _assert_envelope(resp.json(), tag="validation_error")


def test_agent_not_ready_returns_503_agent_not_available(tmp_path: Path) -> None:
    """With no executable agent wired, POST /messages must 503 with the typed
    ``agent_not_available`` envelope (not a generic internal_error)."""

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "no-agent"}).json()["id"]
        resp = client.post(f"/v1/sessions/{sid}/messages", json={"text": "hi"})
    assert resp.status_code == 503, resp.text
    info = _assert_envelope(resp.json(), tag="agent_not_available")
    assert info["details"]["session_id"] == sid


def test_unknown_route_returns_not_found_envelope(tmp_path: Path) -> None:
    """A Starlette-generated 404 (unrouted path) is wrapped by the global
    HTTPException handler into the same envelope shape."""

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        resp = client.get("/v1/this/route/does/not/exist")
    assert resp.status_code == 404, resp.text
    _assert_envelope(resp.json(), tag="not_found")


def test_method_not_allowed_returns_unsupported_envelope(tmp_path: Path) -> None:
    """A 405 from an unsupported method maps to the ``unsupported`` taxonomy tag."""

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        resp = client.request("DELETE", "/v1/sessions")
    assert resp.status_code == 405, resp.text
    _assert_envelope(resp.json(), tag="unsupported")


def test_unhandled_exception_returns_internal_error_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected exception inside a handler must be caught by the global
    Exception handler and returned as a structured 500 internal_error envelope
    rather than leaking a raw traceback to the client."""

    app = build_app(sessions_path=tmp_path / "s.json")

    def _boom(_sid: str) -> None:
        raise RuntimeError("synthetic store failure")

    monkeypatch.setattr(app.state.sessions, "get", _boom)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/v1/sessions/sess_any")
    assert resp.status_code == 500, resp.text
    info = _assert_envelope(resp.json(), tag="internal_error")
    assert info.get("recoverable") is False
    assert info["details"]["original_error"] == "RuntimeError"
