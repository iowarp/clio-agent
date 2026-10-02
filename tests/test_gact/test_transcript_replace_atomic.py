"""``transcript.file: false`` made safe: an atomic whole-transcript replace.

With the file copy off, clio-core's ``_events/m`` atom lane is the only transcript.
Replacing it whole (undo, rewind, fork, compact, import, clear, a divergent-lane
repair) writes a NEW lane generation and switches the lane's pointer in one put
(:mod:`clio_agent.arc.lane_generations`), so a failure halfway leaves the previous
transcript intact -- in clio-core, in the live server, and after a restart.

Everything here runs on a REAL clio-core store: ``ARCMemory(data_dir=...)`` opens this
test's clio-core namespace, and a "restart" is a second ``build_app`` over the same
session registry and the same namespace (a fresh ``ARCMemory``, cold reads). Failures
are injected at the store's own ``put``, refusing ONE named record.

SABOTAGE (recorded, run on real clio-core; each went RED, then was restored):

* ``replace_lane`` drops the current generation before writing the new one (the old
  drop-and-remint order): the second-chunk, pointer-switch, deferred and retire tests
  fail (the old transcript is gone, live and after the restart).
* ``current_base`` ignores the pointer (always generation 0): 7 of 9 fail -- every
  replaced transcript reads from the wrong generation.
* ``replace_lane`` skips the old generation's drop: the switch, pre-generation,
  retire and route tests fail (old chunks survive in clio-core).
* ``reload_resident_on_failure`` keeps the in-memory rows: the second-chunk,
  pointer-switch and deferred tests serve the never-stored rows live.
* ``_seed_from_atoms`` settles nothing: the file-off legacy-row test reads "".
* The file-off boot recovers legacy rows from ``messages/`` file mtimes (the old
  ``SessionStore`` behaviour): the legacy-row test sees the ledger stat calls.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from clio_agent.arc.lane_chunking import chunk_scope, lane_scopes
from clio_agent.arc.lane_generations import (
    LaneReplaceError,
    current_generation,
    generation_base,
    pointer_scope,
)
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.app import build_app
from clio_agent.gact.part_atoms import MESSAGE_PART_SCOPE, mint_message_part_atoms
from clio_agent.gact.session_store import _delete_session_messages, _replace_session_messages
from clio_agent.gact.transcript_projection import on_ledger_replaced
from clio_agent.gact.types import Message, Part, Tokens

from .test_post_messages import FakeClioAgent, _create_session

pytestmark = pytest.mark.usefixtures("host_agent_executor")


@pytest.fixture
def file_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The file copy off and a small lane chunk, so a transcript spans chunks."""

    monkeypatch.setenv("CLIO_TRANSCRIPT_FILE", "false")
    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "2")


class _CompactingAgent(FakeClioAgent):
    """The turn fake plus the two hooks the manual compaction calls."""

    def _run_chat_agent(self, question: str, _session_id: str) -> str:
        return "the summary"

    def _call_with_transient_provider_retries(self, _label: str, call: Callable[[], Any]) -> Any:
        return call()


def _app(tmp_path: Path, agent: Any = None) -> Any:
    """One server process: a fresh ARCMemory over this test's clio-core namespace."""

    return build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=agent if agent is not None else _CompactingAgent(answer="ok"),
        arc=ARCMemory(data_dir=str(tmp_path / "arc")),
    )


def _message(sid: str, message_id: str, text: str, role: str = "user") -> Message:
    return Message(
        id=message_id,
        turn_id=message_id,
        session_id=sid,
        role=role,
        created_at="2026-09-30T10:00:00+00:00",
        updated_at="2026-09-30T10:00:00+00:00",
        parts=[Part(id=f"part_{message_id}", type="text", text=text)],
        tokens=Tokens(),
        stop_reason="end_turn",
    )


def _rows(sid: str, count: int) -> list[Message]:
    return [
        _message(sid, f"msg_{i}", f"text {i}", role="user" if i % 2 else "assistant")
        for i in range(1, count + 1)
    ]


