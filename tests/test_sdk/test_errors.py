"""Error-taxonomy mapping contract (SPEC §6.0 + §14).

These tests encode the SDK's error contract against the live app:

* tag-first mapping (``permission_error`` on a 409 still maps to
  PermissionDeniedError),
* legacy tolerance (clio's ``internal_error``-on-404/422 emissions
  fall back to status-code mapping — SPEC §6.0 drift note),
* the envelope fields (tag, details, recoverable) survive onto the
  raised exception.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.sdk import (
    ClioAPIError,
    ClioClient,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
)
from tests.test_sdk.conftest import StreamingASGITransport, _fresh_arc


def test_unknown_session_maps_to_not_found_despite_legacy_tag(client: ClioClient) -> None:
    """Session-lookup 404s carry the ``not_found`` taxonomy tag; the SDK
    classifies by status regardless (#770 C4 retagged these off the
    legacy ``internal_error`` value, which the SDK also tolerates)."""

    with pytest.raises(NotFoundError) as excinfo:
        client.sessions.get("sess_missing")

    err = excinfo.value
    assert err.status_code == 404
    assert err.error == "not_found"
    assert isinstance(err.details, dict)


def test_empty_message_body_maps_to_invalid_request(client: ClioClient) -> None:
    sess = client.sessions.create(title="errors")

    with pytest.raises(InvalidRequestError) as excinfo:
        client.messages.post(sess.id, parts=[])

    assert excinfo.value.status_code == 422
    assert excinfo.value.recoverable is True


def test_validation_error_tag_maps_to_invalid_request(client: ClioClient) -> None:
    """FastAPI body validation failures carry the canonical
    ``validation_error`` tag — tag-first mapping applies."""

    with pytest.raises(InvalidRequestError) as excinfo:
        client.sessions.create(title="bad mode", mode="not_a_mode")

    assert excinfo.value.status_code == 422
    assert excinfo.value.error == "validation_error"
    assert "errors" in excinfo.value.details


def test_permission_error_tag_wins_over_conflict_status(client: ClioClient) -> None:
    """DELETE ws_default answers 409 with tag ``permission_error``
    (SPEC §6.1) — the tag, not the status, must pick the class."""

    with pytest.raises(PermissionDeniedError) as excinfo:
        client.workspaces.delete("ws_default")

    assert excinfo.value.status_code == 409
    assert excinfo.value.error == "permission_error"


def test_agent_not_available_maps_to_service_unavailable(tmp_path: Path) -> None:
    from clio_agent.gact.app import build_app

    app = build_app(sessions_path=tmp_path / "sessions.json", arc=_fresh_arc(tmp_path))
    transport = StreamingASGITransport(app)
    try:
        with ClioClient("http://testserver", transport=transport) as client:
            sess = client.sessions.create(title="no agent")
            with pytest.raises(ServiceUnavailableError) as excinfo:
                client.messages.post(sess.id, text="hello?")
    finally:
        transport.close()

    err = excinfo.value
    assert err.status_code == 503
    assert err.error == "agent_not_available"
    assert "agent_status" in err.details
    assert err.details.get("recovery_actions")


def test_error_envelope_fields_survive_onto_the_exception(client: ClioClient) -> None:
    """The raised error IS the envelope: tag + message + details +
    recoverable all reachable without re-parsing the response."""

    try:
        client.sessions.get("sess_missing")
    except ClioAPIError as err:
        assert err.info.message
        assert err.retry_after_s is None  # never emitted by clio today (SPEC §6.0)
        assert str(err.status_code) in str(err)
    else:  # pragma: no cover
        pytest.fail("expected a ClioAPIError")


def test_unknown_taxonomy_tag_falls_back_to_status(client: ClioClient) -> None:
    """Tags are an open set (SPEC §14.2): an unrecognized tag on a 404
    must still classify as NotFoundError."""

    import httpx

    from clio_agent.sdk.errors import error_from_response

    response = httpx.Response(
        404,
        json={
            "error": {
                "error": "x_futurevendor_thing",
                "message": "nope",
                "details": {},
                "recoverable": False,
            }
        },
        request=httpx.Request("GET", "http://testserver/v1/things/42"),
    )
    err = error_from_response(response)
    assert isinstance(err, NotFoundError)
    assert err.error == "x_futurevendor_thing"


def test_v01_code_key_is_tolerated() -> None:
    """v0.1 backends used ``code`` as the discriminator (SPEC §6.0)."""

    import httpx

    from clio_agent.sdk.errors import error_from_response

    response = httpx.Response(
        409,
        json={"error": {"code": "conflict", "message": "busy", "details": {}}},
        request=httpx.Request("POST", "http://testserver/v1/sessions/s/undo"),
    )
    err = error_from_response(response)
    assert err.error == "conflict"
    from clio_agent.sdk import ConflictError

    assert isinstance(err, ConflictError)
