"""The ``transcript.file`` switch: the messages/ file copy on (default) or off.

Off, clio-core is the only transcript store: no ``messages/`` file is written or
read, the index is the session registry, and every read assembles the atoms. The
"restart" here is a second ``build_app`` over the same session registry and the
same clio-core store (a fresh ``ARCMemory`` on one shared in-memory ARC store --
clio-core is a separate daemon and outlives the gact process).

SABOTAGE (recorded, run manually; each went RED, then was restored):

* ``materialize_from_atoms`` returns ``[]`` instead of assembling the atoms: the
  restart, undo/rewind/fork/compact, reconciliation and busy-lane tests fail.
* ``RegistryTranscriptIndex`` answers nothing (empty ids, ``has_session`` False): the
  restart index and the empty-session tests fail.
* ``forget_unminted_on_failure`` keeps the failed row: the ghost-row test fails.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from clio_agent.arc import history_mode
from clio_agent.arc.live import _MemoryStore
from clio_agent.arc.memory import ARCMemory
from clio_agent.errors import ConfigError
from clio_agent.gact import transcript_projection as projection
from clio_agent.gact.app import build_app
from clio_agent.gact.part_atoms import MESSAGE_PART_SCOPE, mint_message_part_atoms
from clio_agent.gact.session_store import _replace_session_messages
from clio_agent.gact.transcript_file import (
    LANE_BUSY_REASON,
    TranscriptFileRequiredError,
    TranscriptNotReadyError,
)
from clio_agent.gact.types import Message, Part, Tokens

from .conftest import complete_turn
from .test_post_messages import FakeClioAgent, _create_session

pytestmark = pytest.mark.usefixtures("host_agent_executor")


class _CompactingAgent(FakeClioAgent):
    """The turn fake plus the two hooks the manual compaction calls."""

    def _run_chat_agent(self, question: str, _session_id: str) -> str:
        return "the summary"

    def _call_with_transient_provider_retries(self, _label: str, call: Callable[[], Any]) -> Any:
        return call()


@pytest.fixture
def file_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_TRANSCRIPT_FILE", "false")


@pytest.fixture
def core_store() -> _MemoryStore:
    """One clio-core store shared by every app of a test (it outlives a restart)."""

    return _MemoryStore()


def _app(tmp_path: Path, store: _MemoryStore, name: str, agent: Any = None) -> Any:
    arc = ARCMemory(data_dir=str(tmp_path / f"arc_{name}"), store=store)
    return build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=agent if agent is not None else FakeClioAgent(answer="five dense stations"),
        arc=arc,
    )


def _wire(client: TestClient, sid: str) -> list[dict[str, Any]]:
    response = client.get(f"/v1/sessions/{sid}/messages")
    assert response.status_code == 200, response.text
    return list(reversed(response.json()["messages"]))  # chronological


def _texts(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [
        (row["role"], "".join(p.get("text", "") for p in row["parts"] if p["type"] == "text"))
        for row in rows
    ]


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


def _no_message_files(tmp_path: Path) -> None:
    messages_dir = tmp_path / "messages"
    assert not messages_dir.exists() or not any(messages_dir.iterdir()), list(
        messages_dir.iterdir()
    )


# --------------------------------------------------------------------------- #
# On (default): the pre-switch behaviour.
# --------------------------------------------------------------------------- #


def test_default_keeps_the_file_copy(tmp_path: Path, core_store: _MemoryStore) -> None:
    app = _app(tmp_path, core_store, "on")
    assert app.state.transcript_file is True
    with TestClient(app) as client:
        sid = _create_session(client)
        complete_turn(client, sid, "hello")
        on_disk = app.state.message_store.load_session(sid)
        assert on_disk is not None
        assert [m.role for m in on_disk] == ["user", "assistant"]
    assert (tmp_path / "messages" / f"{sid}.json").exists()


def test_a_non_boolean_value_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, core_store: _MemoryStore
) -> None:
    monkeypatch.setenv("CLIO_TRANSCRIPT_FILE", "sometimes")
    with pytest.raises(ConfigError, match="transcript.file must be true or false"):
        _app(tmp_path, core_store, "bad")


# --------------------------------------------------------------------------- #
# Off: clio-core is the only store.
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("file_off")
def test_off_a_turn_survives_a_restart_with_no_message_files(
    tmp_path: Path, core_store: _MemoryStore
) -> None:
    first = _app(tmp_path, core_store, "first")
    assert first.state.transcript_file is False
    assert first.state.message_store is None
    with TestClient(first) as client:
        sid = _create_session(client)
        complete_turn(client, sid, "how many stations?")
        live = _wire(client, sid)
    assert _texts(live) == [("user", "how many stations?"), ("assistant", "five dense stations")]
    _no_message_files(tmp_path)

    second = _app(tmp_path, core_store, "second")
    messages = second.state.messages
    assert messages.resident_count == 0  # nothing is resident after boot
    assert sid in messages
    assert sid in list(messages)
    assert len(messages) == 1
    with TestClient(second) as client:
        reloaded = _wire(client, sid)  # a cold read: assembled from the atoms only
        metrics = client.get("/v1/metrics").json()
    assert reloaded == live
    assert metrics["messages"]["total"] == 2
    assert metrics["messages"]["by_role"] == {"user": 1, "assistant": 1}
    _no_message_files(tmp_path)


@pytest.mark.usefixtures("file_off")
def test_off_an_empty_session_reads_as_empty_not_unknown(
    tmp_path: Path, core_store: _MemoryStore
) -> None:
    first = _app(tmp_path, core_store, "first")
    with TestClient(first) as client:
        sid = _create_session(client)
        assert _wire(client, sid) == []

    second = _app(tmp_path, core_store, "second")
    assert projection.materialize_ledger(second, sid) == []
    assert projection.materialize_ledger(second, "sess_never_created") is None
    assert second.state.transcript_index.has_session(sid)
    assert sid in second.state.messages
    assert second.state.messages[sid] == []
    with TestClient(second) as client:
        assert _wire(client, sid) == []
    _no_message_files(tmp_path)


@pytest.mark.usefixtures("file_off")
def test_off_undo_rewind_fork_and_compact_persist_through_a_restart(
    tmp_path: Path, core_store: _MemoryStore
) -> None:
    agent = _CompactingAgent(answer="five dense stations")
    first = _app(tmp_path, core_store, "first", agent=agent)
    with TestClient(first) as client:
        sid = _create_session(client)
        rows = [
            _message(sid, "msg_1", "one"),
            _message(sid, "msg_2", "two", role="assistant"),
            _message(sid, "msg_3", "three"),
            _message(sid, "msg_4", "four", role="assistant"),
            _message(sid, "msg_5", "five"),
        ]
        _replace_session_messages(first, sid, rows)
        first.state.sessions.update(sid, message_count=len(rows))
        for row in rows:  # the agent scope's plane, as a turn records it
            first.state.arc.append_segment(
                sid, "main", "user", {"text": row.parts[0].text}, turn_id=row.id
            )

        undo = client.post(f"/v1/sessions/{sid}/undo", json={"count": 1})
        assert undo.status_code == 200, undo.text
        rewind = client.post(
            f"/v1/sessions/{sid}/rewind", json={"message_id": "msg_3", "include_target": False}
        )
        assert rewind.status_code == 200, rewind.text
        fork = client.post(f"/v1/sessions/{sid}/fork", json={"at_message_id": "msg_2"})
        assert fork.status_code == 201, fork.text
        fork_sid = fork.json()["id"]
        compact = client.post(f"/v1/sessions/{sid}/compact", json={})
        assert compact.status_code == 200, compact.text
        live = _wire(client, sid)
        live_fork = _wire(client, fork_sid)
    assert [row["id"] for row in live][:3] == ["msg_1", "msg_2", "msg_3"]
    assert len(live) == 4 and live[3]["parts"][0]["type"] == "compaction"
    assert [row["id"] for row in live_fork] == ["msg_1", "msg_2"]
    _no_message_files(tmp_path)

    second = _app(tmp_path, core_store, "second", agent=_CompactingAgent())
    with TestClient(second) as client:
        assert _wire(client, sid) == live
        assert _wire(client, fork_sid) == live_fork
    _no_message_files(tmp_path)


@pytest.mark.usefixtures("file_off")
def test_off_restart_reconciles_an_interrupted_session_from_the_atoms(
    tmp_path: Path, core_store: _MemoryStore
) -> None:
    first = _app(tmp_path, core_store, "first")
    sid = first.state.sessions.create(workspace_id="ws_default", title="crashed").id
    mint_message_part_atoms(first.state.arc, sid, _message(sid, "msg_user_crash", "still there?"))
    first.state.sessions.update(sid, status="running", message_count=1)

    second = _app(tmp_path, core_store, "second")
    session = second.state.sessions.get(sid)
    assert session.status == "error"
    assert session.message_count == 2
    with TestClient(second) as client:
        rows = _wire(client, sid)
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[1]["error_info"]["error"] == "server_restart_interrupted"
    _no_message_files(tmp_path)


@pytest.mark.usefixtures("file_off")
def test_off_a_busy_lane_waits_off_the_loop(tmp_path: Path, core_store: _MemoryStore) -> None:
    """Off the loop the cold read waits for the writer; never serves an empty lane."""

    app = _app(tmp_path, core_store, "busy")
    sid = app.state.sessions.create(workspace_id="ws_default", title="busy").id
    mint_message_part_atoms(app.state.arc, sid, _message(sid, "msg_busy", "hello"))
    lane_lock = app.state.arc._segments._lock_for(sid, MESSAGE_PART_SCOPE)
    held = threading.Event()
    release = threading.Event()

    def _writer() -> None:
        with lane_lock:
            held.set()
            release.wait()

    writer = threading.Thread(target=_writer, daemon=True)
    writer.start()
    assert held.wait(5.0)
    result: list[Any] = []
    reader = threading.Thread(
        target=lambda: result.append(projection.materialize_ledger(app, sid)), daemon=True
    )
    reader.start()
    time.sleep(0.2)
    assert result == []  # still waiting on the writer, not served early
    release.set()
    reader.join(10.0)
    writer.join(10.0)
    assert [m.id for m in result[0]] == ["msg_busy"]


@pytest.mark.usefixtures("file_off")
def test_off_a_busy_lane_on_a_loop_is_a_typed_retryable_error(
    tmp_path: Path, core_store: _MemoryStore
) -> None:
    app = _app(tmp_path, core_store, "busy_loop")
    sid = app.state.sessions.create(workspace_id="ws_default", title="busy").id
    mint_message_part_atoms(app.state.arc, sid, _message(sid, "msg_busy", "hello"))
    lane_lock = app.state.arc._segments._lock_for(sid, MESSAGE_PART_SCOPE)
    held = threading.Event()
    release = threading.Event()

    def _writer() -> None:
        with lane_lock:
            held.set()
            release.wait()

    writer = threading.Thread(target=_writer, daemon=True)
    writer.start()
    assert held.wait(5.0)

    async def _read_on_loop() -> Any:
        return projection.materialize_ledger(app, sid)

    try:
        with pytest.raises(TranscriptNotReadyError) as raised:
            asyncio.run(_read_on_loop())
    finally:
        release.set()
        writer.join(10.0)
    assert raised.value.reason == LANE_BUSY_REASON
    assert raised.value.to_dict()["error"] == "transcript_not_ready"


@pytest.mark.usefixtures("file_off")
def test_off_the_not_ready_error_is_a_retryable_503(
    tmp_path: Path, core_store: _MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path, core_store, "503")
    with TestClient(app) as client:
        sid = _create_session(client)
        complete_turn(client, sid, "hello")
        app.state.messages.discard(sid)  # evicted: the next read is cold
        app.state.transcript_boot_ready.clear()  # as before the ARC attached
        metrics = client.get("/v1/metrics")
        assert metrics.status_code == 503, metrics.text
        assert metrics.json()["error"]["error"] == "transcript_not_ready"
        assert metrics.json()["error"]["recoverable"] is True
        assert metrics.headers["Retry-After"] == "1"


@pytest.mark.usefixtures("file_off")
def test_off_a_failed_checkpoint_mint_is_typed_and_leaves_no_ghost_row(
    tmp_path: Path, core_store: _MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path, core_store, "mint_fail", agent=_CompactingAgent())
    with TestClient(app) as client:
        sid = _create_session(client)
        rows = [_message(sid, "msg_1", "one"), _message(sid, "msg_2", "two", role="assistant")]
        _replace_session_messages(app, sid, rows)
        app.state.sessions.update(sid, message_count=len(rows))
        app.state.arc.append_segment(sid, "main", "user", {"text": "one"}, turn_id="msg_1")

        def _store_down(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("clio-core store write failed")

        monkeypatch.setattr(projection, "mint_message_part_atoms", _store_down)
        response = client.post(f"/v1/sessions/{sid}/compact", json={})
        assert response.status_code == 500, response.text
        assert response.json()["error"]["error"] == "memory_update_failed"
        assert [m.id for m in app.state.messages[sid]] == ["msg_1", "msg_2"]


@pytest.mark.history_mode
def test_off_in_history_mode_is_a_typed_boot_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_TRANSCRIPT_FILE", "false")
    monkeypatch.setattr(history_mode, "binding_present", lambda: False)
    with pytest.raises(TranscriptFileRequiredError) as raised:
        build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent(), arc=None)
    message = str(raised.value)
    assert "History mode keeps the transcript only in a file" in message
    assert "transcript.file: true" in message
    assert raised.value.to_dict()["error"] == "config_error"
    _no_message_files(tmp_path)


@pytest.mark.history_mode
def test_on_in_history_mode_still_boots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(history_mode, "binding_present", lambda: False)
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent(), arc=None)
    assert app.state.transcript_file is True
    assert app.state.message_store is not None


@pytest.mark.usefixtures("file_off")
def test_off_the_atom_boot_runs_when_the_process_arc_attaches(
    tmp_path: Path, core_store: _MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real server binds the ARC after build: reconcile + metrics seed run then."""

    from clio_agent.arc import storage
    from clio_agent.gact import server_boot

    first = _app(tmp_path, core_store, "first")
    sid = first.state.sessions.create(workspace_id="ws_default", title="crashed").id
    mint_message_part_atoms(first.state.arc, sid, _message(sid, "msg_user", "still there?"))
    first.state.sessions.update(sid, status="running", message_count=1)

    monkeypatch.chdir(tmp_path)  # the construction door's ARC data dir is cwd-relative
    monkeypatch.setattr(storage, "make_arc_store", lambda **_kwargs: core_store)
    monkeypatch.setattr(server_boot, "_PROCESS_ARC_LOCK", threading.Lock())
    second = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent(), arc=None)
    if second.state.arc is None:  # nothing attached clio-core during the build
        assert not second.state.transcript_boot_ready.is_set()
        assert second.state.sessions.get(sid).status == "running"
        with TestClient(second) as client:
            pending = client.get("/v1/metrics")
            assert pending.status_code == 503, pending.text
            assert pending.json()["error"]["details"]["reason"] == "transcript_store_not_attached"
        server_boot.process_arc(second)  # the one construction door (as the boot task runs it)
    assert second.state.transcript_boot_ready.is_set()
    assert second.state.sessions.get(sid).status == "error"
    with TestClient(second) as client:
        rows = _wire(client, sid)
        metrics = client.get("/v1/metrics").json()
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert metrics["messages"]["total"] == 2
    _no_message_files(tmp_path)
