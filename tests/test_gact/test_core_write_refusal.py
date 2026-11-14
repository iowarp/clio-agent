"""A refusal caused by clio-core refusing writes is typed and says so (MC-F3).

Found live: a full 128 MB store lost writes (code 12), the history waited to reconcile
them, and every message answered "Conversation history is recovering" while a new
conversation timed out with a bare 500; the person saw no error.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from clio_agent.arc import clio_core_retry as retry
from clio_agent.gact import core_write_refusal
from clio_agent.gact.error_middleware import install_error_envelope, install_typed_error_handlers
from clio_agent.gact.session_records import WRITE_FAILED_REASON
from clio_agent.gact.task_history import RecordingUnconfirmed

ORIGIN = "http://127.0.0.1:4191"


class _Refused(RuntimeError):
    native_code = 12


@pytest.fixture(autouse=True)
def _write_health() -> Iterator[None]:
    retry._reset_put_write_health_for_tests()
    yield
    retry._reset_put_write_health_for_tests()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, list[str]]]:
    events: list[str] = []
    monkeypatch.setattr(
        core_write_refusal.trace, "event", lambda _c, fmt, *a: events.append(fmt % a)
    )
    app = FastAPI()
    install_error_envelope(app)
    install_typed_error_handlers(app)
    app.add_middleware(CORSMiddleware, allow_origins=[ORIGIN])

    @app.post("/v1/sessions/{sid}/messages")
    async def submit(sid: str) -> None:
        raise RecordingUnconfirmed("Core observations require reconciliation")

    @app.post("/v1/sessions")
    async def create() -> None:
        raise TimeoutError(f"{WRITE_FAILED_REASON}: a session record did not land")

    @app.post("/v1/other")
    async def other() -> None:
        raise TimeoutError("unrelated")

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client, events


def _lose_a_write(code: int | None = 12) -> None:
    error = _Refused("clio-core AsyncPutBlob('x') returned code 12")
    if code is None:
        error = RuntimeError("clio-core refused")  # type: ignore[assignment]
    retry._record_lost_write(name="sess_a___events~m~2", payload=b"x" * 16384, exc=error)


def test_without_a_lost_write_the_refusals_keep_their_answers(client) -> None:
    test_client, events = client
    held = test_client.post("/v1/sessions/sess_a/messages", headers={"Origin": ORIGIN})
    assert held.status_code == 503
    assert held.json()["error"]["error"] == "core_recording_unconfirmed"
    assert test_client.post("/v1/sessions").status_code == 500
    assert events == []


def test_a_full_store_refuses_a_message_typed_and_traced(client) -> None:
    """SABOTAGE: return the original JSONResponse first in the handler and the
    person reads "Conversation history is recovering"."""
    test_client, events = client
    _lose_a_write()
    retry._record_put_success()  # a later small write landed: the history still waits
    response = test_client.post("/v1/sessions/sess_a/messages", headers={"Origin": ORIGIN})
    assert response.status_code == 503
    assert response.headers["access-control-allow-origin"] == ORIGIN  # readable in a browser
    error = response.json()["error"]
    assert error["error"] == core_write_refusal.CORE_WRITES_REFUSED
    assert error["message"].startswith("Not saved: CLIO's conversation store is full")
    assert error["recoverable"] is False
    assert error["details"]["native_code"] == 12
    assert error["details"]["lost_payload_bytes"] == 16384
    assert error["details"]["recovery_actions"] == ["free_disk_space", "enlarge_store", "restart"]
    (event,) = events
    assert event.startswith("clio_core_writes_refused session=sess_a route=/v1/sessions/sess_a/")
    assert "native_code=12" in event


def test_a_new_conversation_that_cannot_be_recorded_is_typed_not_a_bare_500(client) -> None:
    test_client, events = client
    _lose_a_write(code=None)
    created = test_client.post("/v1/sessions")
    assert created.status_code == 503
    assert created.json()["error"]["error"] == core_write_refusal.CORE_WRITES_REFUSED
    assert "refusing writes" in created.json()["error"]["message"]  # no code: not "full"
    assert test_client.post("/v1/other").status_code == 500  # unrelated errors unchanged
    assert len(events) == 1


def test_the_last_lost_write_outlives_a_later_success() -> None:
    _lose_a_write()
    assert retry.last_lost_put_write() is not None
    retry._record_put_success()
    assert retry.last_lost_put_write() is None  # health recovers, as before
    lost = retry.last_lost_put_write_ever()
    assert lost is not None and lost.native_code == 12
    assert lost.to_details()["native_code"] == 12