def _ids(client: TestClient, sid: str) -> list[str]:
    response = client.get(f"/v1/sessions/{sid}/messages")
    assert response.status_code == 200, response.text
    return [row["id"] for row in reversed(response.json()["messages"])]


def _seed(app: Any, client: TestClient, count: int) -> str:
    sid = _create_session(client)
    _replace_session_messages(app, sid, _rows(sid, count))
    app.state.sessions.update(sid, message_count=count)
    return sid


def _record(sid: str, scope: str) -> str:
    """The store record name of one scope (``SegmentStore._record_name``)."""

    return f"{sid}__{scope.replace('/', '~')}"


def _refuse_put(app: Any, monkeypatch: pytest.MonkeyPatch, record: str) -> list[str]:
    """Make this process's clio-core client refuse the put of ONE record.

    Patched on the app's own store instance: a restart builds a new one, unpatched.
    Returns the refused names (proof the failure fired).
    """

    store = app.state.arc._segments._store
    real_put = store.put
    refused: list[str] = []

    def put(kind: str, name: str, data: Any, **kwargs: Any) -> Any:
        if name == record:
            refused.append(name)
            raise RuntimeError(f"clio-core refused {name}")
        return real_put(kind, name, data, **kwargs)

    monkeypatch.setattr(store, "put", put)
    return refused


def _no_messages_dir(tmp_path: Path) -> None:
    assert not (tmp_path / "messages").exists()


# --------------------------------------------------------------------------- #
# A failed replace keeps the old transcript
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("file_off")
def test_off_a_replace_failing_at_its_second_chunk_keeps_the_old_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _app(tmp_path)
    with TestClient(first) as client:
        sid = _seed(first, client, 6)  # 6 atoms under capacity 2: three chunks
        before = _ids(client, sid)
        assert current_generation(first.state.arc._segments, sid, MESSAGE_PART_SCOPE) == 1
        # Undo writes generation 2; its SECOND chunk's put is refused.
        refused = _refuse_put(
            first, monkeypatch, _record(sid, chunk_scope(generation_base(MESSAGE_PART_SCOPE, 2), 2))
        )
        response = client.post(f"/v1/sessions/{sid}/undo", json={"count": 1})
        assert refused, "the injected failure never fired"
        assert response.status_code == 503, response.text
        error = response.json()["error"]
        assert error["error"] == "lane_replace_failed"
        assert error["recoverable"] is True
        assert "exactly as it was" in error["message"]
        assert error["details"]["stage"] == "write"
        assert error["details"]["committed"] is False
        # Live: the server serves the transcript clio-core kept, not the undo's rows.
        assert _ids(client, sid) == before
        assert len(before) == 6
        assert client.get("/v1/metrics").json()["messages"]["total"] == 6
        assert current_generation(first.state.arc._segments, sid, MESSAGE_PART_SCOPE) == 1

    second = _app(tmp_path)
    with TestClient(second) as client:
        assert _ids(client, sid) == before
        # The lane works after the failure: the same undo now succeeds.
        assert client.post(f"/v1/sessions/{sid}/undo", json={"count": 1}).status_code == 200
        assert _ids(client, sid) == before[:-1]
    third = _app(tmp_path)
    with TestClient(third) as client:
        assert _ids(client, sid) == before[:-1]
    _no_messages_dir(tmp_path)


@pytest.mark.usefixtures("file_off")
def test_off_a_replace_failing_at_the_pointer_switch_keeps_the_old_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _app(tmp_path)
    with TestClient(first) as client:
        sid = _seed(first, client, 4)
        before = _ids(client, sid)
        _refuse_put(first, monkeypatch, _record(sid, pointer_scope(MESSAGE_PART_SCOPE)))
        with pytest.raises(LaneReplaceError) as raised:
            _replace_session_messages(first, sid, _rows(sid, 2))
        assert raised.value.stage == "switch"
        assert raised.value.committed is False
        assert raised.value.to_dict()["error"] == "lane_replace_failed"
        assert _ids(client, sid) == before

    second = _app(tmp_path)
    with TestClient(second) as client:
        assert _ids(client, sid) == before
        assert client.get("/v1/metrics").json()["messages"]["total"] == 4
    _no_messages_dir(tmp_path)


