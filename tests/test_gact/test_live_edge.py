"""#737 S7 — the mutable live-edge streaming atom: coalescing, seal, amplification budget.

The LAST highway slice (design ``docs/design/unified-arc-highway.md`` §2.3, §4.2 step 7):
an identity-stable atom that GROWS IN PLACE during streaming while the canonical log gains
exactly ONE sealed atom per part. The chosen shape is **(i) a bounded mutable head slot in
the read-model, not a log record until sealed** — so append-only-within-a-session (§2.6)
holds by construction and there is NO per-token tombstone storm (§4.2 step-7 risk note).

These tests are the slice's gates:

* **in-place growth + coalescing** — deltas accumulate into one identity-stable slot;
  the coalesced text is the read-model's view of the growing edge.
* **write-amplification budget (the §4.2 step-7 risk note)** — a scripted 5k-char stream
  costs ``durable_atoms_for_part <= LIVE_EDGE_MAX_ATOMS_PER_PART`` (default policy: ZERO
  extra atoms). Both backends.
* **coexistence with append-only (§2.6)** — streaming never writes to the SEALED
  ``_events/m`` transcript lane and never tombstones; the ephemeral checkpoint lane is
  dropped at settle. Both backends.
* **the seal byte-match** — the coalesced edge must equal the transcript's sealed text
  (:class:`LiveEdgeSealMismatchError`, typed, no silent fallback). The *sealed-vs-finalize*
  invariant proper is owned by the S5 ``reload == live`` sweep (the live edge never writes
  the sealed atom, so that sweep stays green — see ``test_live_edge_integration``).
* **the flag + regime gate** — engaged ONLY under flag + the S5 atoms regime; default OFF
  leaves every seam a no-op (the frozen wire 1.2 / sealed reload 1.3 unchanged).

SABOTAGE (recorded, run manually — matches the two the task pins):

* (a) unbounded per-token amplification — set ``checkpoint_every=1`` (chunk size 1 /
  disable the bound): :func:`test_write_amplification_budget` goes RED (durable atoms
  == the char count, far over the budget). Restore ``checkpoint_every=0`` (the default).
* (b) sealed-vs-finalize mismatch — make the live-edge coalescing drop the last delta (so
  the seal text != the transcript text): :meth:`LiveEdgeSlot.seal` raises
  :class:`LiveEdgeSealMismatchError` (:func:`test_seal_mismatch_raises`), and the S5
  ``tests/test_equivalence/test_reload_equals_live`` sweep is the authoritative gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.live_edge import (
    DEFAULT_CHECKPOINT_EVERY,
    LIVE_EDGE_CHECKPOINT_SCOPE,
    LIVE_EDGE_MAX_ATOMS_PER_PART,
    LiveEdgeRegistry,
    LiveEdgeSealMismatchError,
    live_edge_enabled,
    overlay_in_flight_part,
    registry_for,
    seal_and_settle,
)
from clio_agent.gact.part_atoms import MESSAGE_PART_SCOPE
from clio_agent.gact.transcript_projection import REGIME_ATOMS, REGIME_LEGACY, REGIME_METADATA_KEY
from clio_agent.gact.types import Part


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


# --------------------------------------------------------------------------- #
# Fake app wiring — a minimal app.state carrying a pinned session + an ARC
# --------------------------------------------------------------------------- #


class _FakeSessions:
    def __init__(self, regime: str | None) -> None:
        meta = {REGIME_METADATA_KEY: regime} if regime else {}
        self._rec = type("R", (), {"metadata": meta})()

    def get(self, _sid: str) -> Any:
        return self._rec


class _FakeApp:
    def __init__(self, arc: ARCMemory | None, regime: str | None) -> None:
        self.state = type(
            "S", (), {"arc": arc, "sessions": _FakeSessions(regime)}
        )()


# --------------------------------------------------------------------------- #
# In-place growth + coalescing (the read-model's view of the growing edge)
# --------------------------------------------------------------------------- #


def test_slot_grows_in_place_and_coalesces(arc: ARCMemory) -> None:
    """Deltas accumulate into ONE identity-stable slot; coalesced == the join."""

    reg = LiveEdgeRegistry()
    slot = reg.open_slot("s", part_id="p1", agent_id="main", field="answer", kind="text")
    for chunk in ["Los ", "Angeles ", "has ", "dense ", "stations."]:
        reg.append_delta("s", "p1", chunk, store=arc._segments)
    # Same slot object throughout (identity-stable, mutated in place).
    assert reg.current_slot("s") is slot
    assert slot.coalesced_text() == "Los Angeles has dense stations."
    # A delta for a NON-open part is a no-op (only the open part grows).
    reg.append_delta("s", "other", "ignored", store=arc._segments)
    assert slot.coalesced_text() == "Los Angeles has dense stations."


def test_new_part_seals_prior_open_slot(arc: ARCMemory) -> None:
    """Opening a new part retires the prior open slot (one open slot per session)."""

    reg = LiveEdgeRegistry()
    reg.open_slot("s", part_id="p1", agent_id="main", field="answer", kind="text")
    reg.append_delta("s", "p1", "first", store=arc._segments)
    slot2 = reg.open_slot("s", part_id="p2", agent_id="main", field="answer", kind="thinking")
    assert reg.current_slot("s") is slot2
    reg.append_delta("s", "p2", "second", store=arc._segments)
    assert slot2.coalesced_text() == "second"


# --------------------------------------------------------------------------- #
# The write-amplification budget (§4.2 step-7 risk note) — BOTH backends
# --------------------------------------------------------------------------- #


def test_write_amplification_budget(arc: ARCMemory) -> None:
    """A 5k-char stream costs <= the durable-atom budget (default policy: ZERO extra)."""

    reg = LiveEdgeRegistry()
    reg.open_slot(
        "s", part_id="p1", agent_id="main", field="answer", kind="text",
        checkpoint_every=DEFAULT_CHECKPOINT_EVERY,
    )
    for _ in range(5000):
        reg.append_delta("s", "p1", "x", store=arc._segments)
    reg.seal_open("s", "x" * 5000)
    atoms = reg.durable_atoms_for_part("p1")
    assert atoms <= LIVE_EDGE_MAX_ATOMS_PER_PART, f"{atoms} durable atoms over 5k-char stream"
    assert atoms == 0  # pure shape-(i): the head slot writes NOTHING durable


def test_sabotage_checkpoint_every_one_blows_the_budget(arc: ARCMemory) -> None:
    """Sabotage (a): ``checkpoint_every=1`` (chunk size 1) exceeds the budget.

    Proves the budget assertion is not a tautology — the SAME 300-char stream with the
    bound disabled writes one durable atom per char, far over
    :data:`LIVE_EDGE_MAX_ATOMS_PER_PART`.
    """

    reg = LiveEdgeRegistry()
    reg.open_slot(
        "s", part_id="p1", agent_id="main", field="answer", kind="text", checkpoint_every=1
    )
    for _ in range(300):
        reg.append_delta("s", "p1", "y", store=arc._segments)
    reg.drop_session("s", store=arc._segments)
    assert reg.durable_atoms_for_part("p1") == 300
    assert reg.durable_atoms_for_part("p1") > LIVE_EDGE_MAX_ATOMS_PER_PART


# --------------------------------------------------------------------------- #
# Coexistence with append-only (§2.6) — no sealed-lane write, no tombstone
# --------------------------------------------------------------------------- #


def test_streaming_never_touches_sealed_lane(arc: ARCMemory) -> None:
    """Coalescing 5k deltas writes NOTHING to the sealed ``_events/m`` transcript lane.

    The seal is S5's mint (a separate slice); the live edge only coalesces. So the lane
    the persistence projection reads is untouched by streaming — append-only holds and
    the memory win is not regressed.
    """

    reg = LiveEdgeRegistry()
    reg.open_slot("s", part_id="p1", agent_id="main", field="answer", kind="text")
    for _ in range(5000):
        reg.append_delta("s", "p1", "z", store=arc._segments)
    sealed = arc._segments.list_segments("s", MESSAGE_PART_SCOPE, include_tombstoned=True)
    assert sealed == []  # zero sealed atoms from streaming alone


def test_checkpoint_lane_is_ephemeral_and_dropped(arc: ARCMemory) -> None:
    """Checkpoint atoms land on the ephemeral lane and are dropped at settle (RULE 4)."""

    reg = LiveEdgeRegistry()
    reg.open_slot(
        "s", part_id="p1", agent_id="main", field="answer", kind="text", checkpoint_every=10
    )
    for _ in range(100):
        reg.append_delta("s", "p1", "w", store=arc._segments)
    mid = arc._segments.list_segments("s", LIVE_EDGE_CHECKPOINT_SCOPE, include_tombstoned=True)
    assert len(mid) == 10  # 100 chars / 10 per checkpoint
    reg.drop_session("s", store=arc._segments)
    after = arc._segments.list_segments("s", LIVE_EDGE_CHECKPOINT_SCOPE, include_tombstoned=True)
    assert after == []  # the ephemeral lane is dropped — no checkpoint lingers


# --------------------------------------------------------------------------- #
# The seal byte-match (typed, no silent fallback)
# --------------------------------------------------------------------------- #


def test_seal_matches_transcript_text(arc: ARCMemory) -> None:
    """The coalesced edge equalling the sealed text seals cleanly."""

    reg = LiveEdgeRegistry()
    reg.open_slot("s", part_id="p1", agent_id="main", field="answer", kind="text")
    reg.append_delta("s", "p1", "hello world", store=arc._segments)
    reg.seal_open("s", "hello world")  # no raise
    assert reg.current_slot("s") is None  # sealed slots are retired


def test_seal_mismatch_raises(arc: ARCMemory) -> None:
    """Sabotage (b): a coalesced edge != the sealed text raises a typed error."""

    reg = LiveEdgeRegistry()
    reg.open_slot("s", part_id="p1", agent_id="main", field="answer", kind="text")
    reg.append_delta("s", "p1", "hello", store=arc._segments)
    with pytest.raises(LiveEdgeSealMismatchError) as exc:
        reg.seal_open("s", "HELLO")
    assert exc.value.reason == "live_edge_seal_mismatch"
    assert exc.value.part_id == "p1"


# --------------------------------------------------------------------------- #
# The flag + regime gate — default OFF => every seam a no-op
# --------------------------------------------------------------------------- #


def test_gate_requires_flag_and_atoms_regime(arc: ARCMemory, monkeypatch: Any) -> None:
    """``live_edge_enabled`` needs BOTH the flag ON and the S5 atoms regime pinned."""

    # flag OFF -> disabled regardless of regime
    monkeypatch.delenv("CLIO_LIVE_EDGE_STREAMING", raising=False)
    assert live_edge_enabled(_FakeApp(arc, REGIME_ATOMS), "s") is False

    monkeypatch.setenv("CLIO_LIVE_EDGE_STREAMING", "1")
    # flag ON + atoms regime -> enabled
    assert live_edge_enabled(_FakeApp(arc, REGIME_ATOMS), "s") is True
    # flag ON but LEGACY regime -> disabled (the seal is the S5 atom; nothing to attach to)
    assert live_edge_enabled(_FakeApp(arc, REGIME_LEGACY), "s") is False
    # flag ON, atoms regime, but NO canonical log (no ARC) -> disabled (capability gate)
    assert live_edge_enabled(_FakeApp(None, REGIME_ATOMS), "s") is False


def test_overlay_fills_open_part_in_place_when_enabled(arc: ARCMemory, monkeypatch: Any) -> None:
    """Under the live edge, the in-flight open part's empty text is coalesced in place."""

    monkeypatch.setenv("CLIO_LIVE_EDGE_STREAMING", "1")
    app = _FakeApp(arc, REGIME_ATOMS)
    reg = registry_for(app)
    reg.open_slot("s", part_id="p1", agent_id="main", field="answer", kind="text")
    reg.append_delta("s", "p1", "coalesced so far", store=arc._segments)

    parts = [Part(id="p1", type="text", agent_id="main", text="")]  # open part, empty text
    out = overlay_in_flight_part(app, "s", parts)
    assert out[0].text == "coalesced so far"
    assert out[0].metadata.get("live_edge") is True
    # The input part is NOT mutated (a copy is overlaid).
    assert parts[0].text == ""


