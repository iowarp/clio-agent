"""A store written by CLIO 0.9.5.1 or older: moved aside (never deleted), refused at boot,
named in the health row, the conversation list and the transcript 503.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.arc.chunk_records import ChunkFormatUnsupportedError, read_chunk
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import encode_segments
from clio_agent.arc.segment_errors import SegmentReadError
from clio_agent.arc.store_format import (
    ARC_STORE_FORMAT_OLD,
    ArcStoreFormatOldError,
    StoreFormat,
)
from clio_agent.gact import store_format_boot, store_migrate_command, store_reset_command
from clio_agent.gact.app import build_app
from clio_agent.gact.messages import LedgerReadError
from clio_agent.gact.semantic_events import SemanticEvent
from clio_agent.gact.session_records import SessionRecords, SessionRecordsNotReadyError
from clio_agent.gact.store_aside import (
    ARC_STORE_CUSTOM_CONFIG,
    StoreAsideRefusedError,
    StoreLocation,
    move_aside,
    planned_moves,
)
from clio_agent.gact.transcript_unreadable import STORE_FORMAT_OLD, typed_transcript_read
from clio_agent.runtime.status import IntegrationState

DAY = date(2026, 11, 6)


@pytest.fixture
def arc() -> Iterator[ARCMemory]:
    """A fresh ARCMemory on clio-core, in this test's namespace."""
    from clio_agent.arc.storage import make_arc_store

    memory = ARCMemory(store=make_arc_store(backend="cte"))
    try:
        yield memory
    finally:
        memory.clear_all()


def _home(tmp_path: Path, *, sessions: bool = True, layout: str = "default") -> StoreLocation:
    """A data home with a clio-core store (and the legacy session list) on disk."""
    data = tmp_path / "data"
    store = data / "cte" / "hosts" / "box" if layout == "default" else tmp_path / "mystore"
    store.mkdir(parents=True)
    (store / "cte.yaml").write_text("storage: here\n")
    (store / "storage.bin").write_bytes(b"\x00" * 64)
    legacy = None
    if sessions:
        legacy = tmp_path / "state" / "server" / "sessions.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text('{"sess_a": {}}')
    config = store / "cte.yaml" if layout != "custom" else tmp_path / "other" / "cte.yaml"
    return StoreLocation(
        store_dir=store,
        config=config,
        movable=layout != "custom",
        default_layout=layout == "default",
        data_dir=data,
        sessions_file=legacy,
    )


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


# ---- moving aside --------------------------------------------------------------------


def test_the_default_store_moves_out_of_the_cte_folder_unchanged(tmp_path: Path) -> None:
    """SABOTAGE: move it beside itself inside ``<data>/cte`` and the next start's one-time
    move of a pre-host store could adopt it; this test pins where it goes."""
    location = _home(tmp_path)
    before = _files(tmp_path)
    moves = move_aside(location, "0.10.0", DAY)
    data = tmp_path / "data"
    assert moves.store_to == data / "cte-before-0.10.0-2026-11-06" / "hosts" / "box"
    assert not location.store_dir.exists()
    assert moves.sessions_to == location.sessions_file.with_name(
        "sessions.json.before-0.10.0-2026-11-06"
    )
    # Nothing deleted, nothing changed: every byte is still there under its new name.
    after = _files(tmp_path)
    assert sorted(after.values()) == sorted(before.values())
    assert len(after) == len(before)


def test_a_second_move_the_same_day_never_overwrites(tmp_path: Path) -> None:
    location = _home(tmp_path, sessions=False)
    first = move_aside(location, "0.10.0", DAY)
    location.store_dir.mkdir(parents=True)
    (location.store_dir / "cte.yaml").write_text("second\n")
    second = move_aside(location, "0.10.0", DAY)
    assert second.store_to.parts[-3] == "cte-before-0.10.0-2026-11-06-2"
    assert (first.store_to / "cte.yaml").read_text() == "storage: here\n"
    assert (second.store_to / "cte.yaml").read_text() == "second\n"


def test_an_explicit_store_folder_moves_beside_itself(tmp_path: Path) -> None:
    location = _home(tmp_path, sessions=False, layout="explicit")
    moves = move_aside(location, "0.10.0", DAY)
    assert moves.store_to == tmp_path / "mystore-before-0.10.0-2026-11-06"
    assert (moves.store_to / "storage.bin").is_file()


def test_a_store_named_by_its_own_config_is_not_moved(tmp_path: Path) -> None:
    location = _home(tmp_path, layout="custom")
    with pytest.raises(StoreAsideRefusedError) as refused:
        planned_moves(location, "0.10.0", DAY)
    assert refused.value.error_type == ARC_STORE_CUSTOM_CONFIG
    assert location.store_dir.is_dir()


