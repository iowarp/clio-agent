"""#737 S5 — the transcript projection: persistence assembles from atoms.

These are the LIVE-server proofs for the atoms read path (design §4.2 step 5): a
real gact turn persists nothing but atoms, and ``GET /messages`` /
``app.state.messages`` re-materialize the transcript from the canonical log so
``reload == live``. Single regime since v0.8.0: the ``CLIO_TRANSCRIPT_PROJECTION``
flag, the legacy messages-store regime, and the per-session pin were deleted —
an app with an ARC substrate is ALWAYS on atoms, and the deleted flag env must
be inert.

Covered:

* **reload == live on a real turn** — drive a turn, evict the resident ledger, reload
  from atoms, diff EMPTY under the S0 §4.1.A persistence normalizer.
* **the deleted flag env is inert** (sabotage twin for the v0.8.0 deletion).
* **the final_message byte-copy dies under atoms** (the embed gate; the embed
  survives only in the no-substrate structural case).
* **transcript ops touch the projection, NEVER ARC memory** (sabotage-c): replace /
  delete re-materialize / drop the ``_events/m`` lane while the ARC working-set scope
  is untouched — the frozen ``gact_visible_transcript_only`` semantics.
* **backfill raises a typed failure, no silent skip** (§3.4 / design (d)).

SABOTAGE (recorded, run manually):

* (b) make the SSE spine and persistence read DIFFERENT sources — e.g. have
  ``assemble_session_messages`` drop the last message: ``test_reload_equals_live_real_turn``
  goes RED (reloaded ledger shorter than the live/streamed one) — the persistence
  normalizer's length divergence names it. Restore the assembly.
* (c) make ``on_ledger_deleted`` erase an ARC working-set scope instead of the
  ``_events/m`` lane: ``test_delete_drops_atom_lane_leaves_arc_memory`` goes RED (the
  working-set render is emptied) — the ``gact_visible_transcript_only`` guard.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.app import build_app
from clio_agent.gact.session_store import (
    _delete_session_messages,
    _replace_session_messages,
)
from clio_agent.gact.transcript_projection import (
    TranscriptBackfillError,
    atoms_active,
    final_message_embed,
    materialize_ledger,
)
from clio_agent.gact.types import Message, Part, Tokens
from tests.equivalence import normalizers as N
from tests.test_gact.conftest import settle_turn_slot
from tests.test_gact.test_post_messages import FakeClioAgent


def _run_turn(client: TestClient, sid: str, text: str = "how many stations?") -> None:
    """Drive one turn to settle (the FakeClioAgent has no LM)."""

    ack = client.post(
        f"/v1/sessions/{sid}/messages", json={"parts": [{"type": "text", "text": text}]}
    )
    assert ack.status_code == 200, ack.text
    # 30s + loud failure: the old 10s window silently BROKE out on timeout and
    # let assertions run against a still-running turn (the CI flake signature:
    # missing assistant / session_busy on the next post). Post-S4b turns build a
    # real blueprint module, so slow runners need the headroom.
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if client.get(f"/v1/sessions/{sid}").json()["status"] != "running":
            # #1334: finalize runs on the turn executor; the status flips a few ms
            # before the turn slot clears, and the next POST needs the slot.
            settle_turn_slot(client, sid, timeout=max(1.0, deadline - time.monotonic()))
            return
        time.sleep(0.05)
    raise TimeoutError(f"turn on session {sid!r} did not settle within 30s")


def _build(tmp_path: Path) -> tuple[Any, ARCMemory]:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    agent = FakeClioAgent(answer="five dense stations near LA")
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=agent, arc=arc)
    return app, arc


# --------------------------------------------------------------------------- #
# reload == live on a real turn (atoms regime)
# --------------------------------------------------------------------------- #


def test_reload_equals_live_real_turn(tmp_path: Path) -> None:
    """Evicting + reloading a turn reproduces it from atoms (the only regime)."""

    app, _arc = _build(tmp_path)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "s5"}).json()["id"]
        _run_turn(client, sid)

        live = [m.model_dump(exclude_none=True) for m in app.state.messages.get(sid, [])]
        assert [m["role"] for m in live] == ["user", "assistant"]
        assert atoms_active(app) is True

        # Evict the resident copy so the next access rehydrates from the canonical log.
        app.state.messages.clear()
        reloaded = [m.model_dump(exclude_none=True) for m in app.state.messages.get(sid, [])]

        report = N.diff_persistence(live, reloaded)
        assert report.empty, f"reload != live:\n{report.pretty()}"

        # And GET /messages (newest-first) serves the same atoms-assembled projection.
        app.state.messages.clear()
        served = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        assert [m["role"] for m in served] == ["assistant", "user"]  # newest-first
        assert len(served) == 2


def test_reload_equals_live_multiturn(tmp_path: Path) -> None:
    """reload == live holds across MULTIPLE turns (append accumulation on the lane)."""

    app, _arc = _build(tmp_path)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "s5multi"}).json()["id"]
        _run_turn(client, sid, "first")
        _run_turn(client, sid, "second")

        live = [m.model_dump(exclude_none=True) for m in app.state.messages.get(sid, [])]
        assert [m["role"] for m in live] == ["user", "assistant", "user", "assistant"]

        app.state.messages.clear()
        reloaded = [m.model_dump(exclude_none=True) for m in app.state.messages.get(sid, [])]
        report = N.diff_persistence(live, reloaded)
        assert report.empty, f"reload != live (multiturn):\n{report.pretty()}"


def test_concurrent_cold_readers_backfill_one_exact_atom_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent first reads cannot duplicate or interleave retained messages."""

    from clio_agent.gact import transcript_projection as projection

    app, arc = _build(tmp_path)
    sid = "sess_concurrent_backfill"
    ledger = [
        Message(
            id="msg_user_concurrent",
            turn_id="msg_user_concurrent",
            session_id=sid,
            role="user",
            created_at="2026-09-11T10:00:00+00:00",
            updated_at="2026-09-11T10:00:00+00:00",
            parts=[Part(id="part_user_concurrent", type="text", text="first")],
        ),
        Message(
            id="msg_asst_concurrent",
            turn_id="msg_user_concurrent",
            session_id=sid,
            role="assistant",
            created_at="2026-09-11T10:00:01+00:00",
            updated_at="2026-09-11T10:00:01+00:00",
            parts=[Part(id="part_asst_concurrent", type="text", text="second")],
        ),
    ]
    app.state.message_store.replace_session(sid, ledger)
    expected = [message.model_dump(exclude_none=True) for message in ledger]

    original_mint = projection.mint_message_part_atoms

    def slow_mint(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.01)
        return original_mint(*args, **kwargs)

    monkeypatch.setattr(projection, "mint_message_part_atoms", slow_mint)
    start = threading.Barrier(8)

    def cold_read() -> list[dict[str, Any]]:
        start.wait(timeout=2.0)
        materialized = projection.materialize_ledger(app, sid)
        assert materialized is not None
        return [message.model_dump(exclude_none=True) for message in materialized]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _index: cold_read(), range(8)))

    assert results == [expected] * 8
    assert [
        message.model_dump(exclude_none=True)
        for message in projection.assemble_session_messages(arc, sid)
    ] == expected


