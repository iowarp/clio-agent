"""Tests for the shared ``json_body`` request-body helper (iowarp/clio-agent#772).

The helper replaces the scattered ``try: body = await request.json() except:
body = {}`` idiom in the gact route handlers with one path that (a) returns the
parsed object for a valid body, and (b) treats a malformed or non-object body as
an empty mapping *while emitting a structured ``request_body_unparseable``
warning* -- so the degraded path is visible instead of silent. The route-level
cases pin behavior preservation: a malformed body still resolves to ``{}`` at a
real endpoint, with no new 4xx/5xx.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.routes._body import NonObjectBodyError, json_body

_ROUTE_LOGGER = "clio_agent.gact.routes.body"


@pytest.fixture()
def echo_client() -> TestClient:
    """A tiny app whose one endpoint echoes ``json_body``'s result."""

    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, Any]:
        return await json_body(request, route="POST /echo")

    return TestClient(app)


@pytest.fixture()
def strict_client() -> TestClient:
    """Endpoints exercising ``non_object="raise"`` (with/without null coercion)."""

    app = FastAPI()

    @app.post("/strict")
    async def strict(request: Request) -> dict[str, Any]:
        try:
            return await json_body(request, route="POST /strict", non_object="raise")
        except NonObjectBodyError as exc:
            return {"rejected": exc.payload_type}

    @app.post("/strict-no-null")
    async def strict_no_null(request: Request) -> dict[str, Any]:
        try:
            return await json_body(
                request, route="POST /strict-no-null", non_object="raise", null_is_empty=False
            )
        except NonObjectBodyError as exc:
            return {"rejected": exc.payload_type}

    return TestClient(app)


def test_json_body_returns_parsed_dict(echo_client: TestClient) -> None:
    """A well-formed JSON object body is returned verbatim."""

    resp = echo_client.post("/echo", json={"a": 1, "nested": {"b": 2}})

    assert resp.status_code == 200
    assert resp.json() == {"a": 1, "nested": {"b": 2}}


def test_json_body_malformed_returns_empty_and_logs(
    echo_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed body resolves to ``{}`` AND logs the structured reason.

    This assertion is the RED-before-the-helper contract: the old inline
    ``except: body = {}`` swallowed the failure with no trace. The helper must
    surface a ``request_body_unparseable`` warning carrying the route and the
    exception type.
    """

    with caplog.at_level(logging.WARNING, logger=_ROUTE_LOGGER):
        resp = echo_client.post(
            "/echo",
            content=b"{not: valid json",
            headers={"content-type": "application/json"},
        )

    assert resp.status_code == 200
    assert resp.json() == {}

    records = [r for r in caplog.records if r.name == _ROUTE_LOGGER]
    assert records, "expected a structured warning from the json_body helper"
    reason = getattr(records[-1], "structured_reason", None)
    assert reason is not None
    assert reason["reason"] == "request_body_unparseable"
    assert reason["category"] == "request_validation"
    assert reason["route"] == "POST /echo"
    assert reason["exception_type"] == "JSONDecodeError"
    # The rendered message stays greppable in the trace/log stream.
    assert "request_body_unparseable" in caplog.text
    assert "POST /echo" in caplog.text


def test_json_body_non_object_returns_empty_and_logs(
    echo_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A syntactically valid *non-object* body (a list) is treated as empty."""

    with caplog.at_level(logging.WARNING, logger=_ROUTE_LOGGER):
        resp = echo_client.post("/echo", json=[1, 2, 3])

    assert resp.status_code == 200
    assert resp.json() == {}

    records = [r for r in caplog.records if r.name == _ROUTE_LOGGER]
    assert records, "expected a structured warning for a non-object body"
    reason = getattr(records[-1], "structured_reason", None)
    assert reason is not None
    assert reason["reason"] == "request_body_unparseable"
    assert reason["route"] == "POST /echo"
    assert reason["payload_type"] == "list"


def test_json_body_valid_non_dict_does_not_log(
    echo_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A valid object body emits no warning (the happy path stays quiet)."""

    with caplog.at_level(logging.WARNING, logger=_ROUTE_LOGGER):
        resp = echo_client.post("/echo", json={"ok": True})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert [r for r in caplog.records if r.name == _ROUTE_LOGGER] == []


def test_json_body_raise_mode_rejects_list_without_logging(
    strict_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Under ``non_object="raise"`` a parsed non-object raises
    ``NonObjectBodyError`` (carrying the payload type) and emits NO fallback
    warning -- the caller surfaces an explicit rejection, nothing degrades."""

    with caplog.at_level(logging.WARNING, logger=_ROUTE_LOGGER):
        resp = strict_client.post("/strict", json=[1, 2])

    assert resp.status_code == 200
    assert resp.json() == {"rejected": "list"}
    assert [r for r in caplog.records if r.name == _ROUTE_LOGGER] == []


def test_json_body_raise_mode_still_coerces_null_and_malformed(
    strict_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """``non_object="raise"`` with the default ``null_is_empty=True`` keeps the
    degrade-to-``{}`` path (with its structured reason) for ``null`` and for a
    malformed body -- only deliberate wrong-shaped JSON is rejected."""

    headers = {"content-type": "application/json"}
    with caplog.at_level(logging.WARNING, logger=_ROUTE_LOGGER):
        null_resp = strict_client.post("/strict", content=b"null", headers=headers)
        malformed_resp = strict_client.post("/strict", content=b"{oops", headers=headers)

    assert null_resp.json() == {}
    assert malformed_resp.json() == {}
    assert len([r for r in caplog.records if r.name == _ROUTE_LOGGER]) == 2


def test_json_body_raise_mode_null_is_empty_false_rejects_null(
    strict_client: TestClient,
) -> None:
    """With ``null_is_empty=False`` a JSON ``null`` body is rejected like any
    other non-object (rewind's pre-#772 contract); a malformed body still
    degrades to ``{}``."""

    headers = {"content-type": "application/json"}

    null_resp = strict_client.post("/strict-no-null", content=b"null", headers=headers)
    malformed_resp = strict_client.post("/strict-no-null", content=b"{oops", headers=headers)

    assert null_resp.json() == {"rejected": "NoneType"}
    assert malformed_resp.json() == {}


# --- behavior preservation at real converted route handlers -----------------


@pytest.fixture()
def app_client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json"))


def test_prompts_reload_accepts_malformed_body(app_client: TestClient) -> None:
    """POST /v1/prompts/reload treats a malformed body as empty (no 4xx/5xx)."""

    resp = app_client.post(
        "/v1/prompts/reload",
        content=b"{oops",
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 200
    assert "reload" in resp.json()


def test_prompts_reload_accepts_absent_body(app_client: TestClient) -> None:
    """An absent body is still an empty mapping, exactly as before."""

    resp = app_client.post("/v1/prompts/reload")

    assert resp.status_code == 200
    assert "reload" in resp.json()


def test_agents_extract_accepts_malformed_body(app_client: TestClient) -> None:
    """POST /v1/agents/extract swallows a malformed body to empty session_ids."""

    resp = app_client.post(
        "/v1/agents/extract",
        content=b"not json at all",
        headers={"content-type": "application/json"},
    )

    # No 5xx from the body parse; the handler proceeds with an empty body.
    assert resp.status_code != 500
