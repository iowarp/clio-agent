"""#1172: an unreadable stored transcript is a typed, recoverable error, never a bare 500.

The chain under test: clio-core ``GetBlob`` raises a bare ``RuntimeError`` for a blob
whose size it reports -> the store raises ``BlobReadError`` -> ``materialize_ledger``
raises ``LedgerReadError`` (so the resident set records ``rehydrate_failed``) ->
``GET /v1/sessions/{sid}/messages`` answers ``503 messages_unavailable``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.arc import rpc_liveness, storage
from clio_agent.arc.blob_frame import BlobReadError
from clio_agent.gact import transcript_projection
from clio_agent.gact.app import build_app
from clio_agent.gact.messages import LedgerReadError


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

    assert info.value.details == {
        "tag": "ns/_events",
        "name": "sess_x~m~0",
        "cause": "GetBlob operation failed",
    }
    assert isinstance(info.value.__cause__, RuntimeError)


def test_materialize_ledger_raises_ledger_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _unreadable(_app: object, _sid: str) -> None:
        raise BlobReadError("ns/_events", "sess_x~m~0", RuntimeError("GetBlob operation failed"))

    monkeypatch.setattr(transcript_projection, "_materialize_ledger", _unreadable)

    with pytest.raises(LedgerReadError) as info:
        transcript_projection.materialize_ledger(SimpleNamespace(), "sess_x")

    assert isinstance(info.value.__cause__, BlobReadError)


def test_get_messages_is_a_typed_recoverable_error_not_500_or_empty(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={}).json()["id"]
    # The session has a stored transcript, so the route takes the materialize path.
    app.state.sessions.get(sid).message_count = 2
    audits: list[dict[str, Any]] = []
    app.state.messages._audit = audits.append

    def _unreadable(session_id: str) -> None:
        raise LedgerReadError(f"session {session_id} transcript is unreadable: GetBlob failed")

    app.state.messages._materialize = _unreadable

    response = client.get(f"/v1/sessions/{sid}/messages")

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["error"] == "messages_unavailable"
    assert error["recoverable"] is True
    assert error["details"]["session_id"] == sid
    assert "GetBlob failed" in error["details"]["reason"]
    assert any(a["reason"] == "rehydrate_failed" and a["session_id"] == sid for a in audits)
