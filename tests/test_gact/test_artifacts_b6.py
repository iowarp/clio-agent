"""B6 (#980): the lease-window → ``fence_proven`` per-edge upgrade + fleet territory.

Covers, all unit/integration-runnable everywhere (no real fence needed — the sandbox state is
INJECTED via ``current_state``):

* the PURE exclusivity set-math predicate (:func:`fence_proves_exclusivity`) — disjoint vs
  overlapping vs granted-shared roots, and the two boundary cases;
* the mint-time wiring: a generated edge carries ``fence_proven`` on a (faked) active fence
  when the territory is exclusive, plain ``lease-window`` (``fence_proven=False``) on the floor;
* ``contended`` preservation — two concurrent actors on the shared workspace stay contended
  and are NEVER stamped ``fence_proven`` (the fence narrows exclusivity, never fakes it);
* the integration seam — a fleet server's undeclared-but-in-territory (designated) write's
  correlated generated edge carries ``fence_proven`` on the fenced tier, plain on the floor.

Each key lock carries a sabotage note (the neutralization that turns it red).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from clio_agent.gact.artifacts.minting import mint_tool_declared_outputs
from clio_agent.gact.artifacts.transform_exclusivity import generated_fence_proven
from clio_agent.gact.artifacts.transform_types import (
    EdgeEvidence,
    EdgeRole,
    ProvEdge,
    TransformKind,
    fence_proves_exclusivity,
)
from clio_agent.gact.artifacts.transforms import record_transform
from clio_agent.gact.sessions import SessionStore
from clio_agent.runtime import sandbox as sb


class _CapturingArc:
    def __init__(self) -> None:
        self.events: list[object] = []

    def record_semantic_event(self, event: object) -> object:
        self.events.append(event)
        return event


class _FakeWorkspaces:
    def __init__(self, roots: dict[str, str]) -> None:
        self._roots = roots

    def get(self, wid: str) -> object:
        root = self._roots.get(wid)
        return SimpleNamespace(root_path=root) if root else None


def _make_app(tmp_path: Path, *, extra_ws: dict[str, str] | None = None):
    store = SessionStore(path=tmp_path / "sessions.json")
    sess = store.create(workspace_id="ws1", title="t")
    arc = _CapturingArc()
    roots = {"ws1": str(tmp_path)}
    roots.update(extra_ws or {})
    state = SimpleNamespace(
        sessions=store,
        arc=arc,
        workspaces=_FakeWorkspaces(roots),
        semantic_event_sink=object(),
        semantic_trace_detail_level="semantic",
        semantic_trace_backend=None,
        artifact_registry=None,
        in_flight_turns={},
    )
    return SimpleNamespace(state=state), sess, store, arc


def _fence(mechanism: str = sb.MECHANISM_SRT_BWRAP) -> sb.SandboxResult:
    return sb.SandboxResult(mechanism=mechanism, active=True, reason=sb.REASON_FENCE_ACTIVE)


def _floor() -> sb.SandboxResult:
    return sb.SandboxResult(
        mechanism=sb.MECHANISM_NONE, active=False, reason=sb.REASON_SRT_NOT_INSTALLED
    )


# --------------------------------------------------------------------------- #
# 1. The pure exclusivity set-math (the fence_proven upgrade predicate).
# --------------------------------------------------------------------------- #


def test_exclusivity_no_other_actors_is_vacuously_true() -> None:
    # Only this fenced actor can write here → exclusive by construction.
    # Sabotage: return False for empty other_actor_roots → red.
    assert fence_proves_exclusivity(["/ws/a"], []) is True


def test_exclusivity_disjoint_territories_proven() -> None:
    assert fence_proves_exclusivity(["/ws/a"], [["/ws/b", "/tmp/cache"]]) is True


def test_exclusivity_overlap_not_proven() -> None:
    # A concurrent actor whose root CONTAINS our output territory → not exclusive.
    # Sabotage: only check equality (drop the relative_to containment) → red.
    assert fence_proves_exclusivity(["/ws/a/out"], [["/ws/a"]]) is False
    assert fence_proves_exclusivity(["/ws/a"], [["/ws/a/out"]]) is False


def test_exclusivity_granted_shared_root_is_contended_not_proven() -> None:
    # B5 grant to BOTH actors of the same shared root → the fence NARROWS, never FAKES.
    shared = "/shared/grant"
    assert fence_proves_exclusivity([shared], [["/ws/b", shared]]) is False


def test_exclusivity_empty_output_roots_is_false() -> None:
    # Nothing to prove exclusive → never a vacuous fence_proven on an empty output set.
    assert fence_proves_exclusivity([], [["/ws/b"]]) is False
    assert fence_proves_exclusivity(["  "], []) is False


# --------------------------------------------------------------------------- #
# 2. The mint-time wiring: generated_fence_proven.
# --------------------------------------------------------------------------- #


def test_generated_fence_proven_true_under_active_fence_single_actor(tmp_path, monkeypatch):
    app, sess, _store, _arc = _make_app(tmp_path)
    monkeypatch.setattr(sb, "current_state", lambda: _fence())
    # Ordinary record (no concurrent peer) + active fence + no overlapping actor → proven.
    assert generated_fence_proven(app, "ws1", sess.id, kind=TransformKind.ORDINARY) is True


def test_generated_fence_proven_false_on_floor(tmp_path, monkeypatch):
    app, sess, _store, _arc = _make_app(tmp_path)
    monkeypatch.setattr(sb, "current_state", lambda: _floor())
    # Floor tier: exclusivity is only correlated, never proven.
    # Sabotage: drop the `not state.active` guard → red.
    assert generated_fence_proven(app, "ws1", sess.id, kind=TransformKind.ORDINARY) is False


def test_generated_fence_proven_false_when_peer_enumeration_races(tmp_path, monkeypatch):
    """A concurrent mutation of in_flight_turns must fail SAFE to lease-window (review finding).

    ``list(in_flight.keys())`` racing a mutation raises ``RuntimeError`` — peers EXIST but
    cannot be enumerated (maximal ambiguity). That must NEVER resolve to ``fence_proven`` (a
    false single-writer proof); the outer guard fails safe to plain lease-window. Sabotage:
    swallow the RuntimeError into an empty peer list → this reddens (the pre-fix defect).
    """
    app, sess, _store, _arc = _make_app(tmp_path)
    monkeypatch.setattr(sb, "current_state", lambda: _fence())

    class _RacingMap(dict):
        def keys(self):  # noqa: D401 - a live map mutated mid-iteration
            raise RuntimeError("dictionary changed size during iteration")

    app.state.in_flight_turns = _RacingMap({sess.id: object(), "sess_peer": object()})
    assert generated_fence_proven(app, "ws1", sess.id, kind=TransformKind.ORDINARY) is False


def test_generated_fence_proven_false_when_contended(tmp_path, monkeypatch):
    app, sess, _store, _arc = _make_app(tmp_path)
    monkeypatch.setattr(sb, "current_state", lambda: _fence())
    # A contended record rides a candidate set — never stamped fence_proven, even fenced.
    # Sabotage: drop the `kind is not ORDINARY` early return → red.
    assert generated_fence_proven(app, "ws1", sess.id, kind=TransformKind.CONTENDED) is False


def test_generated_fence_proven_false_when_no_state_resolved(tmp_path, monkeypatch):
    app, sess, _store, _arc = _make_app(tmp_path)
    monkeypatch.setattr(sb, "current_state", lambda: None)
    assert generated_fence_proven(app, "ws1", sess.id, kind=TransformKind.ORDINARY) is False


def test_generated_fence_proven_false_when_peer_actor_shares_broad_root(tmp_path, monkeypatch):
    # A concurrent DIFFERENT-workspace actor whose fence territory (advisory roots) CONTAINS
    # our workspace → the fence cannot prove exclusivity → plain lease-window. Point the
    # advisory base at the shared tmp parent so the peer's write_roots subsume ws1.
    parent = tmp_path
    ws1 = parent / "ws1"
    ws1.mkdir()
    ws2 = parent / "ws2"
    ws2.mkdir()
    app, sess, store, _arc = _make_app(parent, extra_ws={"ws1": str(ws1), "ws2": str(ws2)})
    # rebind ws1 session's workspace root to the child dir
    peer = store.create(workspace_id="ws2", title="peer")
    app.state.in_flight_turns = {sess.id: object(), peer.id: object()}
    monkeypatch.setattr(sb, "current_state", lambda: _fence())
    # Force the advisory base (peer's effective_write_roots) to include the shared parent.
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(parent))
    # ws1 is contended? No — different workspace. But the peer's territory (parent) contains
    # ws1 → set-math denies exclusivity. Sabotage: ignore other_actor_roots → red.
    assert generated_fence_proven(app, "ws1", sess.id, kind=TransformKind.ORDINARY) is False


# --------------------------------------------------------------------------- #
# 3. End-to-end through record_transform: the generated edge carries the marker.
# --------------------------------------------------------------------------- #


def _record_with_output(app, sess, tmp_path: Path, call_id: str):
    out_path = tmp_path / f"{call_id}.png"
    out_path.write_bytes(b"\x89PNG plot bytes")
    minted = mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="plot",
        effective_args={"output_path": str(out_path)},
        call_id=call_id,
        workspace_id="ws1",
    )
    assert minted, "the designated output should mint a generated version"
    return record_transform(
        app,
        sess.id,
        tool_name="plot",
        args={"output_path": str(out_path)},
        call_id=call_id,
        ok=True,
        result=None,
        minted=minted,
        workspace_id="ws1",
    )


def test_generated_edge_fence_proven_on_fenced_tier(tmp_path, monkeypatch):
    app, sess, _store, _arc = _make_app(tmp_path)
    monkeypatch.setattr(sb, "current_state", lambda: _fence())
    rec = _record_with_output(app, sess, tmp_path, "call_fenced")
    assert rec is not None and rec.kind is TransformKind.ORDINARY
    assert rec.generated, "a designated output edge should exist"
    # Sabotage: pass fence_proven=False unconditionally in _generated_edges → red.
    assert all(e.fence_proven for e in rec.generated)


def test_generated_edge_plain_lease_window_on_floor(tmp_path, monkeypatch):
    app, sess, _store, _arc = _make_app(tmp_path)
    monkeypatch.setattr(sb, "current_state", lambda: _floor())
    rec = _record_with_output(app, sess, tmp_path, "call_floor")
    assert rec is not None and rec.generated
    # Floor: correlated only — never proven. Sabotage: drop the active guard → red.
    assert all(e.fence_proven is False for e in rec.generated)


def test_contended_record_generated_edge_never_fence_proven(tmp_path, monkeypatch):
    app, sess, store, _arc = _make_app(tmp_path)
    # A concurrent peer on the SAME workspace → contended record.
    peer = store.create(workspace_id="ws1", title="peer")
    app.state.in_flight_turns = {sess.id: object(), peer.id: object()}
    monkeypatch.setattr(sb, "current_state", lambda: _fence())
    rec = _record_with_output(app, sess, tmp_path, "call_contended")
    assert rec is not None and rec.kind is TransformKind.CONTENDED
    assert rec.candidates, "the contended candidate set should name the peer"
    # The fence narrows exclusivity, never fakes it: no generated edge is fence_proven.
    assert all(e.fence_proven is False for e in rec.generated)


# --------------------------------------------------------------------------- #
# 4. The ProvEdge field round-trips through the durable payload.
# --------------------------------------------------------------------------- #


def test_fence_proven_round_trips_through_payload() -> None:
    edge = ProvEdge(role=EdgeRole.GENERATED, evidence=EdgeEvidence.HASH_PAIR, fence_proven=True)
    dumped = edge.model_dump()
    assert dumped["fence_proven"] is True
    restored = ProvEdge.model_validate(dumped)
    assert restored.fence_proven is True
    # A legacy payload with no key defaults to False (no silent upgrade).
    legacy = ProvEdge.model_validate({"role": "generated", "evidence": "hash-pair"})
    assert legacy.fence_proven is False
