"""P3.1 (#1038) — cross-job lineage bind unit tests.

A producer that ran under a SEPARATE top-level job (a DIFFERENT ``workspace_id``)
sharing this job's ``root_path`` must become visible to the used-edge detector: the
bound edge REUSES the foreign version's ``artifact_id`` (never a minted local id),
carries the typed ``cross_workspace_bind=True`` marker, an EDITED cross-job input
binds as a ``revision_of`` (auto_revision) on the FOREIGN chain (not a new local
artifact), the cross-record tie-break is a DETERMINISTIC total order, and the
lineage walk recurses across the boundary. The bind is gated to the contributing
set (``root_path``-equality) so an unrelated tenant never binds.

Each key lock carries a sabotage note: the referenced neutralization turns the named
assertion red, proving the test binds the invariant (not a tautology).
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from clio_agent.gact.artifacts.lineage import build_lineage
from clio_agent.gact.artifacts.minting import mint_tool_declared_outputs
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.artifacts.transform_edges import (
    contributing_workspace_ids,
    detect_used_edges,
)
from clio_agent.gact.artifacts.transform_types import EdgeEvidence
from clio_agent.gact.artifacts.transforms import record_transform
from clio_agent.gact.sessions import SessionStore

# --------------------------------------------------------------------------- #
# Fakes (parity with the S5 harness).
# --------------------------------------------------------------------------- #


class _CapturingArc:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def record_semantic_event(self, event: Any) -> Any:
        self.events.append(event)
        return event


class _FakeWorkspaces:
    def __init__(self, roots: dict[str, str]) -> None:
        self._roots = roots

    def get(self, wid: str) -> Any:
        root = self._roots.get(wid)
        return SimpleNamespace(id=wid, root_path=root) if root else None

    def list(self) -> list[Any]:
        return [SimpleNamespace(id=wid, root_path=root) for wid, root in self._roots.items()]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_cross_app(tmp_path: Path, ws_ids, roots=None):
    """App whose workspaces (``ws_ids``) share ``tmp_path`` as root by default.

    ``roots`` overrides individual workspace roots (the tenant-isolation case — a
    workspace at a DIFFERENT root_path is outside the contributing set).
    """
    store = SessionStore(path=tmp_path / "sessions.json")
    roots = roots or {wid: str(tmp_path) for wid in ws_ids}
    sessions = {wid: store.create(workspace_id=wid, title=wid) for wid in ws_ids}
    arc = _CapturingArc()
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
    return SimpleNamespace(state=state), sessions, arc


def _register_under(app, sid, tmp_path: Path, name: str, data: bytes, call_id: str, ws: str):
    """Mint a tool-declared output under ``ws`` so ``name`` exists at its path there."""
    path = tmp_path / name
    path.write_bytes(data)
    minted = mint_tool_declared_outputs(
        app,
        sid,
        tool_name="producer",
        effective_args={"output_path": str(path)},
        call_id=call_id,
        workspace_id=ws,
    )
    assert minted, "fixture mint should register the file"
    return path, minted[0]


# --------------------------------------------------------------------------- #
# 1. Path resolution + the tenant-isolation gate.
# --------------------------------------------------------------------------- #


def test_find_version_by_path_resolves_foreign_producer_in_contributing_set(tmp_path):
    app, sess, _arc = _make_cross_app(tmp_path, ["ws1", "ws2"])
    path, v = _register_under(app, sess["ws1"].id, tmp_path, "a.csv", b"rows\n", "call_a", "ws1")
    reg = get_registry(app)
    # From the CONSUMING job (ws2), the foreign producer (ws1) resolves ONLY when ws1
    # is in the contributing set. Sabotage: drop the allowed-set branch -> None -> red.
    hit = reg.find_version_by_path("ws2", str(path), allowed_workspace_ids={"ws1", "ws2"})
    assert hit is not None
    rec, ver = hit
    assert rec.workspace_id == "ws1" and ver.artifact_id == v.artifact_id
    # A set that EXCLUDES ws1 does not bind (the tenant-isolation gate).
    assert reg.find_version_by_path("ws2", str(path), allowed_workspace_ids={"ws2"}) is None
    # The default (None) stays same-workspace-only — a drop-in for existing callers.
    assert reg.find_version_by_path("ws2", str(path)) is None


def test_contributing_set_excludes_workspace_at_a_different_root(tmp_path):
    other = tmp_path / "other_tenant"
    other.mkdir()
    app, _sess, _arc = _make_cross_app(
        tmp_path,
        ["ws2", "ws_same", "ws_far"],
        roots={"ws2": str(tmp_path), "ws_same": str(tmp_path), "ws_far": str(other)},
    )
    ids = contributing_workspace_ids(app, "ws2")
    # Only workspaces sharing ws2's root_path contribute; a different-root tenant is out.
    # Sabotage: match on basename / drop the root_path filter -> ws_far leaks in -> red.
    assert ids == {"ws2", "ws_same"}


# --------------------------------------------------------------------------- #
# 2. The bound edge — foreign id reuse, marker, revision-on-change.
# --------------------------------------------------------------------------- #


def test_cross_job_clean_input_binds_foreign_id_with_marker(tmp_path):
    app, sess, _arc = _make_cross_app(tmp_path, ["ws1", "ws2"])
    path, v = _register_under(app, sess["ws1"].id, tmp_path, "a.csv", b"rows\n", "call_a", "ws1")
    # The consuming job (ws2) uses the UNCHANGED foreign file -> clean hash-pair on the
    # FOREIGN artifact_id, flagged cross_workspace_bind (never a new/minted local id).
    scan = detect_used_edges(
        app,
        sess["ws2"].id,
        args={"data_path": str(path)},
        workspace_id="ws2",
        turn_id="",
        trace_id="",
        allowed_workspace_ids={"ws1", "ws2"},
    )
    assert len(scan.edges) == 1
    e = scan.edges[0]
    # Sabotage: pass workspace_id (local) instead of record.workspace_id, or drop the
    # cross_workspace_bind stamp -> these go red.
    assert e.evidence is EdgeEvidence.HASH_PAIR
    assert e.artifact_id == v.artifact_id
    assert e.cross_workspace_bind is True
    assert e.sha256 == _sha(b"rows\n")


def test_cross_job_edited_input_binds_as_revision_not_new_artifact(tmp_path):
    from clio_agent.gact import tool_observer

    app, sess, _arc = _make_cross_app(tmp_path, ["ws1", "ws2"])
    path, v1 = _register_under(app, sess["ws1"].id, tmp_path, "a.csv", b"v1", "call_a", "ws1")
    # The file is EDITED after registration; a clean single-writer lease (an active
    # observer call, no other writer) makes the reconcile an auto-revision.
    path.write_bytes(b"v2-edited")
    prev_t0 = getattr(tool_observer._OBSERVER_CALL_T0, "value", None)
    tool_observer._OBSERVER_CALL_T0.value = time.time() - 1000.0
    try:
        scan = detect_used_edges(
            app,
            sess["ws2"].id,
            args={"data_path": str(path)},
            workspace_id="ws2",
            turn_id="",
            trace_id="",
            allowed_workspace_ids={"ws1", "ws2"},
        )
    finally:
        tool_observer._OBSERVER_CALL_T0.value = prev_t0
    assert len(scan.edges) == 1
    e = scan.edges[0]
    # An edited cross-job input REVISES the foreign chain (auto_revision), reusing its
    # identity — NOT a fresh local artifact. Sabotage: reconcile under the LOCAL ws ->
    # registry.get(ws2, name) is None -> stale_fallback (or a new v1 fork) -> red.
    assert e.note == "auto_revision"
    assert e.cross_workspace_bind is True
    assert e.version == 2 and e.artifact_id != v1.artifact_id
    reg = get_registry(app)
    # The revision landed on the FOREIGN (ws1) chain, and NO local ws2 record was forged.
    ws1_rec = reg.get("ws1", "a.csv")
    assert ws1_rec is not None and {v.version for v in ws1_rec.versions} == {1, 2}
    v2 = next(v for v in ws1_rec.versions if v.version == 2)
    assert v2.prior_version == 1 and v2.artifact_id == e.artifact_id
    assert reg.get("ws2", "a.csv") is None
    # One-id-per-version: the reused foreign id resolves to exactly that version.
    assert reg.get_by_artifact_id(e.artifact_id) == (ws1_rec, v2)


# --------------------------------------------------------------------------- #
# 3. Deterministic tie-break + lineage recursion across the boundary.
# --------------------------------------------------------------------------- #


def test_cross_job_tie_break_is_deterministic_total_order(tmp_path):
    app, sess, _arc = _make_cross_app(tmp_path, ["ws1", "ws3", "ws2"])
    path, _va = _register_under(app, sess["ws1"].id, tmp_path, "a.csv", b"rows\n", "call_a", "ws1")
    # ws1 EDITS a.csv -> v2: a HIGHER version but (minted earlier) an OLDER created_at than
    # ws3's v1 below. This forces version-order and created_at-order to DIVERGE, so a naive
    # HEAD-wins-BY-VERSION pick (which would take ws1 v2) differs from the correct total-order
    # winner (newest created_at = ws3 v1) — giving the tie-break assertion real discriminating power.
    path.write_bytes(b"rows-edited\n")
    minted_a2 = mint_tool_declared_outputs(
        app,
        sess["ws1"].id,
        tool_name="producer",
        effective_args={"output_path": str(path)},
        call_id="call_a2",
        workspace_id="ws1",
    )
    assert minted_a2 and minted_a2[0].version == 2
    # A SECOND foreign job registers the SAME path LAST -> newest created_at, but only v1.
    path.write_bytes(b"rows-ws3\n")
    minted_b = mint_tool_declared_outputs(
        app,
        sess["ws3"].id,
        tool_name="producer",
        effective_args={"output_path": str(path)},
        call_id="call_b",
        workspace_id="ws3",
    )
    assert minted_b
    reg = get_registry(app)
    allowed = {"ws1", "ws3", "ws2"}
    cands = [
        reg.find_version_by_path("ws1", str(path)),
        reg.find_version_by_path("ws3", str(path)),
    ]
    cands = [c for c in cands if c is not None]
    expected = max(
        cands,
        key=lambda c: (
            c[0].workspace_id == "ws2",
            c[1].created_at or "",
            c[0].workspace_id,
            c[0].name,
            c[1].version,
        ),
    )
    # The fixture MUST diverge: the total-order winner is ws3 (newest created_at, v1), NOT ws1
    # (v2, the highest version but older) — else the assertion below wouldn't discriminate.
    assert expected[0].workspace_id == "ws3", "fixture must make version-order != created_at-order"
    got = reg.find_version_by_path("ws2", str(path), allowed_workspace_ids=allowed)
    # Sabotage: a naive HEAD-wins-BY-VERSION pick returns ws1 v2 -> != expected (ws3 v1) -> red.
    assert got is not None and got[1].artifact_id == expected[1].artifact_id
    assert got[0].workspace_id == "ws3", (
        "newest-created_at foreign record wins, not highest-version"
    )
    # Local preference wins outright: register the path under the LOCAL ws2 -> it must win
    # regardless of created_at ordering against the foreign records.
    _p, vlocal = _register_under(
        app, sess["ws2"].id, tmp_path, "a.csv", b"local\n", "call_l", "ws2"
    )
    local_hit = reg.find_version_by_path("ws2", str(path), allowed_workspace_ids=allowed)
    assert local_hit is not None and local_hit[0].workspace_id == "ws2"
    assert local_hit[1].artifact_id == vlocal.artifact_id


def test_lineage_recurses_across_the_cross_job_boundary(tmp_path):
    app, sess, _arc = _make_cross_app(tmp_path, ["ws1", "ws2"])
    # JOB A (ws1): produce a.csv.
    a_path, a_v = _register_under(app, sess["ws1"].id, tmp_path, "a.csv", b"raw\n", "gen_a", "ws1")
    record_transform(
        app,
        sess["ws1"].id,
        tool_name="make_a",
        args={"output_path": str(a_path)},
        call_id="gen_a",
        ok=True,
        result=None,
        minted=[a_v],
        workspace_id="ws1",
    )
    # JOB B (ws2, same root): b.png = transform(a.csv). record_transform computes the
    # contributing set itself and binds the foreign a.csv.
    b_path = tmp_path / "b.png"
    b_path.write_bytes(b"\x89PNG")
    b_minted = mint_tool_declared_outputs(
        app,
        sess["ws2"].id,
        tool_name="plot",
        effective_args={"output_path": str(b_path)},
        call_id="gen_b",
        workspace_id="ws2",
    )
    assert b_minted
    b_rec = record_transform(
        app,
        sess["ws2"].id,
        tool_name="plot",
        args={"data_path": str(a_path), "output_path": str(b_path)},
        call_id="gen_b",
        ok=True,
        result=None,
        minted=b_minted,
        workspace_id="ws2",
    )
    assert b_rec is not None
    used = [e for e in b_rec.used if e.artifact_id]
    # The cross-job used edge carries the FOREIGN a artifact_id + the marker.
    assert used and used[0].artifact_id == a_v.artifact_id and used[0].cross_workspace_bind is True
    graph = build_lineage(get_registry(app), b_minted[0].artifact_id, direction="upstream", depth=5)
    assert graph is not None
    node_ids = {n["id"] for n in graph["nodes"]}
    # Sabotage: leave the bound edge without a real artifact_id (external leaf) -> the walk
    # stops at b's activity and never reaches a / activity:gen_a -> these go red.
    assert a_v.artifact_id in node_ids
    assert "activity:gen_a" in node_ids