def test_overlay_is_noop_when_disabled(arc: ARCMemory, monkeypatch: Any) -> None:
    """Flag OFF -> the overlay returns the parts unchanged (byte-identical default)."""

    monkeypatch.delenv("CLIO_LIVE_EDGE_STREAMING", raising=False)
    app = _FakeApp(arc, REGIME_ATOMS)
    reg = registry_for(app)
    reg.open_slot("s", part_id="p1", agent_id="main", field="answer", kind="text")
    reg.append_delta("s", "p1", "would-be edge", store=arc._segments)
    parts = [Part(id="p1", type="text", agent_id="main", text="")]
    out = overlay_in_flight_part(app, "s", parts)
    assert out is parts  # untouched
    assert out[0].text == ""


def test_overlay_does_not_clobber_already_closed_part(arc: ARCMemory, monkeypatch: Any) -> None:
    """A part that already carries text (closed) is never overwritten by the edge."""

    monkeypatch.setenv("CLIO_LIVE_EDGE_STREAMING", "1")
    app = _FakeApp(arc, REGIME_ATOMS)
    reg = registry_for(app)
    reg.open_slot("s", part_id="p1", agent_id="main", field="answer", kind="text")
    reg.append_delta("s", "p1", "partial", store=arc._segments)
    parts = [Part(id="p1", type="text", agent_id="main", text="the closed final text")]
    out = overlay_in_flight_part(app, "s", parts)
    assert out[0].text == "the closed final text"


