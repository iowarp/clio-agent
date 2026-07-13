"""#737 S4 — the ``message_part`` atom family: mint, reproduce, identity, isolation.

The slice provisions a wire-identity atom family on the canonical ``_events`` log,
dual-written alongside ``final_message`` at the message-persist seam (design
``docs/design/unified-arc-highway.md`` §2.3, §2.8c, §4.2 step 4). These tests are the
step-4 gate:

* **reproducibility (§4.2 step-4 gate / the (d) proof).** EVERY wire field of the
  persisted message (``Message.model_dump(exclude_none=True)`` — the exact
  ``final_message`` shape) is reconstructable from the part atoms alone. Proven with a
  FIELD-PATH differ (the S0 harness ``tests/equivalence/normalizers.first_divergence``)
  so a one-field regression is reported as ``.parts[1].text`` rather than a bare
  ``False``. Exercised on a rich finalize-shaped message, a user message, a zero-part
  error-settle message, AND on a REAL gact turn end-to-end.
* **identity pin (§2.8c).** ids/timestamps are minted ONCE (by the message) and STORED
  in the atom, so eviction + rehydration reproduces them byte-exactly — a re-mint on
  read would break ``reload == live`` identity. Proven across both backends by evicting
  the hot segment copy and re-reading from the store.
* **additive msgspec back-compat (§2.3).** ``message_part`` is an additive
  ``SegmentKind``; a ``message_part`` segment round-trips through encode/decode and the
  new kind never perturbs the semantic-event log or a working-set render (invisible
  until S5).

SABOTAGE (recorded, run manually, NOT committed as a second test):

* (a) drop one wire field in the mint — delete e.g. the ``"cost_usd"`` /
  ``"stop_reason"`` key from the ``envelope`` in ``part_atoms._atom_content`` (or drop
  a part field from ``part.model_dump()``): ``test_reproduce_*`` goes RED with
  ``DIVERGENCE at .cost_usd (keys)`` — the differ names the exact dropped field path.
* (b) re-mint an id on read — make ``part_atoms.reproduce_message_wire`` overwrite
  ``envelope["id"] = _new_message_id("asst")`` (or regenerate a ``part_id``):
  ``test_identity_pin_survives_eviction`` goes RED at ``.id`` / the ``part_id``
  assertion — the stored id no longer matches the live one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import Segment, decode_segment, encode_segment
from clio_agent.gact.part_atoms import (
    MESSAGE_PART_KIND,
    MESSAGE_PART_SCOPE,
    build_message_part_atoms,
    load_message_part_atoms,
    mint_message_part_atoms,
    reproduce_message_wire,
)
from clio_agent.gact.types import ErrorInfo, Message, Part, Tokens
from tests.equivalence.normalizers import first_divergence

# --------------------------------------------------------------------------- #
# Fixtures — both backends (LocalFS + clio-core), mirroring tests/test_arc/conftest
# --------------------------------------------------------------------------- #


@pytest.fixture(params=["local", "cte"])
def arc(request: Any, tmp_path: Path) -> Iterator[ARCMemory]:
    """A fresh ARCMemory on BOTH backends (the ``cte`` leg skips without the binding)."""
    backend = request.param
    if backend == "cte":
        pytest.importorskip("clio_cte_core_ext")
        from clio_agent.arc.storage import make_arc_store

        memory = ARCMemory(store=make_arc_store(backend="cte"))
        memory.clear_all()
        try:
            yield memory
        finally:
            memory.clear_all()
        return
    yield ARCMemory(data_dir=str(tmp_path / "arc"))


def _rich_assistant_message() -> Message:
    """A finalize-shaped assistant message exercising every part type + metadata field.

    Mirrors what ``turn_finalize.finalize_turn`` assembles: a routing_decision banner,
    a thinking part, the text answer, a tool_call + tool_result (with nested content),
    and a file_diff frozen ``status="pending"`` — plus the message-level metadata
    (``stream_source`` / ``expert_handoffs`` / ``tools_called``), tokens, and cost the
    finalize seam stamps before the ``Message`` is built.
    """
    return Message(
        id="msg_asst_deadbeef0001",
        turn_id="msg_user_cafe0001",
        session_id="sess_s4",
        role="assistant",
        created_at="2026-07-12T10:00:00.000001+00:00",
        updated_at="2026-07-12T10:00:03.500002+00:00",
        parts=[
            Part(
                id="part_route01",
                type="routing_decision",
                agent_id="main",
                selected_agent="geospatial",
                rationale="query is about station coverage",
                execution_path="expert_loop",
                metadata={"route_source": "dspy", "route_reason": "geo keywords"},
            ),
            Part(
                id="part_think01",
                type="thinking",
                agent_id="geospatial",
                text="Consider the dense-station region around LA.",
                metadata={"stream_source": "batch"},
            ),
            Part(
                id="part_tool01",
                type="tool_call",
                agent_id="geospatial",
                call_id="call_1",
                tool_name="query_stations",
                thought="look up stations",
                input={"region": "LA", "limit": 5},
            ),
            Part(
                id="part_tres01",
                type="tool_result",
                agent_id="geospatial",
                call_id="call_1",
                content=[Part(id="part_tres01_final_text", type="text", text="5 stations")],
            ),
            Part(
                id="part_text01",
                type="text",
                agent_id="geospatial",
                text="There are 5 dense stations near LA.",
                sequence=5,
            ),
            Part(
                id="part_hand01",
                type="expert_handoff",
                agent_id="main",
                parent_agent="main",
                child_agent="geospatial",
                stage="parent.resumed",
                status="completed",
                metadata={
                    "expert_handoff": {
                        "output": "5 dense stations near LA (verbatim child output).",
                        "workflow_state": {"region": "LA", "count": 5},
                    }
                },
            ),
            Part(
                id="part_diff01",
                type="file_diff",
                agent_id="geospatial",
                path="report.md",
                unified_diff="@@ -0,0 +1 @@\n+5 stations\n",
                new_content="5 stations\n",
                status="pending",
                edit_mode="whole",
                lines_added=1,
                lines_removed=0,
            ),
        ],
        tokens=Tokens(input=1200, output=340, cache_read=64, cache_write=0),
        cost_usd=0.0123,
        stop_reason="end_turn",
        error_info=None,
        metadata={
            "stream_source": "live",
            "tools_called": [{"name": "query_stations", "ok": True}],
            "expert_handoffs": [
                {"parent_agent": "main", "child_agent": "geospatial", "status": "completed"}
            ],
        },
    )


def _user_message() -> Message:
    """A user ingest message (turn.py:start_background_user_turn shape)."""
    return Message(
        id="msg_user_cafe0001",
        turn_id="msg_user_cafe0001",
        session_id="sess_s4",
        role="user",
        created_at="2026-07-12T09:59:59.000000+00:00",
        updated_at="2026-07-12T09:59:59.000000+00:00",
        parts=[Part(id="part_u1", type="text", text="How many dense stations near LA?")],
        metadata={"source": "cli"},
    )


def _zero_part_error_message() -> Message:
    """A finalize error-settle message (settle_failed_finalize: ``parts=[]``)."""
    return Message(
        id="msg_asst_error0001",
        turn_id="msg_user_cafe0002",
        session_id="sess_s4",
        role="assistant",
        created_at="2026-07-12T10:05:00.000000+00:00",
        updated_at="2026-07-12T10:05:00.000000+00:00",
        parts=[],
        tokens=Tokens(input=10, output=0),
        cost_usd=0.0,
        stop_reason="error",
        error_info=ErrorInfo(
            error="finalize_error",
            message="turn finalize raised: boom",
            details={"stage": "finalize"},
            recoverable=True,
        ),
    )


# --------------------------------------------------------------------------- #
# (d) reproducibility — pure: reproduce == model_dump(exclude_none=True), field-path
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "message_factory",
    [_rich_assistant_message, _user_message, _zero_part_error_message],
    ids=["rich_assistant", "user", "zero_part_error"],
)
def test_reproduce_matches_final_message_wire(message_factory: Any) -> None:
    """Every wire field of ``final_message`` is reproducible from the part atoms alone."""
    message = message_factory()
    expected = message.model_dump(exclude_none=True)
    atoms = build_message_part_atoms(message)
    reproduced = reproduce_message_wire(atoms)
    div = first_divergence(expected, reproduced)
    assert div is None, div.pretty()


def test_zero_part_message_yields_single_envelope_atom() -> None:
    """A zero-part message provisions exactly ONE envelope-only atom (part=None)."""
    atoms = build_message_part_atoms(_zero_part_error_message())
    assert len(atoms) == 1
    assert atoms[0]["part"] is None
    assert atoms[0]["part_id"] == ""
    assert atoms[0]["atom_role"] == "envelope"


def test_expert_handoff_output_is_verbatim() -> None:
    """The ``expert_handoff`` identity is copied VERBATIM (frozen 1.9, #880 baseline-0)."""
    message = _rich_assistant_message()
    atoms = build_message_part_atoms(message)
    handoff_atoms = [a for a in atoms if "expert_handoff" in a]
    assert len(handoff_atoms) == 1
    assert (
        handoff_atoms[0]["expert_handoff"]["output"]
        == "5 dense stations near LA (verbatim child output)."
    )


def test_file_diff_status_frozen_pending() -> None:
    """A file_diff atom freezes ``status="pending"`` (frozen surface 1.14)."""
    atoms = build_message_part_atoms(_rich_assistant_message())
    diff_atoms = [a for a in atoms if a["kind"] == "file_diff"]
    assert len(diff_atoms) == 1
    assert diff_atoms[0]["status"] == "pending"


# --------------------------------------------------------------------------- #
# identity pin — mint, evict, rehydrate, reproduce byte-exactly (both backends)
# --------------------------------------------------------------------------- #


def test_identity_pin_survives_eviction(arc: ARCMemory) -> None:
    """ids/timestamps stored once reproduce byte-exactly after eviction + rehydration."""
    message = _rich_assistant_message()
    sid = message.session_id
    expected = message.model_dump(exclude_none=True)

    minted = mint_message_part_atoms(arc, sid, message)
    assert len(minted) == len(message.parts)
    assert all(seg.kind == MESSAGE_PART_KIND for seg in minted)

    live = reproduce_message_wire(load_message_part_atoms(arc, sid)[message.id])
    assert first_divergence(expected, live) is None

    # Evict the hot segment copy so the next read rehydrates from the store (the
    # eviction+rehydration path; write-through persistence loses nothing).
    arc._segments.release(sid)

    reloaded_groups = load_message_part_atoms(arc, sid)
    reloaded = reproduce_message_wire(reloaded_groups[message.id])
    div = first_divergence(expected, reloaded)
    assert div is None, div.pretty()

    # The pin's teeth: the STORED ids equal the live ids (never re-minted on read).
    assert reloaded["id"] == message.id
    stored_part_ids = [a["part_id"] for a in reloaded_groups[message.id]]
    assert stored_part_ids == [p.id for p in message.parts]
    assert [p["id"] for p in reloaded["parts"]] == [p.id for p in message.parts]
    # The identity timestamp is stored, not regenerated.
    assert reloaded["created_at"] == message.created_at


def test_mint_lands_on_the_reserved_message_part_lane(arc: ARCMemory) -> None:
    """Atoms land on ``_events/m`` with the additive ``message_part`` kind."""
    message = _user_message()
    mint_message_part_atoms(arc, message.session_id, message)
    segs = arc._segments.list_segments(
        message.session_id, MESSAGE_PART_SCOPE, include_tombstoned=True
    )
    assert len(segs) == 1
    assert segs[0].scope == MESSAGE_PART_SCOPE
    assert segs[0].kind == MESSAGE_PART_KIND
    assert segs[0].content["message_id"] == message.id


# --------------------------------------------------------------------------- #
# additive msgspec back-compat + isolation (invisible until S5)
# --------------------------------------------------------------------------- #


def test_message_part_segment_roundtrips_msgpack() -> None:
    """A ``message_part`` segment encodes/decodes byte-identically (additive kind)."""
    atoms = build_message_part_atoms(_rich_assistant_message())
    seg = Segment(
        scope=MESSAGE_PART_SCOPE,
        kind=MESSAGE_PART_KIND,
        content=atoms[0],
        session_id="sess_s4",
        step=-1,
        order=1.0,
        logical_time=1,
    )
    decoded = decode_segment(encode_segment(seg))
    assert decoded.kind == MESSAGE_PART_KIND
    assert decoded.content == atoms[0]


def test_atoms_do_not_perturb_semantic_event_view(arc: ARCMemory) -> None:
    """Minting atoms leaves the live semantic-event view untouched (isolation).

    The atoms live on the ``_events/m`` sibling lane and are neither ``semantic_event``
    kind nor a working-set kind, so the live reader ignores them and no working-set
    render or turn view changes — the dual-write is invisible on every read surface.
    """
    sid = "sess_iso"
    # A real semantic event lands one turn on the log.
    from clio_agent.gact.semantic_events import SemanticEvent

    arc.on_semantic_event(
        SemanticEvent(
            event_type="react.step.completed",
            session_id=sid,
            turn_id="t1",
            trace_id="tr1",
            summary="a step",
        )
    )
    before = arc.get_live_context(sid)

    msg = _rich_assistant_message()
    mint_message_part_atoms(arc, sid, msg)

    after = arc.get_live_context(sid)
    assert after == before  # the message_part atoms never enter the turn view
    # And they are not visible in the expert working-set render.
    assert arc.render_working_set(sid, "geospatial") == []


# --------------------------------------------------------------------------- #
# end-to-end — the persist-seam hook fires on a REAL gact turn (the (d) proof
# against the actual finalize output, not a hand-built message)
# --------------------------------------------------------------------------- #


def test_real_turn_persist_seam_mints_reproducible_atoms(tmp_path: Path) -> None:
    """A real gact turn dual-writes atoms that reproduce EVERY persisted message.

    Drives ``build_app`` + a ``FakeClioAgent`` (no LM) through ``TestClient`` so the
    ``session_store._append_session_message`` hook runs on the REAL user + assistant
    messages, then proves ``reproduce_message_wire(load(atoms)[msg.id])`` equals each
    persisted ``Message.model_dump(exclude_none=True)`` (the exact ``final_message``
    shape) — the step-4 gate on live finalize output, field-path-diffed.
    """
    import time

    from fastapi.testclient import TestClient

    from clio_agent.gact.app import build_app
    from tests.test_gact.test_post_messages import FakeClioAgent

    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    agent = FakeClioAgent(answer="five dense stations near LA")
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=agent, arc=arc)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "s4"}).json()["id"]
        ack = client.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "how many dense stations near LA?"}]},
        )
        assert ack.status_code == 200, ack.text
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if client.get(f"/v1/sessions/{sid}").json()["status"] != "running":
                break
            time.sleep(0.05)
        persisted = list(app.state.messages.get(sid, []))

    assert [m.role for m in persisted] == ["user", "assistant"]
    groups = load_message_part_atoms(arc, sid)
    for message in persisted:
        assert message.id in groups, f"no atoms minted for {message.role} {message.id}"
        expected = message.model_dump(exclude_none=True)
        reproduced = reproduce_message_wire(groups[message.id])
        div = first_divergence(expected, reproduced)
        assert div is None, f"{message.role}: {div.pretty() if div else ''}"
