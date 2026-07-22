"""Unit + route tests for S5 artifacts (#966/#971) — TransformRecords, environment
tiers, used-edge capture, lineage queries, relay convergence.

Covers: the transform projection from synthetic logs (fold + idempotency for the new
``artifact.transform.recorded`` event); the FULL used-edge matrix (match/hash-pair,
changed→gap-first, external, over-threshold stat-pinned, non-path no-edge); the
environment tier computation + the reproducible/re-runnable replay labeling; failed-
transform recording; lineage both directions incl. depth/truncation; NDP authority-
asserted identity; and the relay ``ArtifactUse``/``ArtifactRef`` shape-compat against
the REAL clio-relay models (sibling checkout).

Each key lock carries a sabotage note: the referenced neutralization turns the named
assertion red, proving the test binds the invariant (not a tautology).
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.environment import (
    EnvironmentRecord,
    EnvironmentTier,
    capture_environment,
    tier_at_least,
)
from clio_agent.gact.artifacts.lineage import build_lineage
from clio_agent.gact.artifacts.minting import mint_tool_declared_outputs
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.artifacts.transforms import (
    ARTIFACT_TRANSFORM_RECORDED_EVENT,
    EdgeEvidence,
    EdgeRole,
    ProvEdge,
    ReplayContract,
    TransformStatus,
    _detect_used_edges,
    compute_replay_contract,
    observe_tool_transform,
    record_transform,
    transform_from_payload,
)
from clio_agent.gact.semantic_events import event_reaches_ui
from clio_agent.gact.sessions import SessionStore

# --------------------------------------------------------------------------- #
# Fakes (parity with the S1-S4 harness).
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
        return SimpleNamespace(root_path=root) if root else None


def _make_app(tmp_path: Path):
    store = SessionStore(path=tmp_path / "sessions.json")
    sess = store.create(workspace_id="ws1", title="t")
    arc = _CapturingArc()
    state = SimpleNamespace(
        sessions=store,
        arc=arc,
        workspaces=_FakeWorkspaces({"ws1": str(tmp_path)}),
        semantic_event_sink=object(),
        semantic_trace_detail_level="semantic",
        semantic_trace_backend=None,
        artifact_registry=None,
        in_flight_turns={},
    )
    return SimpleNamespace(state=state), sess, arc


@pytest.fixture(autouse=True)
def _reset_observer_call_stamp():
    from clio_agent.gact import tool_observer

    tool_observer._OBSERVER_CALL_T0.value = None
    yield
    tool_observer._OBSERVER_CALL_T0.value = None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _events(arc: _CapturingArc, event_type: str) -> list[Any]:
    return [e for e in arc.events if getattr(e, "event_type", "") == event_type]


def _register_file(app, sess, tmp_path: Path, name: str, data: bytes, call_id: str):
    """Mint a tool-declared output so ``name`` exists in the registry at its path."""
    path = tmp_path / name
    path.write_bytes(data)
    minted = mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="producer",
        effective_args={"output_path": str(path)},
        call_id=call_id,
        workspace_id="ws1",
    )
    assert minted, "fixture mint should register the file"
    return path, minted[0]


# --------------------------------------------------------------------------- #
# 1. Environment tier + replay labeling (item 2).
# --------------------------------------------------------------------------- #


def test_environment_tier_lockfile_hash_when_uv_lock_present(tmp_path):
    app, _sess, _arc = _make_app(tmp_path)
    env = capture_environment(app)
    # The repo ships uv.lock, so the tier is lockfile-hash with a real digest.
    # Sabotage: make _lockfile_hash() return "" -> tier falls to declared -> red.
    assert env.tier is EnvironmentTier.LOCKFILE_HASH
    assert len(env.lockfile_sha256) == 64
    assert env.clio_version and env.os and env.python_version


def test_environment_declared_when_lockfile_unreachable(monkeypatch, tmp_path):
    import clio_agent.gact.artifacts.environment as env_mod

    monkeypatch.setattr(env_mod, "_lockfile_hash", lambda: "")
    app, _sess, _arc = _make_app(tmp_path)
    env = capture_environment(app)
    assert env.tier is EnvironmentTier.DECLARED
    assert env.lockfile_sha256 == ""


def test_replay_reproducible_requires_tier_and_pinned_inputs():
    env = EnvironmentRecord(tier=EnvironmentTier.LOCKFILE_HASH, lockfile_sha256="a" * 64)
    used = [
        ProvEdge(
            role=EdgeRole.USED,
            evidence=EdgeEvidence.HASH_PAIR,
            artifact_id="artifact_x",
            sha256="b" * 64,
        )
    ]
    contract, reason = compute_replay_contract(env, used)
    # Sabotage: return RE_RUNNABLE unconditionally -> red.
    assert contract is ReplayContract.REPRODUCIBLE
    assert reason == ""


def test_replay_rerunnable_when_env_below_lockfile():
    env = EnvironmentRecord(tier=EnvironmentTier.DECLARED)
    contract, reason = compute_replay_contract(env, [])
    assert contract is ReplayContract.RE_RUNNABLE
    assert reason == "env_below_lockfile_hash"


def test_replay_rerunnable_when_an_input_is_unpinned():
    env = EnvironmentRecord(tier=EnvironmentTier.LOCKFILE_HASH, lockfile_sha256="a" * 64)
    # A stat-pinned used edge (no sha, schema-arg only) is NOT pinned.
    used = [
        ProvEdge(role=EdgeRole.USED, evidence=EdgeEvidence.SCHEMA_ARG, external_ref="external:/x")
    ]
    contract, reason = compute_replay_contract(env, used)
    # Sabotage: treat schema-arg-without-sha as pinned in _edge_pin_class -> red.
    assert contract is ReplayContract.RE_RUNNABLE
    assert reason.startswith("inputs_unpinned:")


def test_tier_order_total():
    assert tier_at_least(EnvironmentTier.LOCKFILE_HASH, EnvironmentTier.LOCKFILE_HASH)
    assert tier_at_least(EnvironmentTier.IMAGE_DIGEST, EnvironmentTier.LOCKFILE_HASH)
    assert not tier_at_least(EnvironmentTier.DECLARED, EnvironmentTier.LOCKFILE_HASH)


# --------------------------------------------------------------------------- #
# 2. The FULL used-edge matrix (item 3).
# --------------------------------------------------------------------------- #


def _detect(app, sess, args):
    return _detect_used_edges(
        app, sess.id, args=args, workspace_id="ws1", turn_id="", trace_id=""
    ).edges


def test_used_edge_match_hash_pair(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    path, version = _register_file(app, sess, tmp_path, "in.csv", b"rows\n", "call_p1")
    edges = _detect(app, sess, {"data_path": str(path)})
    # Sabotage: drop the disk_sha == version.sha256 branch -> evidence never hash-pair -> red.
    assert len(edges) == 1
    e = edges[0]
    assert e.evidence is EdgeEvidence.HASH_PAIR
    assert e.artifact_id == version.artifact_id
    assert e.sha256 == _sha(b"rows\n")
    assert e.role is EdgeRole.USED


def test_used_edge_changed_input_mints_gap_first(tmp_path):
    app, sess, arc = _make_app(tmp_path)
    path, version = _register_file(app, sess, tmp_path, "in.csv", b"v1", "call_p1")
    # The file changes on disk AFTER registration — the registered sha is now stale.
    path.write_bytes(b"v2-changed")
    edges = _detect(app, sess, {"data_path": str(path)})
    assert len(edges) == 1
    e = edges[0]
    # A NEW (gap) version was minted first; the edge points at it, never the stale v1.
    # No active observer call on this thread -> DIRTY lease -> a custody GAP (finding [3]:
    # the note reports the ACTUAL reconcile class, not an unconditional "gap_first").
    # Sabotage: in _matched_used_edge point the edge at `version` (the stale match) on
    # a hash mismatch instead of the reconcile outcome -> the id-inequality assertion goes red.
    assert e.note == "gap"
    assert e.artifact_id != version.artifact_id
    assert e.evidence is EdgeEvidence.HASH_PAIR
    # The gap version is a real second version in the chain.
    rec = get_registry(app).find_version_by_path("ws1", str(path))
    assert rec is not None and rec[1].version == 2


def test_used_edge_external_when_not_registered(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    q = tmp_path / "external.csv"
    q.write_bytes(b"unregistered")
    edges = _detect(app, sess, {"input": str(q)})
    assert len(edges) == 1
    e = edges[0]
    # Sabotage: register-match-or-nothing (drop the external branch) -> zero edges -> red.
    assert e.external_ref == f"external:{str(q.resolve())}"
    assert e.artifact_id == ""
    assert e.sha256 == _sha(b"unregistered")


def test_used_edge_over_threshold_stat_pinned(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIO_ARTIFACTS_HASH_MAX_FILE_BYTES", "1")
    app, sess, _arc = _make_app(tmp_path)
    path, version = _register_file(app, sess, tmp_path, "big.csv", b"way over one byte", "call_p1")
    edges = _detect(app, sess, {"data_path": str(path)})
    assert len(edges) == 1
    e = edges[0]
    # Over the hash threshold → stat-pinned, labeled (never a silent hash-skip).
    # Sabotage: return a hash-pair edge for an over-threshold file -> note assertion red.
    assert e.note == "over_threshold"
    assert e.sha256 is None
    assert e.evidence is EdgeEvidence.SCHEMA_ARG


def test_used_edge_none_for_non_path_and_output_args(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    edges = _detect(
        app,
        sess,
        {"mode": "stdout", "count": "42", "output_path": str(tmp_path / "out.png")},
    )
    # A non-path string, a numeric string, and an OUTPUT arg (generated side) → no edge.
    # Sabotage: stop excluding OUTPUT_PATH_ARG_NAMES -> the output path could mint an edge -> red.
    assert edges == []


def test_used_edge_none_for_path_outside_workspace(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    outside = tmp_path.parent / f"escape_{tmp_path.name}.csv"
    outside.write_bytes(b"outside")
    try:
        edges = _detect(app, sess, {"data_path": str(outside)})
        # Containment before any read (owner decision 10): an existing file OUTSIDE the
        # workspace root yields NO edge. Sabotage: drop the _contained check -> red.
        assert edges == []
    finally:
        outside.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# 3. Transform recording (item 1) — success, failure, generated edges.
# --------------------------------------------------------------------------- #


def test_record_transform_emits_trace_only_event_keyed_by_call_id(tmp_path):
    app, sess, arc = _make_app(tmp_path)
    in_path, _v = _register_file(app, sess, tmp_path, "in.csv", b"data", "call_prod")
    out_path = tmp_path / "out.png"
    out_path.write_bytes(b"\x89PNG plot")
    minted = mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="plot",
        effective_args={"output_path": str(out_path)},
        call_id="call_xform",
        workspace_id="ws1",
    )
    rec = record_transform(
        app,
        sess.id,
        tool_name="plot",
        args={"data_path": str(in_path), "output_path": str(out_path)},
        call_id="call_xform",
        ok=True,
        result=None,
        minted=minted,
        workspace_id="ws1",
    )
    assert rec is not None
    assert rec.call_id == "call_xform"
    assert rec.status is TransformStatus.SUCCESS
    # generated edge = the minted plot; used edge = the input csv (hash-pair).
    assert len(rec.generated) == 1 and rec.generated[0].artifact_id == minted[0].artifact_id
    used_ids = {e.artifact_id for e in rec.used}
    assert used_ids and any(e.evidence is EdgeEvidence.HASH_PAIR for e in rec.used)
    # The event is emitted, keyed, and TRACE-ONLY (never on the SSE UI wire).
    evs = _events(arc, ARTIFACT_TRANSFORM_RECORDED_EVENT)
    # Sabotage: skip _emit_transform_recorded -> zero events -> red.
    assert len(evs) == 1
    assert evs[0].payload["call_id"] == "call_xform"
    # Sabotage: add the event to SSE_UI_EVENT_TYPES -> this trace-only assertion red.
    assert event_reaches_ui(ARTIFACT_TRANSFORM_RECORDED_EVENT) is False


def test_failed_transform_is_recorded(tmp_path):
    app, sess, arc = _make_app(tmp_path)
    out_path = tmp_path / "partial.csv"
    out_path.write_bytes(b"partial output before failure")
    minted = mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="clean",
        effective_args={"output_path": str(out_path)},
        call_id="call_fail",
        workspace_id="ws1",
    )
    rec = record_transform(
        app,
        sess.id,
        tool_name="clean",
        args={"output_path": str(out_path)},
        call_id="call_fail",
        ok=False,
        result=None,
        minted=minted,
        workspace_id="ws1",
    )
    # A failed run that WROTE outputs is real provenance (owner decision #966.6).
    # Sabotage: gate record_transform on ok -> no record for a failed call -> red.
    assert rec is not None and rec.status is TransformStatus.FAILED
    assert len(rec.generated) == 1
    evs = _events(arc, ARTIFACT_TRANSFORM_RECORDED_EVENT)
    assert evs and evs[0].status == "failed"


def test_observe_tool_transform_end_to_end(tmp_path):
    app, sess, arc = _make_app(tmp_path)
    in_path, _v = _register_file(app, sess, tmp_path, "in.csv", b"input rows", "call_prod")
    out_path = tmp_path / "chart.png"
    out_path.write_bytes(b"\x89PNG chart")
    observe_tool_transform(
        app,
        sess.id,
        tool_name="plot",
        effective_args={"data_path": str(in_path), "output_path": str(out_path)},
        call_id="call_obs",
        ok=True,
        result=None,
    )
    reg = get_registry(app)
    rec = reg.get_transform("call_obs")
    assert rec is not None
    assert any(e.name == "chart.png" or e.path == str(out_path) for e in rec.generated)
    assert any(e.evidence is EdgeEvidence.HASH_PAIR for e in rec.used)


# --------------------------------------------------------------------------- #
# 4. Authority-asserted identity — NDP catalog inputs (item 4).
# --------------------------------------------------------------------------- #


def test_ndp_stage_resource_authority_edge(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    staged = tmp_path / "earthscope.csv"
    staged.write_bytes(b"catalog bytes")
    result = {
        "ok": True,
        "local_path": str(staged),
        "size_bytes": 13,
        "url": "https://nationaldataplatform.org/catalog/dataset/D/resource/R/download/earthscope.csv",
        "_meta": {"tool": "stage_resource", "status": "success"},
    }
    rec = record_transform(
        app,
        sess.id,
        tool_name="ndp.stage_resource",
        args={},
        call_id="call_ndp",
        ok=True,
        result=result,
        minted=[],
        workspace_id="ws1",
    )
    assert rec is not None
    auth = [e for e in rec.used if e.evidence is EdgeEvidence.AUTHORITY]
    # The catalog URL IS the authority (NDP results carry no checksum/ETag/DOI).
    # Sabotage: drop _detect_authority_edges from record_transform -> zero authority edges -> red.
    assert len(auth) == 1
    assert auth[0].authority == result["url"]
    assert auth[0].sha256 == _sha(b"catalog bytes")  # locally hashed since it landed in ws


def test_ndp_catalog_details_authority_edges(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    result = {
        "dataset": {
            "id": "811f0bcc",
            "resources": [
                {"id": "a420", "url": "https://ndp/…/resource/a420/download/x.csv", "name": "x.csv"}
            ],
        },
        "_meta": {"tool": "get_dataset_details", "status": "success"},
    }
    rec = record_transform(
        app,
        sess.id,
        tool_name="ndp.get_dataset_details",
        args={},
        call_id="call_details",
        ok=True,
        result=result,
        minted=[],
        workspace_id="ws1",
    )
    assert rec is not None
    auth = [e for e in rec.used if e.evidence is EdgeEvidence.AUTHORITY]
    assert len(auth) == 1
    assert auth[0].authority.endswith("x.csv")
    assert auth[0].sha256 is None  # a catalog reference, not a local file


def test_non_ndp_result_yields_no_authority_edge(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    rec = record_transform(
        app,
        sess.id,
        tool_name="some_tool",
        args={},
        call_id="call_plain",
        ok=True,
        result={"answer": 42},
        minted=[],
        workspace_id="ws1",
    )
    assert rec is not None
    assert [e for e in rec.used if e.evidence is EdgeEvidence.AUTHORITY] == []


# --------------------------------------------------------------------------- #
# 5. Fold idempotency for the new event (item 1).
# --------------------------------------------------------------------------- #


def test_transform_fold_from_synthetic_payload_and_idempotency(tmp_path):
    app, _sess, _arc = _make_app(tmp_path)
    reg = get_registry(app)
    payload = {
        "event_id": "sem_t1",
        "call_id": "call_synthetic",
        "session_id": "s1",
        "workspace_id": "ws1",
        "status": "success",
        "instrument": {"tool": "plot", "args": {}},
        "environment": {"tier": "lockfile-hash", "lockfile_sha256": "a" * 64},
        "replay": "reproducible",
        "used": [
            {
                "role": "used",
                "evidence": "hash-pair",
                "artifact_id": "artifact_a",
                "sha256": "b" * 64,
            }
        ],
        "generated": [
            {
                "role": "generated",
                "evidence": "hash-pair",
                "artifact_id": "artifact_b",
                "sha256": "c" * 64,
            }
        ],
    }
    r1 = reg.fold_transform_recorded(payload)
    assert r1.applied
    got = reg.get_transform("call_synthetic")
    assert got is not None and got.replay is ReplayContract.REPRODUCIBLE
    assert got.used[0].artifact_id == "artifact_a"
    # Re-fold same event_id → duplicate no-op; different event_id, same call_id → keep-first.
    # Sabotage: drop the _seen_event_ids / call_id de-dup -> applied True again -> red.
    assert reg.fold_transform_recorded(payload).reason == "duplicate_event_id"
    payload2 = {**payload, "event_id": "sem_t2"}
    assert reg.fold_transform_recorded(payload2).reason == "duplicate_call_id"
    assert len(reg.all_transforms()) == 1


def test_transform_from_payload_rejects_missing_call_id():
    assert transform_from_payload({"event_id": "x"}) is None


def test_transform_event_is_in_boot_fold_set():
    from clio_agent.gact.artifacts.registry import _FOLD_EVENT_TYPES

    # Sabotage: remove the transform event from _FOLD_EVENT_TYPES -> a boot rebuild
    # drops all transforms (lineage empty after restart) -> red.
    assert ARTIFACT_TRANSFORM_RECORDED_EVENT in _FOLD_EVENT_TYPES


# --------------------------------------------------------------------------- #
# 6. Lineage — both directions, depth, truncation (item 5).
# --------------------------------------------------------------------------- #


def _build_chain(app, sess, tmp_path):
    """input.csv --(T1)--> mid.csv --(T2)--> plot.png ; returns their versions."""
    in_path, in_v = _register_file(app, sess, tmp_path, "input.csv", b"raw", "call_seed")
    mid_path = tmp_path / "mid.csv"
    mid_path.write_bytes(b"cleaned")
    mid_minted = mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="clean",
        effective_args={"output_path": str(mid_path)},
        call_id="call_t1",
        workspace_id="ws1",
    )
    record_transform(
        app,
        sess.id,
        tool_name="clean",
        args={"data_path": str(in_path), "output_path": str(mid_path)},
        call_id="call_t1",
        ok=True,
        result=None,
        minted=mid_minted,
        workspace_id="ws1",
    )
    plot_path = tmp_path / "plot.png"
    plot_path.write_bytes(b"\x89PNG")
    plot_minted = mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="plot",
        effective_args={"output_path": str(plot_path)},
        call_id="call_t2",
        workspace_id="ws1",
    )
    record_transform(
        app,
        sess.id,
        tool_name="plot",
        args={"data_path": str(mid_path), "output_path": str(plot_path)},
        call_id="call_t2",
        ok=True,
        result=None,
        minted=plot_minted,
        workspace_id="ws1",
    )
    return in_v, mid_minted[0], plot_minted[0]


def test_lineage_upstream_from_plot_finds_activity_and_inputs(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    _in_v, mid_v, plot_v = _build_chain(app, sess, tmp_path)
    reg = get_registry(app)
    graph = build_lineage(reg, plot_v.artifact_id, direction="upstream", depth=5)
    assert graph is not None
    node_ids = {n["id"] for n in graph["nodes"]}
    # plot → activity T2 (generated) → mid.csv (used) → activity T1 → input.csv.
    # Sabotage: skip index.produced_by expansion -> the activity/input nodes vanish -> red.
    assert plot_v.artifact_id in node_ids
    assert "activity:call_t2" in node_ids
    assert mid_v.artifact_id in node_ids
    assert "activity:call_t1" in node_ids
    gen_edges = [e for e in graph["edges"] if e["type"] == "generated"]
    used_edges = [e for e in graph["edges"] if e["type"] == "used"]
    assert gen_edges and used_edges


def test_lineage_downstream_from_input_finds_plot(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    in_v, _mid_v, plot_v = _build_chain(app, sess, tmp_path)
    reg = get_registry(app)
    graph = build_lineage(reg, in_v.artifact_id, direction="downstream", depth=5)
    assert graph is not None
    node_ids = {n["id"] for n in graph["nodes"]}
    # Subvenance: from input.csv (v1) we reach the plot two transforms downstream.
    assert plot_v.artifact_id in node_ids
    assert "activity:call_t1" in node_ids and "activity:call_t2" in node_ids


def test_lineage_depth_bounds_traversal(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    _in_v, _mid_v, plot_v = _build_chain(app, sess, tmp_path)
    reg = get_registry(app)
    shallow = build_lineage(reg, plot_v.artifact_id, direction="upstream", depth=0)
    assert shallow is not None
    # depth=0 → only the root node, no activity expansion.
    # Sabotage: ignore the `current_depth >= depth` cutoff -> more than one node -> red.
    assert [n["id"] for n in shallow["nodes"]] == [plot_v.artifact_id]


def test_lineage_revision_of_edge_downstream(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    path = tmp_path / "d.csv"
    path.write_bytes(b"v1")
    v1 = mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="p",
        effective_args={"output_path": str(path)},
        call_id="c1",
        workspace_id="ws1",
    )[0]
    path.write_bytes(b"v2")
    v2 = mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="p",
        effective_args={"output_path": str(path)},
        call_id="c2",
        workspace_id="ws1",
    )[0]
    graph = build_lineage(get_registry(app), v1.artifact_id, direction="downstream", depth=3)
    assert graph is not None
    rev = [e for e in graph["edges"] if e["type"] == "revision_of"]
    assert rev and any(e["from"] == v2.artifact_id and e["to"] == v1.artifact_id for e in rev)


def test_lineage_unknown_artifact_is_none(tmp_path):
    app, _sess, _arc = _make_app(tmp_path)
    assert build_lineage(get_registry(app), "artifact_missing", direction="both", depth=3) is None


# --------------------------------------------------------------------------- #
# 7. Lineage routes (item 5) — real app.
# --------------------------------------------------------------------------- #


def _workspace_session(client: TestClient, root: Path) -> tuple[str, str]:
    wid = client.post("/v1/workspaces", json={"name": "w", "root_path": str(root)}).json()["id"]
    sid = client.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]
    return wid, sid


def test_route_session_transforms_and_lineage(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        app = client.app
        wid, sid = _workspace_session(client, tmp_path)

        in_path = tmp_path / "in.csv"
        in_path.write_bytes(b"seed")
        in_v = mint_tool_declared_outputs(
            app,
            sid,
            tool_name="p",
            effective_args={"output_path": str(in_path)},
            call_id="rc1",
            workspace_id=wid,
        )[0]
        out_path = tmp_path / "out.png"
        out_path.write_bytes(b"\x89PNG")
        minted = mint_tool_declared_outputs(
            app,
            sid,
            tool_name="plot",
            effective_args={"output_path": str(out_path)},
            call_id="rc2",
            workspace_id=wid,
        )
        record_transform(
            app,
            sid,
            tool_name="plot",
            args={"data_path": str(in_path), "output_path": str(out_path)},
            call_id="rc2",
            ok=True,
            result=None,
            minted=minted,
            workspace_id=wid,
        )

        r = client.get(f"/v1/sessions/{sid}/transforms")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1 and body["transforms"][0]["call_id"] == "rc2"

        r2 = client.get("/v1/transforms/rc2")
        assert r2.status_code == 200 and r2.json()["transform"]["call_id"] == "rc2"

        r3 = client.get(f"/v1/artifacts/{minted[0].artifact_id}/lineage?direction=upstream&depth=5")
        assert r3.status_code == 200
        node_ids = {n["id"] for n in r3.json()["nodes"]}
        assert "activity:rc2" in node_ids and in_v.artifact_id in node_ids

        assert client.get("/v1/transforms/nope").status_code == 404
        assert client.get("/v1/artifacts/artifact_nope/lineage").status_code == 404


# --------------------------------------------------------------------------- #
# 8. Relay convergence — shape-compat against the REAL clio-relay models (item 6).
# --------------------------------------------------------------------------- #


def _load_relay_models():
    """Import the REAL clio_relay models from the sibling checkout, or skip.

    clio-relay is not a clio-agent dependency (federation is future); the shape-compat
    test validates against its ACTUAL pydantic models when the sibling checkout is
    present (set CLIO_RELAY_ROOT), never a hand-copied shape that could drift."""
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [
        os.environ.get("CLIO_RELAY_ROOT", ""),
        str(repo_root.parent / "clio-relay"),
        str(repo_root.parent.parent / "clio-relay"),
    ]
    for cand in candidates:
        if not cand:
            continue
        src = Path(cand) / "src"
        if (src / "clio_relay" / "models.py").is_file():
            if str(src) not in sys.path:
                sys.path.insert(0, str(src))
            import importlib

            return importlib.import_module("clio_relay.models")
    pytest.skip("clio-relay checkout not found (set CLIO_RELAY_ROOT); cannot verify shape-compat")


def test_used_edge_serializes_as_relay_artifact_use():
    from clio_agent.gact.artifacts.records import new_artifact_id

    models = _load_relay_models()
    # Our REAL artifact-id shape (``artifact_<uuid4hex>``) must itself validate as a
    # relay ``DurableRecordId`` — a stronger check than a synthetic id.
    artifact_id = new_artifact_id()
    edge = ProvEdge(
        role=EdgeRole.USED,
        evidence=EdgeEvidence.HASH_PAIR,
        artifact_id=artifact_id,
        sha256="a" * 64,
    )
    use = edge.to_artifact_use()
    assert use is not None
    # Our used edge MUST validate as the REAL relay ArtifactUse {artifact_id, sha256}.
    # Sabotage: rename to_artifact_use's keys (e.g. 'sha' instead of 'sha256') -> the
    # real model rejects extra/missing fields (extra='forbid') -> red.
    relay_use = models.ArtifactUse(**use)
    assert relay_use.artifact_id == artifact_id
    assert relay_use.sha256 == "a" * 64


def test_stat_pinned_edge_is_not_a_relay_artifact_use():
    # A stat-pinned / external-only edge has no sha → cannot be a relay ArtifactUse
    # (relay requires a 64-hex sha). This is the exact convergence gap the issue files.
    edge = ProvEdge(
        role=EdgeRole.USED, evidence=EdgeEvidence.SCHEMA_ARG, external_ref="external:/x"
    )
    assert edge.to_artifact_use() is None


def test_transform_extras_ride_relay_artifact_ref_metadata(tmp_path):
    models = _load_relay_models()
    app, sess, _arc = _make_app(tmp_path)
    in_path, _v = _register_file(app, sess, tmp_path, "in.csv", b"seed", "call_seed")
    rec = record_transform(
        app,
        sess.id,
        tool_name="plot",
        args={"data_path": str(in_path)},
        call_id="call_relay",
        ok=True,
        result=None,
        minted=[],
        workspace_id="ws1",
    )
    assert rec is not None
    provenance = rec.to_relay_provenance()
    # Our extras ride ArtifactRef.metadata['clio.provenance.v1'] (ArtifactUse is frozen
    # + extra='forbid' with no metadata field). Sabotage: put extras as a top-level
    # ArtifactRef field -> extra='forbid' rejects it -> red.
    ref = models.ArtifactRef(
        job_id="job_test0001",
        uri="artifact://ws1/in.csv@v1",
        kind="dataset",
        sha256="d" * 64,
        metadata={"clio.provenance.v1": provenance},
    )
    assert ref.metadata["clio.provenance.v1"]["activity_id"] == "call_relay"
    assert "used_artifact_refs" in ref.metadata["clio.provenance.v1"]


# --------------------------------------------------------------------------- #
# 8b. Relay convergence — ALWAYS-run compat against the VENDORED relay mirror
#     (finding [12]): the real-repo tests above skip in default CI; this fixture
#     layer catches a clio-side to_artifact_use/to_relay_provenance regression on
#     every run, and pins the relay commit it mirrors.
# --------------------------------------------------------------------------- #


def test_used_edge_serializes_as_relay_artifact_use_fixture():
    from clio_agent.gact.artifacts.records import new_artifact_id
    from tests.test_gact.relay_compat_fixture import ArtifactUse as FixtureArtifactUse

    artifact_id = new_artifact_id()
    edge = ProvEdge(
        role=EdgeRole.USED,
        evidence=EdgeEvidence.HASH_PAIR,
        artifact_id=artifact_id,
        sha256="a" * 64,
    )
    use = edge.to_artifact_use()
    assert use is not None
    # ALWAYS-run: the vendored mirror is frozen + extra='forbid' + 64-hex sha, so a
    # clio-side key/shape drift reddens in default CI (no sibling checkout needed).
    # Sabotage: rename to_artifact_use's keys ('sha' for 'sha256') -> extra='forbid' -> red.
    relay_use = FixtureArtifactUse(**use)
    assert relay_use.artifact_id == artifact_id
    assert relay_use.sha256 == "a" * 64


def test_transform_extras_ride_relay_artifact_ref_metadata_fixture(tmp_path):
    from tests.test_gact.relay_compat_fixture import ArtifactRef as FixtureArtifactRef

    app, sess, _arc = _make_app(tmp_path)
    in_path, _v = _register_file(app, sess, tmp_path, "in.csv", b"seed", "call_seed")
    rec = record_transform(
        app,
        sess.id,
        tool_name="plot",
        args={"data_path": str(in_path)},
        call_id="call_relay_fx",
        ok=True,
        result=None,
        minted=[],
        workspace_id="ws1",
    )
    assert rec is not None
    ref = FixtureArtifactRef(
        artifact_id="artifact_" + "a" * 32,
        job_id="job_test0001",
        uri="artifact://ws1/in.csv@v1",
        kind="dataset",
        sha256="d" * 64,
        metadata={"clio.provenance.v1": rec.to_relay_provenance()},
    )
    assert ref.metadata["clio.provenance.v1"]["activity_id"] == "call_relay_fx"
    assert "used_artifact_refs" in ref.metadata["clio.provenance.v1"]


def test_relay_fixture_pins_the_mirrored_commit():
    from tests.test_gact.relay_compat_fixture import RELAY_PINNED_COMMIT

    # The fixture must carry the relay commit it mirrors (finding [12]) so a re-mirror
    # is auditable. Sabotage: blank the pin -> red.
    assert len(RELAY_PINNED_COMMIT) == 40 and all(
        c in "0123456789abcdef" for c in RELAY_PINNED_COMMIT
    )


# --------------------------------------------------------------------------- #
# 9. Finding [1] — freshness guard: a freshly-written output under a non-designation
#    arg is NEVER a used input of the same call (a typed note makes the miss visible).
# --------------------------------------------------------------------------- #


def test_used_edge_freshness_guard_skips_freshly_written_output(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    call_start = 1_000_000.0
    # A genuinely pre-existing INPUT (mtime before the call) STILL edges.
    in_path = tmp_path / "input.csv"
    in_path.write_bytes(b"rows")
    os.utime(in_path, (call_start - 100, call_start - 100))
    # A freshly-WRITTEN output under a NON-designation arg (filename=) — mtime AFTER the
    # call start. Designation did not mint it; the used-detector must NOT record it as a
    # used INPUT of the same call (finding [1] — the self-dependency false edge).
    out_path = tmp_path / "out.png"
    out_path.write_bytes(b"\x89PNG plot")
    os.utime(out_path, (call_start + 100, call_start + 100))
    scan = _detect_used_edges(
        app,
        sess.id,
        args={"data_path": str(in_path), "filename": str(out_path)},
        workspace_id="ws1",
        turn_id="",
        trace_id="",
        call_started_at=call_start,
    )
    edge_paths = {e.path for e in scan.edges}
    resolved_out = str(out_path.resolve())
    resolved_in = str(in_path.resolve())
    # Sabotage: drop the mtime>=call_started_at freshness guard -> out.png becomes a
    # used edge (the call consumes its own output) -> this assertion red.
    assert resolved_out not in edge_paths
    assert resolved_in in edge_paths
    # The miss is DETECTABLE: a typed note names the freshly-written candidate.
    assert any(
        n["reason"] == "unminted_output_candidate" and n["path"] == resolved_out for n in scan.notes
    )


def test_used_edge_freshness_guard_off_when_no_call_start(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    q = tmp_path / "x.csv"
    q.write_bytes(b"data")
    # With no call_started_at (a direct detector call), the guard is inert — an ordinary
    # existing input still edges (the guard never suppresses a legitimate input).
    scan = _detect_used_edges(
        app, sess.id, args={"data_path": str(q)}, workspace_id="ws1", turn_id="", trace_id=""
    )
    assert len(scan.edges) == 1 and not scan.notes


# --------------------------------------------------------------------------- #
# 10. Finding [4] — relative/bare path args resolve against the WORKSPACE ROOT;
#     an unresolved path-looking arg records a typed miss (a bare query does not).
# --------------------------------------------------------------------------- #


def test_used_edge_relative_bare_path_resolves_against_workspace_root(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    (tmp_path / "in.csv").write_bytes(b"rows")
    scan = _detect_used_edges(
        app, sess.id, args={"data_path": "in.csv"}, workspace_id="ws1", turn_id="", trace_id=""
    )
    # Sabotage: resolve relative args against the process CWD (not the workspace root)
    # -> the bare filename is not a file -> zero edges -> red.
    assert len(scan.edges) == 1
    assert scan.edges[0].path == str((tmp_path / "in.csv").resolve())


def test_used_edge_unresolved_path_arg_is_noted_bare_query_is_not(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    scan = _detect_used_edges(
        app,
        sess.id,
        args={"data_path": "missing.csv", "city": "Los Angeles", "fmt": "png"},
        workspace_id="ws1",
        turn_id="",
        trace_id="",
    )
    assert scan.edges == []
    # A path-looking miss ('missing.csv') is DETECTABLE; a bare non-path string
    # ('Los Angeles') and a bare format ('png', no dot) are not noise (finding [4]).
    # Sabotage: drop the unresolved_path_arg note -> the missed input is undetectable -> red.
    assert [n["reason"] for n in scan.notes] == ["unresolved_path_arg"]
    assert scan.notes[0]["value"] == "missing.csv"


# --------------------------------------------------------------------------- #
# 11. Finding [3] — the changed-input edge note reports the ACTUAL reconcile class.
# --------------------------------------------------------------------------- #


def test_used_edge_note_auto_revision_when_lease_clean(tmp_path):
    from clio_agent.gact import tool_observer

    app, sess, _arc = _make_app(tmp_path)
    path, version = _register_file(app, sess, tmp_path, "in.csv", b"v1", "call_p1")
    path.write_bytes(b"v2-changed")
    # Inside an active tool call (stamp set) with a single writer -> a provably-clean
    # lease -> an auto-revision, NOT a custody gap (finding [3]).
    tool_observer._OBSERVER_CALL_T0.value = time.time()
    try:
        scan = _detect_used_edges(
            app, sess.id, args={"data_path": str(path)}, workspace_id="ws1", turn_id="", trace_id=""
        )
    finally:
        tool_observer._OBSERVER_CALL_T0.value = None
    e = scan.edges[0]
    # Sabotage: stamp note="gap_first" unconditionally -> a clean auto-revision is
    # mislabelled as a gap -> red.
    assert e.note == "auto_revision"
    assert e.artifact_id != version.artifact_id


# --------------------------------------------------------------------------- #
# 12. Finding [2] — a broad search LISTS hits (never consumes them): no authority
#     USED edges, a typed catalog_hits_not_consumed note instead.
# --------------------------------------------------------------------------- #


def test_search_datasets_notes_hits_not_consumed(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    result = {
        "datasets": [
            {
                "id": "d1",
                "resources": [
                    {"id": "r1", "url": "https://ndp/r1", "name": "a.csv"},
                    {"id": "r2", "url": "https://ndp/r2", "name": "b.csv"},
                ],
            }
        ],
        "_meta": {"tool": "search_datasets", "status": "success"},
    }
    rec = record_transform(
        app,
        sess.id,
        tool_name="ndp.search_datasets",
        args={},
        call_id="call_search",
        ok=True,
        result=result,
        minted=[],
        workspace_id="ws1",
    )
    assert rec is not None
    # Sabotage: keep search_datasets in the authority-edge set -> the 2 listed
    # resources become 'used' authority inputs -> this assertion red.
    assert [e for e in rec.used if e.evidence is EdgeEvidence.AUTHORITY] == []
    notes = [n for n in rec.notes if n["reason"] == "catalog_hits_not_consumed"]
    assert notes and notes[0]["hits"] == 2 and notes[0]["tool"] == "search_datasets"


# --------------------------------------------------------------------------- #
# 13. Finding [5] — authority-without-hash is re-runnable (identity pin, not bits);
#     authority-WITH-hash (a staged download) is reproducible.
# --------------------------------------------------------------------------- #


def test_replay_rerunnable_authority_without_hash():
    env = EnvironmentRecord(tier=EnvironmentTier.LOCKFILE_HASH, lockfile_sha256="a" * 64)
    used = [
        ProvEdge(
            role=EdgeRole.USED,
            evidence=EdgeEvidence.AUTHORITY,
            authority="https://ndp/resource/123",
        )
    ]
    contract, reason = compute_replay_contract(env, used)
    # A mutable catalog URL pins IDENTITY, not the bytes -> never a false bit-identical
    # guarantee. Sabotage: treat authority-without-sha as content-pinned -> REPRODUCIBLE -> red.
    assert contract is ReplayContract.RE_RUNNABLE
    assert reason == "inputs_authority_asserted:1"


def test_replay_reproducible_authority_with_hash():
    env = EnvironmentRecord(tier=EnvironmentTier.LOCKFILE_HASH, lockfile_sha256="a" * 64)
    used = [
        ProvEdge(
            role=EdgeRole.USED,
            evidence=EdgeEvidence.AUTHORITY,
            authority="https://ndp/resource/123",
            sha256="c" * 64,
        )
    ]
    contract, reason = compute_replay_contract(env, used)
    # A staged download hashed in-workspace DOES pin the bytes -> reproducible.
    assert contract is ReplayContract.REPRODUCIBLE and reason == ""


# --------------------------------------------------------------------------- #
# 14. Finding [6] — the lockfile hash resolves ONLY at the clio-agent repo anchor.
# --------------------------------------------------------------------------- #


def test_lockfile_anchor_ignores_foreign_uv_lock(tmp_path):
    from clio_agent.gact.artifacts.environment import _lockfile_path

    # A packaged install: a FOREIGN uv.lock at an ancestor, NO clio-agent pyproject.
    (tmp_path / "uv.lock").write_text("foreign-lock", encoding="utf-8")
    env_py = (
        tmp_path
        / ".venv"
        / "lib"
        / "site-packages"
        / "clio_agent"
        / "gact"
        / "artifacts"
        / "environment.py"
    )
    env_py.parent.mkdir(parents=True)
    env_py.write_text("# stub", encoding="utf-8")
    # Sabotage: revert to the first-uv.lock directory walk -> the foreign lock is
    # hashed as clio's environment identity -> red.
    assert _lockfile_path(start=env_py) is None


def test_lockfile_anchor_resolves_clio_repo_root(tmp_path):
    from clio_agent.gact.artifacts.environment import _lockfile_path

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "clio-agent"\nversion = "0.0.0"\n', encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text("clio-lock", encoding="utf-8")
    env_py = tmp_path / "src" / "clio_agent" / "gact" / "artifacts" / "environment.py"
    env_py.parent.mkdir(parents=True)
    env_py.write_text("# stub", encoding="utf-8")
    assert _lockfile_path(start=env_py) == tmp_path / "uv.lock"


# --------------------------------------------------------------------------- #
# 15. Finding [7] — a large inline arg is bounded to its content digest.
# --------------------------------------------------------------------------- #


def test_instrument_args_bounded_for_large_inline_content(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    big = "x" * (5 * 1024 * 1024)
    rec = record_transform(
        app,
        sess.id,
        tool_name="create_artifact",
        args={"content": big, "name": "r.md"},
        call_id="call_big",
        ok=True,
        result=None,
        minted=[],
        workspace_id="ws1",
    )
    assert rec is not None
    stored = rec.instrument.args["content"]
    # Sabotage: store args verbatim (dict(args)) -> content is the full 5MB string ->
    # the truncated-dict assertions red (and the registry/trace grow unbounded).
    assert isinstance(stored, dict) and stored["truncated"] is True
    assert stored["sha256"] == hashlib.sha256(big.encode("utf-8")).hexdigest()
    assert stored["size"] == 5 * 1024 * 1024 and len(stored["head"]) == 256
    # A small arg is kept verbatim (only over-bound values are elided).
    assert rec.instrument.args["name"] == "r.md"


# --------------------------------------------------------------------------- #
# 16. Finding [8] — the environment stamps the EXECUTING expert's bound LM.
# --------------------------------------------------------------------------- #


def test_environment_model_ref_from_executing_expert_lm(tmp_path):
    import dspy

    app, _sess, _arc = _make_app(tmp_path)
    expert_lm = dspy.LM("openai/expert-model-x")
    with dspy.context(lm=expert_lm):
        env = capture_environment(app)
    # The stamp names the EXECUTING expert's per-context LM, not the app-bound global.
    # Sabotage: read only _active_lm_model_ref(app) -> model_id is the global "" -> red.
    assert env.model_id == "openai/expert-model-x"
    assert env.provider_id == "openai"
    assert env.model_source == "executing_lm"


def test_environment_model_ref_typed_global_fallback(tmp_path):
    app, _sess, _arc = _make_app(tmp_path)
    env = capture_environment(app)  # no per-profile dspy.context bound
    # No executing-LM context -> a TYPED fallback to the global, never a silent wrong stamp.
    assert env.model_source == "global_fallback"


# --------------------------------------------------------------------------- #
# 17. Findings [9]/[10] — lineage truncation is typed AND well-formed (no dangling).
# --------------------------------------------------------------------------- #


def test_lineage_node_cap_has_no_dangling_edges(tmp_path, monkeypatch):
    import clio_agent.gact.artifacts.lineage as lin

    monkeypatch.setattr(lin, "_MAX_NODES", 2)
    app, sess, _arc = _make_app(tmp_path)
    _in_v, _mid_v, plot_v = _build_chain(app, sess, tmp_path)
    graph = build_lineage(get_registry(app), plot_v.artifact_id, direction="both", depth=5)
    assert graph is not None
    node_ids = {n["id"] for n in graph["nodes"]}
    # Sabotage: ignore add_node's return before add_edge -> a boundary edge references
    # a node clipped by the cap -> this well-formedness assertion red.
    for e in graph["edges"]:
        assert e["from"] in node_ids, e
        assert e["to"] in node_ids, e
    assert graph["truncated"] == {"reason": "node_cap", "nodes": 2}


def test_lineage_depth_horizon_is_typed(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    _in_v, _mid_v, plot_v = _build_chain(app, sess, tmp_path)
    graph = build_lineage(get_registry(app), plot_v.artifact_id, direction="upstream", depth=1)
    assert graph is not None
    # depth=1 clips before input.csv -> the graph continues past the requested horizon.
    # Sabotage: leave truncated=None on a depth clip -> a UI reads it as complete -> red.
    assert graph["truncated"] == {"reason": "depth_horizon", "at_depth": 1}


def test_lineage_complete_graph_is_not_truncated(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    _in_v, _mid_v, plot_v = _build_chain(app, sess, tmp_path)
    graph = build_lineage(get_registry(app), plot_v.artifact_id, direction="upstream", depth=9)
    assert graph is not None and graph["truncated"] is None


# --------------------------------------------------------------------------- #
# 18. Finding [11] — the REAL observer->transform wiring, and its typed failure.
# --------------------------------------------------------------------------- #


def test_observe_tool_transform_drives_real_observer_seam(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    from clio_agent.gact.tool_observer import _OBSERVER_CALL_IDS, _make_tool_observer

    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        app = client.app
        wid, sid = _workspace_session(client, tmp_path)
        in_path = tmp_path / "in.csv"
        in_path.write_bytes(b"rows")
        mint_tool_declared_outputs(
            app,
            sid,
            tool_name="seed",
            effective_args={"output_path": str(in_path)},
            call_id="seed",
            workspace_id=wid,
        )
        out_path = tmp_path / "chart.png"
        observe = _make_tool_observer(app)
        # Drive the REAL observer seam POSITIONALLY, exactly as production does.
        observe("plot", {"data_path": str(in_path)}, "started", None)
        out_path.write_bytes(b"\x89PNG chart")
        call_id = _OBSERVER_CALL_IDS.value
        observe(
            "plot",
            {"data_path": str(in_path), "output_path": str(out_path)},
            "completed",
            None,
            {"content": [{"type": "text", "text": "ok"}]},
        )
        rec = get_registry(app).get_transform(call_id)
        # Sabotage: swap/insert a positional arg at tool_observer.py's
        # observe_tool_transform(...) call -> the record never lands -> red.
        assert rec is not None
        assert any(e.path == str(out_path) or e.name == "chart.png" for e in rec.generated)


def test_transform_record_failure_is_typed_not_swallowed(tmp_path, monkeypatch):
    import clio_agent.gact.artifacts.transforms as tmod

    app, sess, _arc = _make_app(tmp_path)

    def _boom(*_a, **_k):
        raise RuntimeError("sabotage-inside-record")

    monkeypatch.setattr(tmod, "record_transform", _boom)
    # The turn must be UNHARMED (no exception escapes) AND the failure must be TYPED +
    # queryable — never a bare swallow (finding [11]).
    observe_tool_transform(app, sess.id, "plot", {"x": "y"}, "call_boom", True, None)
    failures = tmod.transform_record_failures(app)
    # Sabotage: revert the except to a bare logger.warning swallow -> ledger empty -> red.
    assert failures and failures[-1]["reason"] == "transform_record_failed"
    assert failures[-1]["call_id"] == "call_boom"
    assert failures[-1]["cause"] == "RuntimeError"