# ---- clio-agent store-check / reset-store ---------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreLocation:
    """The commands see a tmp data home, no other CLIO, and no clio-core daemon."""
    location = _home(tmp_path)
    monkeypatch.setattr(store_reset_command, "locate", lambda: location)
    monkeypatch.setattr(store_migrate_command, "_other_store_clients", lambda: [])
    monkeypatch.setattr(store_reset_command, "_no_daemon_left", lambda: True)
    monkeypatch.setattr(store_reset_command, "_version", lambda: "0.10.0")
    return location


def _verdict(monkeypatch: pytest.MonkeyPatch, verdict: str) -> None:
    reason = {"old": ARC_STORE_FORMAT_OLD, "unknown": "arc_store_format_unknown"}.get(verdict, "")
    found = StoreFormat(verdict, reason, error="unrecognized records" if reason else "")  # type: ignore[arg-type]
    monkeypatch.setattr(store_reset_command, "_inspect", lambda *_: found)


@pytest.mark.parametrize(("verdict", "code"), [("old", 5), ("unknown", 6), ("current", 0)])
def test_store_check_says_what_wrote_the_data(
    home: StoreLocation, monkeypatch: pytest.MonkeyPatch, verdict: str, code: int
) -> None:
    _verdict(monkeypatch, verdict)
    lines: list[str] = []
    status, payload = store_reset_command.check(lines.append)
    text = "\n".join(lines)
    assert status == code
    assert home.store_dir.is_dir()  # a check never moves anything
    if verdict == "old":
        assert "written by CLIO 0.9.5.1 or older" in text
        assert "cte-before-0.10.0-" in text and "Nothing is deleted" in text
        assert payload["move"]["store_from"] == str(home.store_dir)
    elif verdict == "unknown":
        assert "Could not tell which CLIO version wrote the data" in text
    else:
        assert "nothing to do" in text


def test_reset_store_moves_the_old_data_after_a_yes(
    home: StoreLocation, monkeypatch: pytest.MonkeyPatch
) -> None:
    _verdict(monkeypatch, "old")
    lines: list[str] = []
    status, payload = store_reset_command.reset(yes=False, emit=lines.append, ask=lambda _q: "y")
    assert status == 0, lines
    assert payload["status"] == "moved"
    assert not home.store_dir.exists() and Path(payload["store_to"]).is_dir()
    assert any("Nothing was deleted" in line for line in lines)


def test_reset_store_leaves_everything_on_a_no_or_without_a_terminal(
    home: StoreLocation, monkeypatch: pytest.MonkeyPatch
) -> None:
    _verdict(monkeypatch, "old")
    status, _ = store_reset_command.reset(yes=False, emit=lambda _l: None, ask=lambda _q: "n")
    assert status == 1 and home.store_dir.is_dir()
    lines: list[str] = []
    status, _ = store_reset_command.reset(yes=False, emit=lines.append, ask=None)
    assert status == 1 and home.store_dir.is_dir()
    assert any("reset-store --yes" in line for line in lines)