@pytest.mark.usefixtures("file_off")
def test_off_a_failed_deferred_replace_drops_the_unstored_rows_from_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deferred seam (ledger now, atoms later off the loop) fails its atom job."""

    app = _app(tmp_path)
    with TestClient(app) as client:
        sid = _seed(app, client, 4)
        before = _ids(client, sid)
        new_rows = _rows(sid, 3)
        _replace_session_messages(app, sid, new_rows, atoms_minted=True)
        assert [m.id for m in app.state.messages[sid]] == [m.id for m in new_rows]
        _refuse_put(
            app, monkeypatch, _record(sid, chunk_scope(generation_base(MESSAGE_PART_SCOPE, 2), 2))
        )
        with pytest.raises(LaneReplaceError):
            on_ledger_replaced(app, sid, new_rows)
        assert app.state.messages.get_if_resident(sid) is None
        assert _ids(client, sid) == before
        assert client.get("/v1/metrics").json()["messages"]["total"] == 4


# --------------------------------------------------------------------------- #
# A successful replace
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("file_off")
def test_off_a_replace_switches_generation_and_drops_the_old_one(tmp_path: Path) -> None:
    first = _app(tmp_path)
    with TestClient(first) as client:
        sid = _seed(first, client, 5)
        segments = first.state.arc._segments
        old_base = generation_base(MESSAGE_PART_SCOPE, 1)
        old_chunks = lane_scopes(segments, sid, old_base)
        assert len(old_chunks) == 3
        _replace_session_messages(first, sid, _rows(sid, 2))
        assert current_generation(segments, sid, MESSAGE_PART_SCOPE) == 2
        assert _ids(client, sid) == ["msg_1", "msg_2"]
        for scope in old_chunks:
            assert segments.list_segments(sid, scope, include_tombstoned=True) == []

    second = _app(tmp_path)
    with TestClient(second) as client:
        assert _ids(client, sid) == ["msg_1", "msg_2"]
        fresh = second.state.arc._segments
        for scope in old_chunks:  # gone from clio-core, not only from one process
            assert fresh.list_segments(sid, scope, include_tombstoned=True) == []
    _no_messages_dir(tmp_path)


@pytest.mark.usefixtures("file_off")
def test_off_a_pre_generation_lane_is_replaced_from_generation_zero(tmp_path: Path) -> None:
    """A lane written before generations existed (the bare ``_events/m``) is generation 0."""

    first = _app(tmp_path)
    with TestClient(first) as client:
        sid = _create_session(client)
        for row in _rows(sid, 3):
            mint_message_part_atoms(first.state.arc, sid, row)
        segments = first.state.arc._segments
        assert current_generation(segments, sid, MESSAGE_PART_SCOPE) == 0
        legacy_chunks = lane_scopes(segments, sid, MESSAGE_PART_SCOPE)
        assert legacy_chunks[0] == MESSAGE_PART_SCOPE
        assert _ids(client, sid) == ["msg_1", "msg_2", "msg_3"]
        _replace_session_messages(first, sid, _rows(sid, 1))
        assert current_generation(segments, sid, MESSAGE_PART_SCOPE) == 1
        for scope in legacy_chunks:
            assert segments.list_segments(sid, scope, include_tombstoned=True) == []

    second = _app(tmp_path)
    with TestClient(second) as client:
        assert _ids(client, sid) == ["msg_1"]


@pytest.mark.usefixtures("file_off")
def test_off_a_failed_retire_is_committed_and_the_next_replace_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        sid = _seed(app, client, 2)
        segments = app.state.arc._segments
        store = segments._store
        real_delete = store.delete
        old_record = _record(sid, generation_base(MESSAGE_PART_SCOPE, 1))

        def delete(kind: str, name: str) -> Any:
            if name == old_record:
                raise RuntimeError(f"clio-core refused to delete {name}")
            return real_delete(kind, name)

        monkeypatch.setattr(store, "delete", delete)
        with pytest.raises(LaneReplaceError) as raised:
            _replace_session_messages(app, sid, _rows(sid, 1))
        assert raised.value.stage == "retire"
        assert raised.value.committed is True
        monkeypatch.setattr(store, "delete", real_delete)
        # Committed: the new transcript is what every reader sees.
        assert _ids(client, sid) == ["msg_1"]
        leftover = generation_base(MESSAGE_PART_SCOPE, 1)
        assert segments.list_segments(sid, leftover, include_tombstoned=True)
        _replace_session_messages(app, sid, _rows(sid, 3))  # clears generation 1 first
        assert segments.list_segments(sid, leftover, include_tombstoned=True) == []
        assert _ids(client, sid) == ["msg_1", "msg_2", "msg_3"]


# --------------------------------------------------------------------------- #
# The whole-replace routes under the file off, each through a restart
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("file_off")
def test_off_undo_rewind_fork_compact_import_and_clear_survive_restarts(
    tmp_path: Path,
) -> None:
    first = _app(tmp_path)
    with TestClient(first) as client:
        sid = _seed(first, client, 5)
        for row in _rows(sid, 5):  # the agent scope's plane, as a turn records it
            first.state.arc.append_segment(
                sid, "main", "user", {"text": row.parts[0].text}, turn_id=row.id
            )
        assert client.post(f"/v1/sessions/{sid}/undo", json={"count": 1}).status_code == 200
    expected = {sid: ["msg_1", "msg_2", "msg_3", "msg_4"]}

    def _restart_and_check() -> Any:
        app = _app(tmp_path)
        with TestClient(app) as client:
            for session_id, ids in expected.items():
                assert _ids(client, session_id) == ids, session_id
        return app

    app = _restart_and_check()
    with TestClient(app) as client:
        rewind = client.post(
            f"/v1/sessions/{sid}/rewind", json={"message_id": "msg_3", "include_target": False}
        )
        assert rewind.status_code == 200, rewind.text
    expected[sid] = ["msg_1", "msg_2", "msg_3"]

    app = _restart_and_check()
    with TestClient(app) as client:
        fork = client.post(f"/v1/sessions/{sid}/fork", json={"at_message_id": "msg_2"})
        assert fork.status_code == 201, fork.text
        fork_sid = fork.json()["id"]
    expected[fork_sid] = ["msg_1", "msg_2"]

    app = _restart_and_check()
    with TestClient(app) as client:
        compact = client.post(f"/v1/sessions/{sid}/compact", json={})
        assert compact.status_code == 200, compact.text
        compacted = _ids(client, sid)
        assert compacted[:3] == ["msg_1", "msg_2", "msg_3"] and len(compacted) == 4
        blob = client.get(f"/v1/sessions/{fork_sid}/export").json()
        imported = client.post("/v1/sessions/import", json=blob)
        assert imported.status_code == 200, imported.text
        import_sid = imported.json()["id"]
    expected[sid] = compacted
    expected[import_sid] = ["msg_1", "msg_2"]

    app = _restart_and_check()
    with TestClient(app) as client:
        _delete_session_messages(app, fork_sid)  # the /clear seam: the session stays
        app.state.sessions.update(fork_sid, message_count=0)
        segments = app.state.arc._segments
        assert current_generation(segments, fork_sid, MESSAGE_PART_SCOPE) == 0  # no pointer
        assert not segments.has_segments(fork_sid, pointer_scope(MESSAGE_PART_SCOPE))
        for generation in (0, 1, 2):
            base = generation_base(MESSAGE_PART_SCOPE, generation)
            assert segments.list_segments(fork_sid, base, include_tombstoned=True) == []
        assert client.delete(f"/v1/sessions/{import_sid}").status_code in (200, 204)
    expected[fork_sid] = []
    del expected[import_sid]

    app = _restart_and_check()
    assert app.state.sessions.get(import_sid) is None
    assert not app.state.arc._segments.list_segments(
        import_sid, generation_base(MESSAGE_PART_SCOPE, 1), include_tombstoned=True
    )
    _no_messages_dir(tmp_path)


# --------------------------------------------------------------------------- #
# A legacy session row's interaction time
# --------------------------------------------------------------------------- #


def _legacy_rows(tmp_path: Path, ids: list[str]) -> None:
    """Session rows persisted before ``last_interaction_at`` existed."""

    (tmp_path / "sessions.json").write_text(
        json.dumps(
            {
                sid: {
                    "id": sid,
                    "workspace_id": "ws_default",
                    "title": sid,
                    "created_at": "2026-08-01T00:00:00+00:00",
                    "updated_at": "2026-08-23T03:00:00+00:00",
                }
                for sid in ids
            }
        )
    )


@pytest.mark.usefixtures("file_off")
def test_off_a_legacy_row_takes_its_last_atom_message_time_never_messages_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _legacy_rows(tmp_path, ["sess_talked", "sess_silent"])
    seeding = ARCMemory(data_dir=str(tmp_path / "arc"))
    last = _message("sess_talked", "msg_last", "bye").model_copy(
        update={"updated_at": "2026-08-22T18:21:00+00:00"}
    )
    mint_message_part_atoms(seeding, "sess_talked", _message("sess_talked", "msg_first", "hi"))
    mint_message_part_atoms(seeding, "sess_talked", last)

    stat_calls: list[str] = []
    real_stat = Path.stat

    def stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.parent.name == "messages":  # a ledger file under messages/
            stat_calls.append(str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    first = _app(tmp_path)
    sessions = first.state.sessions
    assert sessions.get("sess_talked").last_interaction_at == "2026-08-22T18:21:00+00:00"
    assert sessions.get("sess_silent").last_interaction_at == "2026-08-01T00:00:00+00:00"
    assert sessions.sessions_without_interaction_time() == []
    with TestClient(first) as client:
        wire = client.get("/v1/sessions/sess_talked").json()
        assert wire["last_interaction_at"] == "2026-08-22T18:21:00+00:00"
    assert stat_calls == []
    _no_messages_dir(tmp_path)

    # Settled once and persisted: the restart reads the row's own value.
    stored = json.loads((tmp_path / "sessions.json").read_text())
    assert stored["sess_talked"]["last_interaction_at"] == "2026-08-22T18:21:00+00:00"
    second = _app(tmp_path)
    assert second.state.sessions.get("sess_silent").last_interaction_at == (
        "2026-08-01T00:00:00+00:00"
    )
    assert stat_calls == []
    _no_messages_dir(tmp_path)


def test_on_a_legacy_row_takes_its_message_file_mtime(tmp_path: Path) -> None:
    """File on (default): the pre-switch recovery, from the ``messages/`` file mtime."""

    _legacy_rows(tmp_path, ["sess_legacy", "sess_no_file"])
    messages_dir = tmp_path / "messages"
    messages_dir.mkdir()
    ledger = messages_dir / "sess_legacy.json"
    ledger.write_text("[]")
    observed = datetime(2026, 8, 22, 18, 21, tzinfo=timezone.utc).timestamp()
    os.utime(ledger, (observed, observed))

    app = _app(tmp_path)
    assert app.state.transcript_file is True
    sessions = app.state.sessions
    assert sessions.get("sess_legacy").last_interaction_at == "2026-08-22T18:21:00+00:00"
    assert sessions.get("sess_no_file").last_interaction_at == "2026-08-01T00:00:00+00:00"
