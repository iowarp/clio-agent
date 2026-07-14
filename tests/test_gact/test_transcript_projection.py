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

import time
from pathlib import Path
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
from tests.test_gact.test_post_messages import FakeClioAgent


def _run_turn(client: TestClient, sid: str, text: str = "how many stations?") -> None:
    """Drive one turn to settle (the FakeClioAgent has no LM)."""

    ack = client.post(
        f"/v1/sessions/{sid}/messages", json={"parts": [{"type": "text", "text": text}]}
    )
    assert ack.status_code == 200, ack.text
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if client.get(f"/v1/sessions/{sid}").json()["status"] != "running":
            break
        time.sleep(0.05)


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
    """Undo/rewind/fork/compact (replace) re-materialize atoms; ARC memory is untouched."""

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