def test_reset_store_refuses_while_clio_runs(
    home: StoreLocation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: drop the running-CLIO refusal and the store moves under a live server."""
    monkeypatch.setattr(store_migrate_command, "_other_store_clients", lambda: [4242])
    lines: list[str] = []
    status, payload = store_reset_command.reset(yes=True, emit=lines.append)
    assert status == 3 and payload["reason"] == "store_in_use"
    assert home.store_dir.is_dir()
    assert "CLIO is running (process 4242)" in lines[0] and "clio stop" in lines[0]


def test_reset_store_with_yes_moves_without_reading_the_store(
    home: StoreLocation, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_reset_command, "_inspect", lambda *_: pytest.fail("read"))
    status, payload = store_reset_command.reset(yes=True, emit=lambda _l: None)
    assert status == 0 and Path(payload["store_to"]).is_dir()
    assert Path(payload["sessions_to"]).is_file()


# ---- boot, health, conversation list ----------------------------------------------------


def _old_store(arc: ARCMemory) -> Any:
    arc.record_semantic_event(
        SemanticEvent(event_type="lm.call", session_id="sess_old", trace_id="t", span_id="s")
    )
    raw = arc._segments._store
    name = arc._segments._record_name("sess_old", "_events")
    parsed = read_chunk(raw, "segments", name)
    assert parsed is not None
    raw.put("segments", name, encode_segments(parsed.segments))  # as v0.9.5.1 stored it
    return raw


def _boot_app(tmp_path: Path) -> Any:
    records = SessionRecords()
    records.expect_store()
    sessions = SimpleNamespace(index=records)
    return SimpleNamespace(state=SimpleNamespace(sessions=sessions)), records


def test_boot_refuses_an_old_store_before_reading_it_and_says_why(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: return the store whatever the verdict in ``checked_boot_store`` and the
    boot opens (and writes into) the old data; this test fails."""
    raw = _old_store(arc)
    location = _home(tmp_path)
    monkeypatch.setattr(store_format_boot, "locate", lambda: location)
    made: list[str] = []

    def make(**kwargs: Any) -> Any:
        made.append(kwargs["data_dir"])
        return raw

    monkeypatch.setattr("clio_agent.arc.storage.make_arc_store", make)
    app, records = _boot_app(tmp_path)
    with pytest.raises(ArcStoreFormatOldError) as refused:
        store_format_boot.checked_boot_store(app, "/data/arc")
    message = refused.value.message
    assert "written by CLIO 0.9.5.1 or older" in message
    assert "clio reset-store" in message and "nothing is deleted" in message
    assert str(location.store_dir) in message
    # Decided once: a retried attach answers at once, without attaching again.
    with pytest.raises(ArcStoreFormatOldError):
        store_format_boot.checked_boot_store(app, "/data/arc")
    assert made == ["/data/arc"]
    # The conversation list says the same, typed, instead of "still attaching".
    with pytest.raises(SessionRecordsNotReadyError) as listing:
        records._ready()
    assert listing.value.reason == ARC_STORE_FORMAT_OLD
    assert "0.9.5.1 or older" in listing.value.message
    [row] = store_format_boot.health_rows(app)
    assert row.name == "arc_store_format" and row.state is IntegrationState.UNAVAILABLE
    assert row.required and row.details["reason"] == ARC_STORE_FORMAT_OLD
    assert "cannot open it" in row.summary and "clio reset-store" in row.next_action


def test_boot_attaches_a_current_store_and_reports_it_ready(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arc.record_semantic_event(
        SemanticEvent(event_type="lm.call", session_id="sess_new", trace_id="t", span_id="s")
    )
    raw = arc._segments._store
    monkeypatch.setattr("clio_agent.arc.storage.make_arc_store", lambda **_: raw)
    monkeypatch.setattr(store_format_boot, "locate", lambda: _home(tmp_path))
    app, records = _boot_app(tmp_path)
    assert store_format_boot.checked_boot_store(app, "/data/arc") is raw
    [row] = store_format_boot.health_rows(app)
    assert row.state is IntegrationState.READY
    assert records._refused is None


def test_migrate_refuses_an_old_store_typed(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _old_store(arc)
    monkeypatch.setattr("clio_agent.gact.store_aside.locate", lambda: _home(tmp_path))
    with pytest.raises(ArcStoreFormatOldError):
        store_migrate_command._refuse_old_store(raw)
    store_migrate_command._refuse_old_store(_fresh_store(arc))


def _fresh_store(arc: ARCMemory) -> Any:
    raw = arc._segments._store
    raw.clear()
    return raw


# ---- the conversation 503 -----------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ChunkFormatUnsupportedError("sess_x~_events"),
        # A reader that wrapped it keeps it as the cause.
        SegmentReadError("sess_x", "_events"),
    ],
)
def test_an_old_format_conversation_is_typed_as_such(error: Exception) -> None:
    """SABOTAGE: drop the ChunkFormatUnsupportedError branch and the cause is
    ``transcript_corrupt``; this test fails."""
    if isinstance(error, SegmentReadError) and not isinstance(error, ChunkFormatUnsupportedError):
        error.__cause__ = ChunkFormatUnsupportedError("sess_x~_events")
    with pytest.raises(LedgerReadError) as info, typed_transcript_read("sess_x"):
        raise error
    assert info.value.cause == STORE_FORMAT_OLD == ARC_STORE_FORMAT_OLD


def test_the_503_tells_the_person_what_to_do(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={}).json()["id"]
    app.state.sessions.get(sid).message_count = 2

    def _old(session_id: str) -> None:
        with typed_transcript_read(session_id):
            raise ChunkFormatUnsupportedError("sess_x~m~0")

    app.state.messages._materialize = _old
    response = client.get(f"/v1/sessions/{sid}/messages")
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["error"] == "messages_unavailable"
    assert error["details"]["cause"] == ARC_STORE_FORMAT_OLD
    assert error["recoverable"] is False
    assert "saved by CLIO 0.9.5.1 or older" in error["message"]
    assert "clio reset-store" in error["message"] and "not deleted" in error["message"]
    assert "retry-after" not in response.headers
