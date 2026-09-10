"""Streaming-native transcript persistence (#1337): the part-atom minter.

Covers the seal ladder :mod:`clio_agent.gact.part_atom_minter` owns:

* :meth:`TurnTranscript._seal_locked` hands a part to the minter's non-blocking
  ``sink`` the moment it becomes FINAL (a closed non-whitespace text part, a
  tool_call once its result lands) -- never for a whitespace-only or discarded
  part, and the sink call never touches the ARC store under the transcript lock.
* :meth:`PartAtomMinter.mint_remainder` mints exactly what the live seals did
  NOT land (unsealed or since-mutated parts) plus the trailing envelope atom, at
  finalize / pause / failed-finalize.
* The schema v2 ``"atom"`` envelope-authority profile: lean sealed part atoms
  carry no message-level fields at all; the envelope atom is their SOLE
  authority. A message whose envelope never landed reproduces as a TYPED
  incomplete message (``stop_reason="incomplete"``), never a silently complete
  one -- and the v1 ``"inline"`` profile (every atom self-describing) keeps
  reproducing byte-identically.
* :func:`~clio_agent.gact.part_atoms.group_atoms_in_order` -- ONE grouping rule
  for both profiles, keyed by message id, closed by the envelope atom for the
  atom profile and by the v1 ordinal boundary otherwise.

Style: direct-construction unit tests in the manner of ``test_ledger_guard.py``
/ ``test_goal_judge_offloop.py`` (a bare ``SimpleNamespace`` fake app + a real
in-memory :class:`~clio_agent.arc.memory.ARCMemory`), plus a couple of
``TestClient``-driven turns (the style of ``test_finalize_error_envelope.py`` /
``test_paused_transcript_persistence.py``) for the two scenarios that only a
real turn exercises: a finalize-region crash, and a pause.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.arc.live import _MemoryStore
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import part_atom_minter
from clio_agent.gact.app import _append_session_message, build_app
from clio_agent.gact.events import EventBus
from clio_agent.gact.part_atom_minter import (
    PART_ATOM_SEAL_FAILED,
    close_turn_minter,
    open_turn_minter,
    persist_finalized_message,
    transcript_sink,
    turn_minter,
)
from clio_agent.gact.part_atoms import (
    MESSAGE_PART_SCOPE,
    TRANSCRIPT_INCOMPLETE_REASON,
    build_envelope_atom,
    build_sealed_part_atom,
    group_atoms_in_order,
    message_stub,
    reproduce_message_wire,
)
from clio_agent.gact.transcript import EventBusTranscriptPublisher, TurnTranscript
from clio_agent.gact.transcript_projection import assemble_session_messages
from clio_agent.gact.turn_state import TurnState
from clio_agent.gact.types import Message, Part, Tokens
from clio_agent.gact.user_question_pause import maybe_pause_for_user
from tests.equivalence import normalizers as N

from .test_post_messages import FakeClioAgent

# #948 S4b: default sessions run the blueprint react ``main``; route it to the
# ``build_app(agent=...)`` host fake used by the two TestClient-driven tests below.
pytestmark = pytest.mark.usefixtures("host_agent_executor")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class _RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def publish(self, event_type: str, payload: Any) -> None:
        self.events.append((event_type, dict(payload)))


def _arc(tmp_path: Path, name: str = "arc") -> ARCMemory:
    return ARCMemory(data_dir=str(tmp_path / name), store=_MemoryStore())


def _fake_app(arc: ARCMemory | None) -> SimpleNamespace:
    # ``bus`` is real (not a stub): the package-wide autouse ``_live_equals_reload_property``
    # fixture wraps ``gact_app._append_session_message`` for every test in this package and
    # reads ``app.state.bus._history`` on every call -- a fake app must carry one too.
    return SimpleNamespace(state=SimpleNamespace(arc=arc, messages={}, bus=EventBus()))


def _transcript(sid: str, turn_id: str, app: SimpleNamespace) -> TurnTranscript:
    return TurnTranscript(
        session_id=sid,
        turn_id=turn_id,
        publisher=_RecordingPublisher(),
        sink=transcript_sink(app, sid),
    )


def _tool_call(call_id: str, part_id: str) -> Part:
    return Part(
        id=part_id,
        type="tool_call",
        agent_id="data",
        call_id=call_id,
        tool_name="fs_read_file",
        input={"path": "README.md"},
        metadata={"stream_source": "live"},
    )


def _tool_result(call_id: str, part_id: str) -> Part:
    return Part(
        id=part_id,
        type="tool_result",
        call_id=call_id,
        tool_name="fs_read_file",
        content=[Part(id=f"{part_id}_raw", type="text", text="ok")],
    )


def _atoms_on_lane(arc: ARCMemory, sid: str) -> list[dict[str, Any]]:
    return [
        seg.content
        for seg in arc._segments.list_segments(sid, MESSAGE_PART_SCOPE, include_tombstoned=False)
    ]


def _bare_atom(message_id: str, role: str, authority: str, index: int, part_id: str = "") -> dict:
    """The minimal fields :func:`group_atoms_in_order` reads -- for pure grouping tests."""

    return {
        "message_id": message_id,
        "atom_role": role,
        "envelope_authority": authority,
        "part_index": index,
        "part_id": part_id,
    }


# --------------------------------------------------------------------------- #
# 1. A closed streamed text part seals exactly one atom, full text
# --------------------------------------------------------------------------- #


def test_streamed_text_part_seals_exactly_one_atom_with_the_full_text(tmp_path: Path) -> None:
    arc = _arc(tmp_path)
    app = _fake_app(arc)
    minter = open_turn_minter(app, "sess1", "turn1")
    transcript = _transcript("sess1", "turn1", app)

    transcript.append_text_delta("main", "answer", "Hel")
    transcript.append_text_delta("main", "answer", "lo ")
    transcript.append_text_delta("main", "answer", "world")
    transcript.close_open_text()

    assert minter.drain(timeout=5.0)
    atoms = _atoms_on_lane(arc, "sess1")
    assert len(atoms) == 1
    assert atoms[0]["atom_role"] == "part"
    assert atoms[0]["envelope_authority"] == "atom"
    assert atoms[0]["seal_source"] == "live"
    assert atoms[0]["part"]["text"] == "Hello world"
    minter.close()


# --------------------------------------------------------------------------- #
# 2. Whitespace-only + discarded-retry parts never seal
# --------------------------------------------------------------------------- #


def test_whitespace_only_and_discarded_parts_never_seal(tmp_path: Path) -> None:
    arc = _arc(tmp_path)
    app = _fake_app(arc)
    minter = open_turn_minter(app, "sess2", "turn2")
    transcript = _transcript("sess2", "turn2", app)

    # Whitespace-only: dropped from the ledger at close, so it was never eligible.
    transcript.append_text_delta("main", "answer", "   ")
    transcript.close_open_text()

    # Discarded retry: the abandoned attempt never closes, so it never seals either.
    transcript.append_text_delta("main", "reasoning", "abandoned attempt")
    assert transcript.discard_open_text() is True

    assert minter.drain(timeout=5.0)
    assert _atoms_on_lane(arc, "sess2") == []
    minter.close()


# --------------------------------------------------------------------------- #
# 3. Seal-time sequence matches finalize()'s, with a dropped part in the middle
# --------------------------------------------------------------------------- #


def test_seal_sequence_matches_finalize_with_a_dropped_part_in_the_middle(tmp_path: Path) -> None:
    arc = _arc(tmp_path)
    app = _fake_app(arc)
    minter = open_turn_minter(app, "sess3", "turn3")
    transcript = _transcript("sess3", "turn3", app)

    transcript.append_text_delta("main", "reasoning", "first")
    transcript.close_open_text()  # seals live: sequence 1
    transcript.append_text_delta("main", "answer", "   ")
    transcript.close_open_text()  # whitespace-only: dropped, never seals
    transcript.append_text_delta("main", "answer", "second")
    transcript.close_open_text()  # seals live: sequence 2 (the drop never counted)

    assert minter.drain(timeout=5.0)
    sealed_sequence = {a["part_id"]: a["part"]["sequence"] for a in _atoms_on_lane(arc, "sess3")}
    assert len(sealed_sequence) == 2

    frozen = transcript.finalize()
    assert [p.text for p in frozen] == ["first", "second"]
    for part in frozen:
        assert sealed_sequence[part.id] == part.sequence  # idempotent re-stamp, unchanged
    assert [p.sequence for p in frozen] == [1, 2]
    minter.close()


# --------------------------------------------------------------------------- #
# 4. A tool_call seals when its tool_result lands; a presentation delta rides it
# --------------------------------------------------------------------------- #


def test_tool_call_seals_on_its_result_carrying_a_presentation_delta(tmp_path: Path) -> None:
    arc = _arc(tmp_path)
    app = _fake_app(arc)
    minter = open_turn_minter(app, "sess4", "turn4")
    transcript = _transcript("sess4", "turn4", app)

    call_part = transcript.append_part(_tool_call("call_x", "tc_x"))
    assert call_part is not None
    assert minter.drain(timeout=5.0)
    assert _atoms_on_lane(arc, "sess4") == []  # not final yet: no result

    # A presentation delta mutates the SAME live object in place, before the result.
    call_part.presentation = {"summary": "reading README.md", "blocks": []}

    transcript.append_part(_tool_result("call_x", "tr_x"))  # seals the tool_call now
    assert minter.drain(timeout=5.0)

    atoms = _atoms_on_lane(arc, "sess4")
    assert len(atoms) == 1  # only the tool_call sealed; its result did not (no match)
    sealed = atoms[0]
    assert sealed["part_id"] == "tc_x"
    assert sealed["part"]["presentation"] == {"summary": "reading README.md", "blocks": []}
    minter.close()


# --------------------------------------------------------------------------- #
# 5. The seal never touches the store under the transcript lock
# --------------------------------------------------------------------------- #


def test_seal_never_touches_the_store_under_the_transcript_lock(tmp_path: Path) -> None:
    arc = _arc(tmp_path)
    real_lock_for = arc._segments._lock_for

    def _slow_lock_for(session_id: str, scope: str) -> Any:
        time.sleep(0.2)  # stands in for a slow store RPC
        return real_lock_for(session_id, scope)

    arc._segments._lock_for = _slow_lock_for  # type: ignore[method-assign]
    app = _fake_app(arc)
    minter = open_turn_minter(app, "sess5", "turn5")
    transcript = _transcript("sess5", "turn5", app)

    started = time.monotonic()
    transcript.append_text_delta("main", "answer", "quick")
    transcript.close_open_text()
    elapsed = time.monotonic() - started
    assert elapsed < 0.05, f"close_open_text() blocked on the store: {elapsed:.3f}s"

    started = time.monotonic()
    transcript.append_part(_tool_call("call_y", "tc_y"))
    transcript.append_part(_tool_result("call_y", "tr_y"))
    elapsed = time.monotonic() - started
    assert elapsed < 0.05, f"append_part() blocked on the store: {elapsed:.3f}s"

    assert minter.drain(timeout=5.0)  # the slow mints DID land, off the caller's thread
    assert len(_atoms_on_lane(arc, "sess5")) == 2
    minter.close()


# --------------------------------------------------------------------------- #
# 6. The loop never blocks on a mint
# --------------------------------------------------------------------------- #


def test_loop_never_blocks_while_a_mint_is_stuck(tmp_path: Path) -> None:
    arc = _arc(tmp_path)
    reached = threading.Event()
    release = threading.Event()
    real_lock_for = arc._segments._lock_for

    def _stuck_lock_for(session_id: str, scope: str) -> Any:
        reached.set()
        assert release.wait(timeout=5.0), "test released the mint too late"
        return real_lock_for(session_id, scope)

    arc._segments._lock_for = _stuck_lock_for  # type: ignore[method-assign]
    app = _fake_app(arc)
    minter = open_turn_minter(app, "sess6", "turn6")
    transcript = _transcript("sess6", "turn6", app)

    transcript.append_text_delta("main", "answer", "hi")
    transcript.close_open_text()  # queues the seal; the minter thread now sits stuck
    assert reached.wait(timeout=2.0), "the minter thread never reached the slow store call"

    ticks = 0

    async def _tick_the_loop() -> None:
        nonlocal ticks
        for _ in range(50):
            await asyncio.sleep(0)
            ticks += 1

    asyncio.run(_tick_the_loop())
    assert ticks == 50, "the loop stalled while the minter thread was blocked on the store"

    release.set()
    assert minter.drain(timeout=5.0)
    minter.close()


# --------------------------------------------------------------------------- #
# 7. Finalize mints exactly the unminted remainder
# --------------------------------------------------------------------------- #


def test_finalize_mints_exactly_the_unsealed_remainder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arc = _arc(tmp_path)
    app = _fake_app(arc)
    calls: list[str] = []
    real_append = part_atom_minter.append_part_atom

    def _counting(store: Any, sid: str, content: dict[str, Any]) -> Any:
        calls.append(str(content.get("part_id") or content.get("atom_role")))
        return real_append(store, sid, content)

    monkeypatch.setattr(part_atom_minter, "append_part_atom", _counting)

    minter = open_turn_minter(app, "sess7", "turn7")
    transcript = _transcript("sess7", "turn7", app)

    transcript.append_text_delta("main", "reasoning", "sealed live text")
    transcript.close_open_text()  # seal #1 (live)
    transcript.append_part(_tool_call("call_z", "tc_z"))
    transcript.append_part(_tool_result("call_z", "tr_z"))  # seal #2 (live, the tool_call)
    assert minter.drain(timeout=5.0)
    live_call_count = len(calls)
    assert live_call_count == 2  # the tool_result itself never seals live

    # A part appended AFTER the live seals settled — never sealed live either.
    transcript.append_part(
        Part(
            id="p_batch",
            type="text",
            agent_id="main",
            text="batch tail",
            metadata={"stream_source": "batch"},
        )
    )
    frozen = transcript.finalize()
    message = Message(
        id=transcript.message_id or "msg_asst_7",
        session_id="sess7",
        turn_id="turn7",
        role="assistant",
        created_at="t",
        updated_at="t",
        parts=frozen,
        stop_reason="end_turn",
    )
    written = minter.mint_remainder(message)
    finalize_calls = calls[live_call_count:]

    # Unsealed remainder: the tool_result + the batch text = 2, plus the envelope = 3.
    assert len(finalize_calls) == 3
    assert written == 3
    minter.close()


# --------------------------------------------------------------------------- #
# 8. ``atoms_minted=True`` skips the batch mint; ``persist_finalized_message``
#    still records the state_merge op as an explicit separate step
# --------------------------------------------------------------------------- #


def test_atoms_minted_true_skips_the_mint_persist_still_records_state_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arc = _arc(tmp_path)
    app = _fake_app(arc)
    minter = open_turn_minter(app, "sess8", "turn8")

    merge_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "clio_agent.gact.transcript_projection.record_state_merge_best_effort",
        lambda arc_, sid_, msg_: merge_calls.append((sid_, msg_.id)),
    )

    message1 = Message(
        id="msg_asst_8a",
        session_id="sess8",
        turn_id="turn8",
        role="assistant",
        created_at="t",
        updated_at="t",
        parts=[],
        stop_reason="end_turn",
    )
    minter.mint_remainder(message1)  # the envelope lands directly (bypassing the seam)
    before = len(_atoms_on_lane(arc, "sess8"))

    # ``on_message_appended`` itself is a no-op under atoms_minted=True: no new atoms,
    # no state_merge record (that is NOT its job).
    _append_session_message(app, "sess8", message1, atoms_minted=True)
    assert len(_atoms_on_lane(arc, "sess8")) == before
    assert app.state.messages["sess8"][-1].id == "msg_asst_8a"
    assert merge_calls == []

    # persist_finalized_message is the real call path: mint_remainder + the append seam
    # + the EXPLICIT record_state_merge_best_effort step.
    message2 = Message(
        id="msg_asst_8b",
        session_id="sess8",
        turn_id="turn8",
        role="assistant",
        created_at="t",
        updated_at="t",
        parts=[],
        stop_reason="end_turn",
    )
    persist_finalized_message(app, "sess8", message2)
    assert merge_calls == [("sess8", "msg_asst_8b")]
    minter.close()


# --------------------------------------------------------------------------- #
# 9. The envelope atom is the sole authority for message-level fields
# --------------------------------------------------------------------------- #


def test_envelope_atom_is_the_sole_authority_for_message_level_fields(tmp_path: Path) -> None:
    arc = _arc(tmp_path)
    app = _fake_app(arc)
    minter = open_turn_minter(app, "sess9", "turn9")
    transcript = _transcript("sess9", "turn9", app)

    transcript.append_text_delta("main", "answer", "hello")
    transcript.close_open_text()
    assert minter.drain(timeout=5.0)

    frozen = transcript.finalize()
    message = Message(
        id=transcript.message_id,
        session_id="sess9",
        turn_id="turn9",
        role="assistant",
        created_at="t",
        updated_at="t",
        parts=frozen,
        tokens=Tokens(input=5, output=7),
        cost_usd=0.02,
        stop_reason="end_turn",
    )
    minter.mint_remainder(message)

    atoms = _atoms_on_lane(arc, "sess9")
    part_atoms = [a for a in atoms if a["atom_role"] == "part"]
    assert part_atoms
    forbidden = {"usage", "stop_reason", "cost_usd", "error_info"}
    for atom in part_atoms:
        assert not (set(atom) & forbidden), f"a part atom carries message authority: {atom}"

    envelope = next(a for a in atoms if a["atom_role"] == "envelope")
    assert envelope["message"]["stop_reason"] == "end_turn"
    assert envelope["usage"] == {"input": 5, "output": 7, "cache_read": 0, "cache_write": 0}

    # Drop the envelope from the group: reproduce_message_wire must degrade to a
    # TYPED incomplete message, never silently serve the parts as "complete".
    groups = group_atoms_in_order(atoms)
    assert len(groups) == 1
    without_envelope = [a for a in groups[0] if a["atom_role"] != "envelope"]
    rebuilt = reproduce_message_wire(without_envelope)
    assert rebuilt["stop_reason"] == "incomplete"
    assert rebuilt["metadata"]["transcript_incomplete"]["reason"] == TRANSCRIPT_INCOMPLETE_REASON
    minter.close()


# --------------------------------------------------------------------------- #
# 10. A hand-built v1 lane reproduces byte-identically
# --------------------------------------------------------------------------- #


def test_hand_built_v1_lane_reproduces_byte_identically() -> None:
    message = Message(
        id="msg_v1",
        session_id="sess_v1",
        turn_id="turn_v1",
        role="assistant",
        created_at="c",
        updated_at="u",
        parts=[Part(id="p1", type="text", text="hi", sequence=1)],
        tokens=Tokens(input=1),
        cost_usd=0.0,
        stop_reason="end_turn",
    )
    envelope = message.model_dump(exclude={"parts"})
    v1_atom = {
        "schema_version": 1,
        # deliberately NO "envelope_authority" key — a truly pre-#1337 record.
        "atom_role": "part",
        "message_id": message.id,
        "part_id": "p1",
        "part_index": 0,
        "created_at": message.created_at,
        "role": message.role,
        "kind": "text",
        "stream_source": "",
        "usage": message.tokens.model_dump(),
        "status": "",
        "part": message.parts[0].model_dump(),
        "message": envelope,
    }
    assert "envelope_authority" not in v1_atom

    groups = group_atoms_in_order([v1_atom])
    assert groups == [[v1_atom]]
    rebuilt = reproduce_message_wire(groups[0])
    assert rebuilt == message.model_dump(exclude_none=True)


# --------------------------------------------------------------------------- #
# 11. group_atoms_in_order: interleave / v1 adjacency / atom-profile reseal
# --------------------------------------------------------------------------- #


def test_group_atoms_in_order_keeps_an_interleaved_message_separate() -> None:
    x0 = _bare_atom("X", "part", "atom", 0, "px0")
    x1 = _bare_atom("X", "part", "atom", 1, "px1")
    y0 = _bare_atom("Y", "part", "atom", 0, "py0")
    x2 = _bare_atom("X", "part", "atom", 2, "px2")
    x_env = _bare_atom("X", "envelope", "atom", 3)

    groups = group_atoms_in_order([x0, x1, y0, x2, x_env])
    assert len(groups) == 2
    x_group = next(g for g in groups if g[0]["message_id"] == "X")
    y_group = next(g for g in groups if g[0]["message_id"] == "Y")
    assert x_group == [x0, x1, x2, x_env]
    assert len([a for a in x_group if a["atom_role"] == "part"]) == 3
    assert y_group == [y0]


def test_group_atoms_in_order_splits_adjacent_v1_messages_sharing_one_id() -> None:
    shared_id = "msg_asst_shared"
    first_message_part = _bare_atom(shared_id, "part", "inline", 0, "a0")
    second_message_part = _bare_atom(
        shared_id, "part", "inline", 0, "b0"
    )  # unseen id, index resets

    groups = group_atoms_in_order([first_message_part, second_message_part])
    assert groups == [[first_message_part], [second_message_part]]


def test_group_atoms_in_order_keeps_a_late_atom_profile_reseal_in_its_group() -> None:
    mid = "msg_reseal"
    p0_first = _bare_atom(mid, "part", "atom", 0, "p0")
    p1 = _bare_atom(mid, "part", "atom", 1, "p1")
    p0_resealed = _bare_atom(
        mid, "part", "atom", 0, "p0"
    )  # same id + index: a reseal, not a new msg

    groups = group_atoms_in_order([p0_first, p1, p0_resealed])
    assert groups == [[p0_first, p1, p0_resealed]]


# --------------------------------------------------------------------------- #
# 12. Crash without envelope: reload sees a TYPED incomplete message
# --------------------------------------------------------------------------- #


def test_crash_without_envelope_reassembles_as_typed_incomplete(tmp_path: Path) -> None:
    arc = _arc(tmp_path)
    app = _fake_app(arc)
    minter = open_turn_minter(app, "sess12", "turn12")
    transcript = _transcript("sess12", "turn12", app)

    transcript.append_text_delta("main", "answer", "partial answer before crash")
    transcript.close_open_text()
    assert minter.drain(timeout=5.0)
    # Never finalize; never mint_remainder / the envelope — simulate a hard crash.
    minter.close()

    messages = assemble_session_messages(arc, "sess12")
    assert len(messages) == 1
    message = messages[0]
    assert message.stop_reason == "incomplete"
    assert message.metadata["transcript_incomplete"]["reason"] == TRANSCRIPT_INCOMPLETE_REASON
    assert [p.text for p in message.parts if p.type == "text"] == ["partial answer before crash"]


# --------------------------------------------------------------------------- #
# 13. A mint failure mid-turn is audited, the ledger keeps working, and finalize
#     mints the missing part so reload == live
# --------------------------------------------------------------------------- #


def test_mint_failure_mid_turn_is_audited_and_recovered_at_finalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arc = _arc(tmp_path)
    app = _fake_app(arc)

    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        part_atom_minter, "stream_audit", lambda stage, **fields: audits.append((stage, fields))
    )

    real_append = part_atom_minter.append_part_atom
    call_n = {"n": 0}

    def _flaky_append(store: Any, sid: str, content: dict[str, Any]) -> Any:
        call_n["n"] += 1
        if call_n["n"] == 2:
            raise RuntimeError("simulated store failure")
        return real_append(store, sid, content)

    monkeypatch.setattr(part_atom_minter, "append_part_atom", _flaky_append)

    minter = open_turn_minter(app, "sess13", "turn13")
    transcript = _transcript("sess13", "turn13", app)

    transcript.append_text_delta("main", "reasoning", "first thought")
    transcript.close_open_text()  # seal #1: succeeds
    transcript.append_text_delta("main", "answer", "second thought")
    transcript.close_open_text()  # seal #2: raises inside the minter thread
    assert minter.drain(timeout=5.0)

    job_failed = [fields for stage, fields in audits if stage == "transcript.job_failed"]
    assert any(f["reason"] == PART_ATOM_SEAL_FAILED for f in job_failed)

    # The ledger keeps working after the failure (no exception reaches the caller).
    transcript.append_text_delta("main", "answer", " still able to append more")
    transcript.close_open_text()

    frozen = transcript.finalize()
    message = Message(
        id=transcript.message_id,
        session_id="sess13",
        turn_id="turn13",
        role="assistant",
        created_at="t",
        updated_at="t",
        parts=frozen,
        stop_reason="end_turn",
    )
    app.state.messages["sess13"] = []
    persist_finalized_message(app, "sess13", message)

    reloaded = assemble_session_messages(arc, "sess13")
    assert len(reloaded) == 1
    live_dump = message.model_dump(exclude_none=True)
    reload_dump = reloaded[0].model_dump(exclude_none=True)
    report = N.diff_persistence([live_dump], [reload_dump])
    assert report.empty, report.pretty()


# --------------------------------------------------------------------------- #
# 14. A drain-timeout double write (two atoms for one part) reproduces to one
#     part — last wins
# --------------------------------------------------------------------------- #


def test_double_sealed_atom_for_one_part_reproduces_to_one_part_last_wins() -> None:
    stub = message_stub(message_id="m_dup", turn_id="t", session_id="s", created_at="c")
    first_version = {"id": "p1", "type": "text", "text": "first version", "sequence": 1}
    final_version = {"id": "p1", "type": "text", "text": "final version", "sequence": 1}
    atom_a = build_sealed_part_atom(stub, first_version, 0, sealed_at="t1", seal_source="live")
    atom_b = build_sealed_part_atom(stub, final_version, 0, sealed_at="t2", seal_source="live")
    message = Message(
        id="m_dup",
        session_id="s",
        turn_id="t",
        role="assistant",
        created_at="c",
        updated_at="c",
        parts=[Part(**final_version)],
    )
    envelope = build_envelope_atom(message)

    rebuilt = reproduce_message_wire([atom_a, atom_b, envelope])
    assert len(rebuilt["parts"]) == 1
    assert rebuilt["parts"][0]["text"] == "final version"


# --------------------------------------------------------------------------- #
# 15. Failed finalize keeps the streamed parts: TestClient-driven, real crash
# --------------------------------------------------------------------------- #


def test_failed_finalize_keeps_the_streamed_parts_and_reload_equals_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(app: Any, sid: str, error_info: Any) -> Any:
        raise RuntimeError("simulated finalize failure")

    monkeypatch.setattr("clio_agent.gact.app._enrich_cancellation_error_info", _boom)

    app = build_app(
        sessions_path=tmp_path / "s.json", agent=FakeClioAgent(answer="streamed then crashed")
    )
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        ack = c.post(
            f"/v1/sessions/{sid}/messages", json={"parts": [{"type": "text", "text": "hi"}]}
        )
        assert ack.status_code == 200, ack.text
        user_id = ack.json()["message_id"]

        deadline = time.monotonic() + 20.0
        status = "running"
        while time.monotonic() < deadline:
            status = c.get(f"/v1/sessions/{sid}").json()["status"]
            if status != "running":
                break
            time.sleep(0.05)
        assert status == "error", f"finalize crash must settle the turn; stayed {status!r}"

        live = [m.model_dump(exclude_none=True) for m in app.state.messages.get(sid, [])]
        error_turns = [m for m in live if m["role"] == "assistant" and m.get("turn_id") == user_id]
        assert error_turns, "the error turn must be visible live"
        assert any(p.get("text") == "streamed then crashed" for p in error_turns[0]["parts"]), (
            f"the streamed answer must survive the crash: {error_turns[0]['parts']}"
        )

        app.state.messages.clear()
        reloaded = [m.model_dump(exclude_none=True) for m in app.state.messages.get(sid, [])]
        report = N.diff_persistence(live, reloaded)
        assert report.empty, f"reload != live after a failed finalize:\n{report.pretty()}"


# --------------------------------------------------------------------------- #
# 16. The paused transcript persists only the remainder
# --------------------------------------------------------------------------- #


def test_paused_transcript_persists_only_the_remainder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="pause", mode="chat")
    app.state.sessions.update(
        session.id,
        metadata_patch={"pending_ask_user": {"question": "Which view?", "kind": "freeform"}},
    )
    user = Message(
        id="user_pause",
        session_id=session.id,
        role="user",
        parts=[],
        created_at=session.created_at,
        updated_at=session.updated_at,
    )
    # A real POST /messages appends the user message before the turn starts streaming;
    # match that so the pause's already-persisted dedupe check (a resident-ledger
    # ``.get()``) hits the warm in-memory cache instead of rehydrating a still-open,
    # envelope-less transcript from ARC (a false "already persisted" positive).
    app.state.messages[session.id] = [user]
    state = TurnState(
        app=app,
        sid=session.id,
        sess=session,
        bus=app.state.bus,
        user_msg=user,
        user_text="Show the tool result",
        turn_agent_id="main",
        turn_id=user.id,
        trace_id="trace_pause",
        retry_attempt_id="",
        native_images=[],
        context_frame={"id": "frame_pause"},
    )
    open_turn_minter(app, session.id, user.id)
    state.transcript = app.state.turn_transcripts.open_turn(
        session.id,
        user.id,
        EventBusTranscriptPublisher(app.state.bus, session.id),
        sink=transcript_sink(app, session.id),
    )
    state.transcript.append_text_delta("main", "reasoning", "Keep this exact thought.")
    state.transcript.close_open_text()  # seals live: 1 atom
    state.transcript.append_part(
        Part(
            id="result_pause",
            type="tool_result",
            tool_name="load_skill",
            call_id="call_pause",
            content=[Part(id="raw_pause", type="text", text='{"skill":"unchanged"}')],
        )
    )  # no matching live tool_call: never sealed live

    minter = turn_minter(app, session.id)
    assert minter is not None
    assert minter.drain(timeout=5.0)

    calls: list[str] = []
    real_append = part_atom_minter.append_part_atom

    def _counting(store: Any, sid: str, content: dict[str, Any]) -> Any:
        calls.append(str(content.get("atom_role")))
        return real_append(store, sid, content)

    monkeypatch.setattr(part_atom_minter, "append_part_atom", _counting)
    monkeypatch.setattr(
        "clio_agent.gact.user_question_pause._finalize_context_frame", lambda *a, **k: None
    )
    monkeypatch.setattr("clio_agent.gact.enrichment._finalize_context_frame", lambda *a, **k: None)

    assert maybe_pause_for_user(state, SimpleNamespace(), update_retry_attempt=lambda *a, **k: None)

    # The remainder: the never-sealed tool_result + the envelope = 2 atoms written here.
    assert len(calls) == 2
    close_turn_minter(app, session.id)