def _time_materialize_ledger(app: Any, sid: str, *, use_loop: bool) -> tuple[Any, float]:
    """Call ``materialize_ledger`` either from inside a running loop (an async
    route handler calling it synchronously) or from a plain worker thread (no
    running loop), timing the call either way."""

    from clio_agent.gact import transcript_projection as projection

    outcome: dict[str, Any] = {}

    def _call() -> None:
        start = time.monotonic()
        outcome["messages"] = projection.materialize_ledger(app, sid)
        outcome["elapsed"] = time.monotonic() - start

    if use_loop:

        async def _coro() -> None:
            _call()

        asyncio.run(_coro())
    else:
        worker = threading.Thread(target=_call)
        worker.start()
        worker.join(timeout=2.0)
    return outcome["messages"], outcome["elapsed"]


@pytest.mark.parametrize("use_loop", [True, False], ids=["loop_registered", "worker_no_loop"])
def test_materialize_ledger_serves_retained_ledger_without_waiting_when_lane_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_loop: bool
) -> None:
    """#1334 review round (Fable, F1): the lane lock guards only the has-atoms
    DECISION, never a read waiting on an in-flight append or mint. A busy lane
    (another thread mid-append, exactly what ``part_atoms._append_segment_raw``
    holds around every store RPC) must be served from the retained ledger
    IMMEDIATELY, on the calling thread's own loop or a plain worker thread alike
    -- a blocking acquire here would let a GET /messages on the server loop wait
    behind a store write, the exact #1334 stall class."""

    from clio_agent.gact import transcript_projection as projection

    app, arc = _build(tmp_path)
    sid = f"sess_lane_busy_{use_loop}"
    ledger = [
        Message(
            id="msg_user_busy",
            turn_id="msg_user_busy",
            session_id=sid,
            role="user",
            created_at="2026-09-12T10:00:00+00:00",
            updated_at="2026-09-12T10:00:00+00:00",
            parts=[Part(id="part_user_busy", type="text", text="hello")],
        )
    ]
    app.state.message_store.replace_session(sid, ledger)
    expected = [message.model_dump(exclude_none=True) for message in ledger]

    audited: list[dict[str, Any]] = []
    monkeypatch.setattr(
        projection,
        "stream_audit",
        lambda stage, **fields: audited.append({"stage": stage, **fields}),
    )

    lane_lock = arc._segments._lock_for(sid, projection.MESSAGE_PART_SCOPE)
    holder_ready = threading.Event()

    def _hold_lock_briefly() -> None:
        lane_lock.acquire()
        holder_ready.set()
        time.sleep(0.4)  # simulates an in-flight append/mint under the same lock
        lane_lock.release()

    holder = threading.Thread(target=_hold_lock_briefly, daemon=True)
    holder.start()
    assert holder_ready.wait(timeout=2.0)
    try:
        messages, elapsed = _time_materialize_ledger(app, sid, use_loop=use_loop)
    finally:
        holder.join(timeout=2.0)

    assert elapsed < 0.2, f"materialize_ledger waited on the busy lane ({elapsed:.3f}s)"
    assert messages is not None
    assert [message.model_dump(exclude_none=True) for message in messages] == expected
    assert any(
        row["stage"] == "transcript.lane_busy_served_retained" and row.get("session_id") == sid
        for row in audited
    )


