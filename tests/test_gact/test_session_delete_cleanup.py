"""Deleting a session never leaves its clio-core transcript behind silently.

``DELETE /v1/sessions/{id}`` (and ``/clear``) erases the session's clio-core data
FIRST -- the ``_events/m`` transcript payload generations and the ``_events/s`` op
lane. Its empty visibility marker remains so retained task input cannot restore
the deleted transcript.
When clio-core cannot finish, the request fails typed (``transcript_erase_failed``,
a retryable 503) BEFORE the session row, the in-memory ledger or the file copy is
touched: the session stays, and repeating the request finishes the erase (every
step is idempotent). Before this, the failure was logged as a warning after the row
was already gone, leaving unreachable clio-core data behind.

Everything runs on a REAL clio-core store (``ARCMemory(data_dir=...)``); a "restart"
is a second ``build_app`` over the same registry and namespace. Failures are injected
at the store's own ``put`` / ``delete``, refusing ONE named record.

Historical pre-replacement sabotage (not acceptance of the current regressions):

* the old swallow restored in ``_delete_session_messages`` (catch, log a warning)
  with the row deleted first: 12 of 14 fail -- the delete answers 204, the session is
  gone and its lane records remain in clio-core.
* ``on_ledger_deleted`` moved back AFTER the ledger/file removal (still raising):
  the file-on test loses its ``messages/`` copy on the failed delete.
* the route deletes the row before the erase (still raising): the session is gone
  after the 503 and the retry answers 404 with the lane still in clio-core.
* ``drop_lane`` drops chunks lowest first (the old order): the ``retire`` cases leave
  chunk 3 of the old generation orphaned after the retry (chunk 1 went first, so the
  retry's dense walk stops at the hole).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.arc.lane_chunking import chunk_scope
from clio_agent.arc.lane_generations import (
    GENERATION_KIND,
    current_generation,
    generation_base,
    pointer_scope,
)
from clio_agent.gact.part_atoms import MESSAGE_PART_SCOPE
from clio_agent.gact.workflow_state.state_merge import STATE_MERGE_KIND, STATE_MERGE_SCOPE

from .test_transcript_replace_atomic import _app, _ids, _record, _refuse_put, _seed

pytestmark = pytest.mark.usefixtures("host_agent_executor")

ROWS = 6  # 6 atoms under chunk capacity 2: three chunks per generation


@pytest.fixture(params=[False, True], ids=["file_off", "file_on"])
def file_mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    """Both ``transcript.file`` settings, with a small lane chunk."""

    monkeypatch.setenv("CLIO_TRANSCRIPT_FILE", "true" if request.param else "false")
    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "2")
    return bool(request.param)


def _refuse_delete(app: Any, monkeypatch: pytest.MonkeyPatch, record: str) -> list[str]:
    """Make this process's clio-core client refuse the delete of ONE record."""

    store = app.state.arc._segments._store
    real_delete = store.delete
    refused: list[str] = []

    def delete(kind: str, name: str) -> Any:
        if name == record:
            refused.append(name)
            raise RuntimeError(f"clio-core refused to delete {name}")
        return real_delete(kind, name)

    monkeypatch.setattr(store, "delete", delete)
    return refused


def _lane_records(app: Any, sid: str) -> list[str]:
    """Every clio-core record of the session's transcript and op lanes (any generation)."""

    store = app.state.arc._segments._store
    names = []
    for scope in (MESSAGE_PART_SCOPE, STATE_MERGE_SCOPE):
        names += list(store.scan_names("segments", _record(sid, scope)))
    return sorted(names)


def _seed_with_ops(app: Any, client: TestClient) -> str:
    """A session with a 3-chunk transcript (generation 1) and an ``_events/s`` op."""

    sid = _seed(app, client, ROWS)
    app.state.arc._segments.append(sid, STATE_MERGE_SCOPE, kind=STATE_MERGE_KIND, content={})
    assert current_generation(app.state.arc._segments, sid, MESSAGE_PART_SCOPE) == 1
    assert _lane_records(app, sid)
    return sid


