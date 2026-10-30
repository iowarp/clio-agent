"""#1172: an unreadable stored transcript is a typed 503, never a bare 500 or an empty list.

The chain under test: clio-core ``GetBlob`` raises a bare ``RuntimeError`` for a blob
whose size it reports -> ``read_framed_blob`` raises ``BlobReadError`` -> both
transcript read paths raise ``LedgerReadError`` with a cause (so the resident set
records ``rehydrate_failed``) -> ``GET /v1/sessions/{sid}/messages`` answers
``503 messages_unavailable``, with no blob names in the response.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.arc import rpc_liveness, storage
from clio_agent.arc.blob_frame import BlobFrameError, BlobReadError, frame, read_framed_blob
from clio_agent.gact import transcript_pages, transcript_projection
from clio_agent.gact.app import build_app
from clio_agent.gact.messages import LedgerReadError
from clio_agent.gact.transcript_unreadable import (
    STORAGE_UNREACHABLE,
    TRANSCRIPT_CORRUPT,
    typed_transcript_read,
)


class _UnreadableTag:
    def __init__(self, name: str) -> None:
        self.name = name

    def GetBlobSize(self, _blob: str) -> int:  # noqa: N802 - binding name
        return 128

    def GetBlob(self, _blob: str, _size: int, _offset: int) -> bytes:  # noqa: N802
        raise RuntimeError("GetBlob operation failed")


def test_store_get_types_a_present_but_unreadable_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rpc_liveness, "call_with_liveness", lambda fn, **_: fn())
    stub: Any = SimpleNamespace(
        _live=lambda: None,
        _cte=SimpleNamespace(Tag=_UnreadableTag),
        tag=lambda kind: f"ns/{kind}",
        _gate=SimpleNamespace(port=9413, note_rpc_stalled=lambda *a, **k: None),
        _reconnect=lambda: None,
    )

    with pytest.raises(BlobReadError) as info:
        storage.ClioCoreStore.get(stub, "_events", "sess_x~m~0")

    assert info.value.details == {"name": "sess_x~m~0", "cause": "GetBlob operation failed"}
    assert isinstance(info.value.__cause__, RuntimeError)


def test_the_framed_length_retry_is_typed_too() -> None:
    """The stale-size retry read goes through the same check as the first read."""
    body = frame(b"x" * 40)
    reads: list[int] = []

    def read(length: int) -> bytes:
        reads.append(length)
        if len(reads) == 1:
            return body[:10]  # stale size: the frame says more is stored
        raise RuntimeError("GetBlob operation failed")

    with pytest.raises(BlobReadError):
        read_framed_blob("sess_x~m~0", 10, read)
    assert len(reads) == 2


@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (
            BlobReadError("sess_x~m~0", RuntimeError("GetBlob operation failed")),
            STORAGE_UNREACHABLE,
        ),
        (BlobFrameError("sess_x~m~0", 80, 10), TRANSCRIPT_CORRUPT),
    ],
)
def test_blob_failures_become_a_ledger_read_error_with_a_cause(
    error: Exception, cause: str
) -> None:
    with pytest.raises(LedgerReadError) as info, typed_transcript_read("sess_x"):
        raise error

    assert info.value.cause == cause
    assert "sess_x~m~0" not in str(info.value)
    assert info.value.__cause__ is error


def test_materialize_ledger_types_an_unreadable_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    def _unreadable(_app: object, _sid: str) -> None:
        raise BlobReadError("sess_x~m~0", RuntimeError("GetBlob operation failed"))

    monkeypatch.setattr(transcript_projection, "materialize_from_atoms", _unreadable)

    with pytest.raises(LedgerReadError) as info:
        transcript_projection.materialize_ledger(SimpleNamespace(), "sess_x")

    assert info.value.cause == STORAGE_UNREACHABLE


def _session_with_stored_transcript(tmp_path: Path) -> tuple[Any, TestClient, str]:
    app = build_app(sessions_path=tmp_path / "s.json")
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={}).json()["id"]
    # The session has a stored transcript, so the route reads it rather than answering [].
    app.state.sessions.get(sid).message_count = 2
    return app, client, sid


def test_full_read_is_a_typed_recoverable_503_not_500_or_empty(tmp_path: Path) -> None:
    app, client, sid = _session_with_stored_transcript(tmp_path)
    audits: list[dict[str, Any]] = []
    app.state.messages._audit = audits.append

    def _unreadable(session_id: str) -> None:
        with typed_transcript_read(session_id):
            raise BlobReadError("sess_x~m~0", RuntimeError("GetBlob operation failed"))

    app.state.messages._materialize = _unreadable

    response = client.get(f"/v1/sessions/{sid}/messages")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    error = response.json()["error"]
    assert error["error"] == "messages_unavailable"
    assert error["recoverable"] is True
    assert error["details"] == {"session_id": sid, "cause": STORAGE_UNREACHABLE}
    assert "sess_x~m~0" not in response.text
    assert any(a["reason"] == "rehydrate_failed" and a["session_id"] == sid for a in audits)


def test_paged_read_is_typed_and_a_corrupt_transcript_is_not_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The UI's bounded read takes the lane path, which never materializes the ledger."""
    _app, client, sid = _session_with_stored_transcript(tmp_path)

    def _corrupt(*_args: object, **_kwargs: object) -> None:
        raise BlobFrameError("sess_x~m~0", 80, 10)

    monkeypatch.setattr(transcript_pages, "read_lane_page", _corrupt)
    monkeypatch.setattr("clio_agent.gact.transcript_file._canonical_log_arc", lambda *_: object())
    monkeypatch.setattr(
        "clio_agent.gact.transcript_projection.canonical_log_arc", lambda *_: object()
    )

    response = client.get(f"/v1/sessions/{sid}/messages", params={"limit": 20})

    assert response.status_code == 503
    assert "retry-after" not in response.headers
    error = response.json()["error"]
    assert error["error"] == "messages_unavailable"
    assert error["recoverable"] is False
    assert error["details"]["cause"] == TRANSCRIPT_CORRUPT


def test_a_session_with_a_corrupt_transcript_can_still_be_deleted(tmp_path: Path) -> None:
    """The UI offers removal for ``transcript_corrupt``; deleting must not read the ledger."""
    app, client, sid = _session_with_stored_transcript(tmp_path)

    def _corrupt(session_id: str) -> None:
        with typed_transcript_read(session_id):
            raise BlobFrameError("sess_x~m~0", 80, 10)

    app.state.messages._materialize = _corrupt
    assert client.get(f"/v1/sessions/{sid}/messages").status_code == 503

    response = client.delete(f"/v1/sessions/{sid}")

    assert response.status_code == 204
    assert app.state.sessions.get(sid) is None