def test_restart_reconciliation_preserves_streamed_partial_from_the_atom_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1334 review round (Fable, F2): a crash mid-turn already sealed a partial
    assistant answer onto the atom lane. The restart reconciliation must not
    destroy it with the deleted-vehicle synthetic empty row (owner rule:
    deleting a vehicle keeps the feature) -- it overlays the restart's
    stop_reason/error_info onto the lane's OWN typed-incomplete trailing
    message, through materialize_ledger (the real read path), not assemble."""

    from clio_agent.gact import transcript_projection as projection
    from clio_agent.gact.part_atom_minter import open_turn_minter, transcript_sink
    from clio_agent.gact.session_store import _reconcile_restart_interrupted_sessions
    from clio_agent.gact.transcript import EventBusTranscriptPublisher, TurnTranscript

    app, arc = _build(tmp_path)
    sid = app.state.sessions.create(workspace_id="ws_default", title="crashed").id
    turn_id = "msg_user_crash"

    user_message = Message(
        id=turn_id,
        turn_id=turn_id,
        session_id=sid,
        role="user",
        created_at="2026-09-12T09:00:00+00:00",
        updated_at="2026-09-12T09:00:00+00:00",
        parts=[Part(id="part_user_crash", type="text", text="how many stations?")],
    )
    projection.mint_message_part_atoms(arc, sid, user_message)
    # File: only the user message -- the assistant turn never reached finalize.
    app.state.message_store.replace_session(sid, [user_message])

    # Lane: the user message's atoms plus a SEALED partial assistant text part
    # with no trailing envelope atom -- the exact crash-before-finalize shape
    # `_incomplete_envelope` (part_atoms.py) types as stop_reason="incomplete".
    minter = open_turn_minter(app, sid, turn_id)
    transcript = TurnTranscript(
        session_id=sid,
        turn_id=turn_id,
        publisher=EventBusTranscriptPublisher(app.state.bus, sid),
        sink=transcript_sink(app, sid),
    )
    transcript.append_text_delta("main", "answer", "five dense stations near")
    transcript.close_open_text()
    assert minter.drain(timeout=5.0)
    minter.close()  # never finalized: no envelope atom ever lands

    app.state.sessions.update(sid, status="running")

    errors_logged: list[Any] = []
    monkeypatch.setattr(
        projection.logger, "error", lambda *args, **kwargs: errors_logged.append(args)
    )

    _reconcile_restart_interrupted_sessions(app)

    materialized = projection.materialize_ledger(app, sid)
    assert materialized is not None
    assert [m.role for m in materialized] == ["user", "assistant"]
    asst = materialized[1]
    assert [p.text for p in asst.parts if p.type == "text"] == ["five dense stations near"]
    assert asst.stop_reason == "error"
    assert asst.error_info is not None
    assert asst.error_info.error == "server_restart_interrupted"
    assert asst.metadata.get("transcript_incomplete")

    # The divergence-repair path (materialize_ledger's own logger.error) never
    # fires: the reconciliation kept file and lane in agreement by construction.
    assert not errors_logged, errors_logged

    on_disk = app.state.message_store.load_session(sid)
    assert [m.role for m in on_disk] == ["user", "assistant"]
    assert on_disk[1].stop_reason == "error"
    assert on_disk[1].metadata.get("transcript_incomplete")


# --------------------------------------------------------------------------- #
# single regime (v0.8.0) — the deleted flag env is inert, no pin is written
# --------------------------------------------------------------------------- #


def test_deleted_projection_flag_env_is_inert(tmp_path: Path, monkeypatch) -> None:
    """SABOTAGE twin: CLIO_TRANSCRIPT_PROJECTION=0 (the deleted opt-out) must not
    resurrect the legacy messages-store regime — atoms stay the only read path,
    and no per-session regime pin is written anymore."""

    monkeypatch.setenv("CLIO_TRANSCRIPT_PROJECTION", "0")
    app, _arc = _build(tmp_path)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "inert"}).json()["id"]
        _run_turn(client, sid)
        assert atoms_active(app) is True

        # No regime pin lands in session metadata (the pin machinery is deleted).
        record = app.state.sessions.get(sid)
        assert "transcript_regime" not in (record.metadata or {})

        # And the transcript still rehydrates from the canonical log.
        live = [m.id for m in app.state.messages.get(sid, [])]
        app.state.messages.clear()
        reloaded = [m.id for m in app.state.messages.get(sid, [])]
        assert reloaded == live and live, "atoms must remain the read path"


# --------------------------------------------------------------------------- #
# the final_message byte-copy gate (design (b)/(e))
# --------------------------------------------------------------------------- #


def _assistant_msg() -> Message:
    return Message(
        id="msg_asst_x",
        turn_id="msg_user_x",
        session_id="sess_fm",
        role="assistant",
        created_at="2026-07-12T10:00:00+00:00",
        updated_at="2026-07-12T10:00:00+00:00",
        parts=[Part(id="p1", type="text", text="hi")],
        tokens=Tokens(input=1, output=1),
    )


class _FakeApp:
    def __init__(self, arc: Any) -> None:
        self.state = type("S", (), {"arc": arc})()


def test_final_message_embed_dropped_under_atoms(tmp_path: Path) -> None:
    """The byte-copy dies under atoms; it survives ONLY in the no-substrate
    structural case (no ARC on the app), where the embed remains the only
    trace-derivable copy (design §4.2 step 5)."""

    msg = _assistant_msg()
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    assert final_message_embed(_FakeApp(arc), "sess_fm", msg) == {}
    embed = final_message_embed(_FakeApp(None), "sess_fm", msg)  # no substrate
    assert embed["final_message"]["id"] == msg.id


# --------------------------------------------------------------------------- #
# transcript ops touch the projection ONLY — never ARC memory (sabotage-c)
# --------------------------------------------------------------------------- #


def test_replace_rematerializes_atom_lane_leaves_arc_memory(tmp_path: Path) -> None:
    """Undo/rewind/fork (replace) re-materialize atoms; ARC memory is untouched.

    Compaction no longer replaces the ledger (#1339: it APPENDS a checkpoint via
    ``_append_session_message`` like any other row), so it is not one of these
    replace-shaped callers any more.
    """

    app, arc = _build(tmp_path)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "replace"}).json()["id"]
        _run_turn(client, sid)

        # Seed an ARC memory scope for this session (stand-in for ARC memory).
        arc._segments.append(sid, "agentX", kind="text", content={"text": "arc memory"})
        before = arc._segments.list_segments(sid, "agentX")
        assert before, "expected an ARC-memory segment to guard"

        # Replace the transcript with a single trimmed message (an undo-shaped mutation).
        trimmed = [
            Message(
                id="msg_user_trim",
                turn_id="msg_user_trim",
                session_id=sid,
                role="user",
                created_at="2026-07-12T09:00:00+00:00",
                updated_at="2026-07-12T09:00:00+00:00",
                parts=[Part(id="pu", type="text", text="only me")],
            )
        ]
        _replace_session_messages(app, sid, trimmed)

        app.state.messages.clear()
        reloaded = [m.id for m in app.state.messages.get(sid, [])]
        assert reloaded == ["msg_user_trim"], "atom lane must reflect the replaced ledger"

        # The ARC memory scope is UNTOUCHED (gact_visible_transcript_only).
        assert arc._segments.list_segments(sid, "agentX") == before


def test_delete_drops_atom_lane_leaves_arc_memory(tmp_path: Path) -> None:
    """DELETE drops the transcript lane; the ARC working-set scope survives (sabotage-c)."""

    app, arc = _build(tmp_path)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "del"}).json()["id"]
        _run_turn(client, sid)

        arc._segments.append(sid, "agentX", kind="text", content={"text": "arc memory"})
        before = arc._segments.list_segments(sid, "agentX")
        assert before

        # The real delete seam drops BOTH the store copy and the atom lane (via
        # on_ledger_deleted); the transcript is then gone from every projection.
        _delete_session_messages(app, sid)
        assert materialize_ledger(app, sid) in (None, [])
        # ARC memory is intact (gact_visible_transcript_only — sabotage-c).
        assert arc._segments.list_segments(sid, "agentX") == before


def test_delete_finishes_when_unreachable_arc_cannot_drop_atom_lane(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A completed session delete is not reported as failed by orphan cleanup."""

    from clio_agent.gact import transcript_projection

    class _MessageStore:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def delete_session(self, session_id: str) -> None:
            self.deleted.append(session_id)

    store = _MessageStore()
    app = SimpleNamespace(
        state=SimpleNamespace(
            messages={"sess_dead": []},
            message_store=store,
            metrics_counters=None,
        )
    )
    monkeypatch.setattr(
        transcript_projection,
        "on_ledger_deleted",
        lambda _app, _sid: (_ for _ in ()).throw(RuntimeError("GetBlob operation failed")),
    )

    with caplog.at_level("WARNING"):
        _delete_session_messages(app, "sess_dead")

    assert "sess_dead" not in app.state.messages
    assert store.deleted == ["sess_dead"]
    assert "GetBlob operation failed" in caplog.text


# --------------------------------------------------------------------------- #
# backfill — typed failure, no silent skip (§3.4 / design (d))
# --------------------------------------------------------------------------- #


def test_backfill_raises_typed_failure_no_silent_skip(tmp_path: Path) -> None:
    """A ledger whose mint fails surfaces a typed ``TranscriptBackfillError`` (no skip)."""

    from clio_agent.gact import transcript_projection as TP

    arc = ARCMemory(data_dir=str(tmp_path / "arc"))

    class _Boom:
        id = "msg_boom"

    with pytest.raises(TranscriptBackfillError) as excinfo:
        TP.mint_atoms_from_ledger(arc, "sess_boom", [_Boom()])  # type: ignore[list-item]
    assert excinfo.value.reason == "transcript_backfill_failed"
    assert excinfo.value.message_id == "msg_boom"