def _assert_erase_failed(response: Any, cause: str = "LaneReplaceError") -> None:
    assert response.status_code == 503, response.text
    assert response.headers.get("retry-after") == "1"
    error = response.json()["error"]
    assert error["error"] == "transcript_erase_failed"
    assert error["recoverable"] is True
    assert "the session was kept" in error["message"]
    assert error["details"]["cause"] == cause


def _assert_only_visibility_marker(app: Any, sid: str) -> str:
    """Require all payload/op records gone and one valid empty visibility boundary."""
    segments = app.state.arc._segments
    scope = pointer_scope(MESSAGE_PART_SCOPE)
    assert _lane_records(app, sid) == [_record(sid, scope)]
    pointers = segments.list_segments(sid, scope, include_tombstoned=True)
    assert len(pointers) == 1 and pointers[0].kind == GENERATION_KIND
    marker = pointers[0].content
    visibility = marker.get("transcript_visibility")
    assert isinstance(visibility, str) and visibility
    generation = current_generation(segments, sid, MESSAGE_PART_SCOPE)
    assert generation > 0 and marker["generation"] == generation
    assert (
        segments.list_segments(
            sid, generation_base(MESSAGE_PART_SCOPE, generation), include_tombstoned=True
        )
        == []
    )
    return visibility


def _assert_nothing_left(tmp_path: Path, sid: str, visibility: str) -> None:
    """After restart: deleted row/file/payloads stay gone; the same visibility survives."""

    app = _app(tmp_path)
    with TestClient(app) as client:
        assert app.state.sessions.get(sid) is None
        assert client.get(f"/v1/sessions/{sid}").status_code == 404
        assert _assert_only_visibility_marker(app, sid) == visibility
        segments = app.state.arc._segments
        assert segments.has_segments(sid, pointer_scope(MESSAGE_PART_SCOPE))
        for generation in (0, 1, 2, 3):
            base = generation_base(MESSAGE_PART_SCOPE, generation)
            assert segments.list_segments(sid, base, include_tombstoned=True) == []
    assert not (tmp_path / "messages" / f"{sid}.json").exists()


# Each case refuses one record the erase of a generation-1 lane touches, naming the
# stage it fails at: the pointer switch (nothing changed yet), the old generation's
# second chunk (switched; old chunks remain), or the op lane. The visibility
# pointer is intentionally retained; its no-delete guarantee is tested separately.
_FAILURES = {
    "switch": ("put", lambda sid: _record(sid, pointer_scope(MESSAGE_PART_SCOPE))),
    "retire": (
        "delete",
        lambda sid: _record(sid, chunk_scope(generation_base(MESSAGE_PART_SCOPE, 1), 2)),
    ),
    "op_lane": ("delete", lambda sid: _record(sid, STATE_MERGE_SCOPE)),
}


@pytest.mark.parametrize("failure", sorted(_FAILURES))
def test_a_failed_clio_core_erase_fails_the_delete_typed_and_a_retry_finishes_it(
    tmp_path: Path, file_mode: bool, failure: str
) -> None:
    op, record_of = _FAILURES[failure]
    first = _app(tmp_path)
    with TestClient(first) as client:
        sid = _seed_with_ops(first, client)
        before = _ids(client, sid)
        assert len(before) == ROWS
        refuse = _refuse_put if op == "put" else _refuse_delete
        with pytest.MonkeyPatch.context() as injected:
            refused = refuse(first, injected, record_of(sid))
            response = client.delete(f"/v1/sessions/{sid}")
        assert refused, "the injected failure never fired"
        _assert_erase_failed(
            response, "RuntimeError" if failure == "op_lane" else "LaneReplaceError"
        )
        # The session is kept, reported as kept, and still listed.
        assert first.state.sessions.get(sid) is not None
        assert client.get(f"/v1/sessions/{sid}").status_code == 200
        if file_mode:
            # The file copy is untouched: nothing but clio-core was changed.
            assert (tmp_path / "messages" / f"{sid}.json").exists()
        if failure == "switch":
            # Nothing switched: clio-core still holds the whole transcript.
            assert _ids(client, sid) == before
        else:
            # The lane already reads empty; the server serves what clio-core holds.
            assert _ids(client, sid) == []
        # Not silently left: the retry (same process, store healthy again) removes it.
        retry = client.delete(f"/v1/sessions/{sid}")
        assert retry.status_code == 204, retry.text
        visibility = _assert_only_visibility_marker(first, sid)
    _assert_nothing_left(tmp_path, sid, visibility)