def test_seal_and_settle_pairs_finalized_part(arc: ARCMemory, monkeypatch: Any) -> None:
    """``seal_and_settle`` seals the slot against the matching finalized part + cleans up."""

    monkeypatch.setenv("CLIO_LIVE_EDGE_STREAMING", "1")
    app = _FakeApp(arc, REGIME_ATOMS)
    reg = registry_for(app)
    reg.open_slot("s", part_id="p1", agent_id="main", field="answer", kind="text")
    reg.append_delta("s", "p1", "final answer", store=arc._segments)
    final_parts = [Part(id="p1", type="text", agent_id="main", text="final answer")]
    seal_and_settle(app, "s", final_parts)
    assert reg.current_slot("s") is None  # sealed + settled


# --------------------------------------------------------------------------- #
# Live-server integration — a REAL streamed turn under atoms + live edge
# --------------------------------------------------------------------------- #


def test_live_edge_integration_streamed_turn(tmp_path: Path, monkeypatch: Any) -> None:
    """A streamed turn under atoms + live edge: reload==live holds; ONE atom per part.

    Drives a real streaming turn (deltas flow through ``emit_chunk`` -> the live-edge
    feed). Proves (1) the persisted transcript is byte-equal reloaded from the canonical
    log (``reload == live`` unchanged — the memory win is not regressed), and (2) the
    streamed text part costs exactly ONE sealed ``_events/m`` atom, not one per token
    (the write-amplification bound holds end-to-end).
    """

    from fastapi.testclient import TestClient

    from clio_agent.gact.app import build_app
    from clio_agent.gact.part_atoms import load_message_part_atoms
    from tests.equivalence import normalizers as N
    from tests.test_gact.conftest import complete_turn
    from tests.test_gact.test_post_messages import FakeClioAgent, FakePrediction

    async def fake_streamed_forward(
        app: Any, enriched_text: str, sid: str, emit_chunk: Any, **kwargs: Any
    ) -> Any:
        del app, enriched_text, sid, kwargs
        for chunk in ["Los ", "Angeles ", "has ", "dense ", "seismic ", "stations."]:
            await emit_chunk(chunk)
        return FakePrediction(answer="Los Angeles has dense seismic stations.")

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_streamed_forward)
    monkeypatch.setenv("CLIO_TRANSCRIPT_PROJECTION", "1")  # S5 atoms regime
    monkeypatch.setenv("CLIO_LIVE_EDGE_STREAMING", "1")  # S7 live edge

    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    agent = FakeClioAgent(answer="unused")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent, arc=arc)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "s7"}).json()["id"]
        assistant = complete_turn(client, sid, "how many stations near LA?")

        # The streamed answer landed verbatim (coalesced), stream_source live.
        text_parts = [p for p in assistant["parts"] if p["type"] == "text"]
        assert text_parts[0]["text"] == "Los Angeles has dense seismic stations."

        live = [m.model_dump(exclude_none=True) for m in app.state.messages.get(sid, [])]
        # Evict + reload from the canonical log: reload == live (byte-equal under §4.1.A).
        app.state.messages.clear()
        reloaded = [m.model_dump(exclude_none=True) for m in app.state.messages.get(sid, [])]
        report = N.diff_persistence(live, reloaded)
        assert report.empty, f"reload != live under live edge:\n{report.pretty()}"

        # The write-amplification bound END-TO-END: the streamed text part is exactly ONE
        # sealed atom on the canonical log, not one per token (6 deltas, 39 chars).
        groups = load_message_part_atoms(arc, sid)
        assistant_id = assistant["id"]
        text_atoms = [
            a
            for a in groups.get(assistant_id, [])
            if a.get("atom_role") == "part" and a.get("kind") == "text"
        ]
        assert len(text_atoms) == 1, f"expected 1 sealed text atom, got {len(text_atoms)}"