def test_a_failed_erase_keeps_the_session_across_a_restart_and_the_retry_there_finishes(
    tmp_path: Path, file_mode: bool
) -> None:
    first = _app(tmp_path)
    with TestClient(first) as client:
        sid = _seed_with_ops(first, client)
        before = _ids(client, sid)
        with pytest.MonkeyPatch.context() as injected:
            _refuse_put(first, injected, _record(sid, pointer_scope(MESSAGE_PART_SCOPE)))
            _assert_erase_failed(client.delete(f"/v1/sessions/{sid}"))

    second = _app(tmp_path)  # a restart: an unpatched store over the same namespace
    with TestClient(second) as client:
        assert second.state.sessions.get(sid) is not None
        assert _ids(client, sid) == before
        assert client.delete(f"/v1/sessions/{sid}").status_code == 204
        visibility = _assert_only_visibility_marker(second, sid)
    _assert_nothing_left(tmp_path, sid, visibility)


def test_a_successful_delete_removes_payloads_and_preserves_visibility(
    tmp_path: Path, file_mode: bool
) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        sid = _seed_with_ops(app, client)
        # A second replace moves the lane to generation 2 (generation 1 retired).
        client.post(f"/v1/sessions/{sid}/undo", json={"count": 1}).raise_for_status()
        assert current_generation(app.state.arc._segments, sid, MESSAGE_PART_SCOPE) == 2
        assert client.delete(f"/v1/sessions/{sid}").status_code == 204
        visibility = _assert_only_visibility_marker(app, sid)
    _assert_nothing_left(tmp_path, sid, visibility)


def test_delete_never_attempts_to_remove_the_visibility_boundary(
    tmp_path: Path, file_mode: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusing pointer deletion cannot block cleanup because that marker must remain."""
    app = _app(tmp_path)
    with TestClient(app) as client:
        sid = _seed_with_ops(app, client)
        with monkeypatch.context() as injected:
            refused = _refuse_delete(app, injected, _record(sid, pointer_scope(MESSAGE_PART_SCOPE)))
            response = client.delete(f"/v1/sessions/{sid}")
        assert response.status_code == 204, response.text
        assert refused == []
        visibility = _assert_only_visibility_marker(app, sid)
    _assert_nothing_left(tmp_path, sid, visibility)


def test_a_failed_clear_is_typed_and_its_retry_clears(tmp_path: Path, file_mode: bool) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        sid = _seed_with_ops(app, client)
        before = _ids(client, sid)
        with pytest.MonkeyPatch.context() as injected:
            refused = _refuse_put(app, injected, _record(sid, pointer_scope(MESSAGE_PART_SCOPE)))
            response = client.post(f"/v1/sessions/{sid}/commands/clear")
        assert refused, "the injected failure never fired"
        _assert_erase_failed(response)
        assert _ids(client, sid) == before
        assert app.state.sessions.get(sid).message_count == ROWS

        retry = client.post(f"/v1/sessions/{sid}/commands/clear")
        assert retry.status_code == 200, retry.text
        assert not set(_ids(client, sid)) & set(before)  # only the "cleared" notice
        old_generation = _record(sid, generation_base(MESSAGE_PART_SCOPE, 1))
        assert not [name for name in _lane_records(app, sid) if name.startswith(old_generation)]
        assert _record(sid, STATE_MERGE_SCOPE) not in _lane_records(app, sid)
        assert app.state.sessions.get(sid) is not None
