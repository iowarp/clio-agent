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

import asyncio
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
from clio_agent.gact.events import EventBus
from clio_agent.gact.semantic_events import (
    SSE_TRACE_ONLY_EVENT_TYPES,
    SSE_UI_EVENT_TYPES,
    NoopSemanticTraceBackend,
    SemanticEvent,
    SemanticEventSink,
    event_reaches_ui,
)
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
        return SimpleNamespace(id=wid, root_path=root) if root else None

    def list(self) -> list[Any]:
        return [SimpleNamespace(id=wid, root_path=root) for wid, root in self._roots.items()]


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


# --------------------------------------------------------------------------- #
# GAP A — designation-by-result (S5 live gate #971). An intermediate written by a
# tool that carries the path ONLY in its RESULT (no output ARG names it — e.g.
# ndp_stage_resource -> local_path) must MINT so the downstream call pins it as a
# hash-pair edge, not external-with-sha.
# --------------------------------------------------------------------------- #


def test_result_declared_paths_extracts_recognized_keys():
    from clio_agent.gact.artifacts.designation import result_declared_paths

    # MCP envelope: reads under structuredContent AND a bare dict, identically.
    env = {"structuredContent": {"ok": True, "local_path": "/ws/catalog.csv", "url": "https://x"}}
    assert result_declared_paths(env) == {"local_path": "/ws/catalog.csv"}
    assert result_declared_paths({"local_path": "/ws/catalog.csv"}) == {
        "local_path": "/ws/catalog.csv"
    }
    # Precision over recall: a non-artifact-suffix value ("stdout"), and a remote url
    # under a NON-recognized key, both yield nothing.
    # Sabotage: add "url" to RESULT_PATH_KEYS -> the remote url is captured -> red.
    assert result_declared_paths({"local_path": "stdout", "url": "https://x/y.csv"}) == {}
    assert result_declared_paths({}) == {}
    assert result_declared_paths(None) == {}


def test_mint_result_declared_output_stage_resource(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    staged = tmp_path / "earthscope_converted_data.csv"
    staged.write_bytes(b"Site,Latitude\nA,34.0\n")
    # The tool takes a destination DIRECTORY only; the concrete path rides the result.
    result = {
        "ok": True,
        "local_path": str(staged),
        "url": "https://ndp/earthscope_converted_data.csv",
        "_meta": {"tool": "stage_resource", "status": "success"},
    }
    minted = mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="ndp.stage_resource",
        effective_args={"url": "https://ndp/x", "output_dir": str(tmp_path)},
        call_id="call_stage",
        workspace_id="ws1",
        result=result,
    )
    # Sabotage: drop the result channel from mint_tool_declared_outputs -> minted == [] -> red.
    assert len(minted) == 1
    assert minted[0].path == str(staged)
    match = get_registry(app).find_version_by_path("ws1", str(staged))
    assert match is not None
    _rec, ver = match
    # The designation basis is queryable: result channel + the exact result key.
    assert ver.producer.get("designation") == "tool-result"
    assert ver.producer.get("result_key") == "local_path"
    assert ver.producer.get("call_id") == "call_stage"


def test_result_declared_path_deduped_against_arg(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    out = tmp_path / "out.csv"
    out.write_bytes(b"rows")
    # The SAME path appears both as an output ARG and echoed in the result.
    minted = mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="t",
        effective_args={"output_path": str(out)},
        call_id="c",
        workspace_id="ws1",
        result={"local_path": str(out)},
    )
    # Minted exactly once (the arg channel wins); the result channel dedups by path.
    # Sabotage: drop the `seen` dedup -> two versions minted -> red.
    assert len(minted) == 1
    assert minted[0].producer.get("designation") == "tool-arg"


def test_result_declared_output_connects_downstream_hash_pair(tmp_path):
    """The canary acceptance shape: an intermediate minted by RESULT designation lets
    the downstream clean pin it as a hash-pair edge (not external-with-sha)."""
    app, sess, _arc = _make_app(tmp_path)
    # 1. stage_resource writes the intermediate catalog, declared ONLY in the result.
    staged = tmp_path / "earthscope_converted_data.csv"
    staged.write_bytes(b"Site,Latitude\nA,34.0\nB,33.0\n")
    observe_tool_transform(
        app,
        sess.id,
        tool_name="ndp.stage_resource",
        effective_args={"url": "https://ndp/x", "output_dir": str(tmp_path)},
        call_id="call_stage",
        ok=True,
        result={
            "ok": True,
            "local_path": str(staged),
            "url": "https://ndp/earthscope_converted_data.csv",
            "_meta": {"tool": "stage_resource"},
        },
    )
    reg = get_registry(app)
    # BEFORE the fix the intermediate stayed external (unregistered); now it is a
    # registered artifact version.
    match = reg.find_version_by_path("ws1", str(staged))
    assert match is not None
    staged_id = match[1].artifact_id

    # 2. the downstream clean USES the intermediate as input.
    clean = tmp_path / "earthscope_stations_clean.csv"
    clean.write_bytes(b"Site,Latitude\nA,34.0\n")
    observe_tool_transform(
        app,
        sess.id,
        tool_name="pandas.filter_data",
        effective_args={"file_path": str(staged), "output_file": str(clean)},
        call_id="call_filter",
        ok=True,
        result=None,
    )
    rec = reg.get_transform("call_filter")
    assert rec is not None
    used_to_staged = [e for e in rec.used if e.path == str(staged)]
    assert used_to_staged, "the clean must record a used edge to the intermediate"
    edge = used_to_staged[0]
    # Sabotage: revert the result-channel mint -> the intermediate stays unregistered
    # -> this edge is external:schema-arg with an empty artifact_id -> red.
    assert edge.evidence is EdgeEvidence.HASH_PAIR
    assert edge.artifact_id == staged_id
    assert not edge.external_ref


# --------------------------------------------------------------------------- #
# GAP B — parent aggregation (S5 live gate #971). A parent orchestrator's own
# /transforms and /artifacts are empty while its spawned children hold everything;
# ?include_children=true merges the descendants' records with per-row attribution.
# --------------------------------------------------------------------------- #


def test_descendant_session_ids_bfs_bounded_and_cycle_safe():
    from clio_agent.gact.agent_tasks import AgentTask, AgentTaskRegistry, descendant_session_ids

    reg = AgentTaskRegistry()
    reg.register(
        AgentTask(task_id="t1", parent_session_id="root", child_session_id="c1", created_at="1")
    )
    reg.register(
        AgentTask(task_id="t2", parent_session_id="root", child_session_id="c2", created_at="2")
    )
    reg.register(
        AgentTask(task_id="t3", parent_session_id="c1", child_session_id="g1", created_at="3")
    )
    app = SimpleNamespace(state=SimpleNamespace(agent_task_registry=reg))

    # Full descendants: children + grandchildren.
    assert set(descendant_session_ids(app, "root")) == {"c1", "c2", "g1"}
    # depth 1 = direct children only (grandchild excluded).
    # Sabotage: ignore max_depth -> g1 leaks in -> red.
    assert set(descendant_session_ids(app, "root", max_depth=1)) == {"c1", "c2"}
    # Cycle safety: a task pointing back to the root never re-includes it / loops.
    reg.register(
        AgentTask(task_id="t4", parent_session_id="g1", child_session_id="root", created_at="4")
    )
    got = descendant_session_ids(app, "root")
    assert "root" not in got and set(got) == {"c1", "c2", "g1"}
    # No registry -> empty.
    empty = SimpleNamespace(state=SimpleNamespace(agent_task_registry=None))
    assert descendant_session_ids(empty, "root") == []


def test_route_session_transforms_include_children(tmp_path, monkeypatch):
    from clio_agent.gact.agent_tasks import seed_agent_task

    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        app = client.app
        wid, parent = _workspace_session(client, tmp_path)
        t1 = seed_agent_task(app, parent_session_id=parent, agent_ref={"expert_id": "ndp"})
        t2 = seed_agent_task(app, parent_session_id=parent, agent_ref={"expert_id": "geo"})
        for child, cid in ((t1.child_session_id, "cc1"), (t2.child_session_id, "cc2")):
            record_transform(
                app,
                child,
                tool_name="t",
                args={},
                call_id=cid,
                ok=True,
                result=None,
                minted=[],
                workspace_id=wid,
            )

        # Flag OFF: the parent's OWN transforms are empty, and the body is unchanged.
        off = client.get(f"/v1/sessions/{parent}/transforms")
        assert off.status_code == 200
        assert off.json()["count"] == 0
        assert "include_children" not in off.json()

        # Flag ON: aggregated with per-row producing-session attribution.
        on = client.get(f"/v1/sessions/{parent}/transforms?include_children=true").json()
        # Sabotage: ignore include_children -> count stays 0 -> red.
        assert on["count"] == 2
        assert {t["session_id"] for t in on["transforms"]} == {
            t1.child_session_id,
            t2.child_session_id,
        }
        assert {t["call_id"] for t in on["transforms"]} == {"cc1", "cc2"}
        assert set(on["child_session_ids"]) == {t1.child_session_id, t2.child_session_id}


def test_route_session_artifacts_include_children(tmp_path, monkeypatch):
    from clio_agent.gact.agent_tasks import AgentTask

    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    ws2_dir = tmp_path / "ws2"
    ws2_dir.mkdir()
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        app = client.app
        wid, parent = _workspace_session(client, tmp_path)
        # A child in a DIFFERENT workspace (the case workspace-scoped listing misses).
        wid2 = client.post("/v1/workspaces", json={"name": "w2", "root_path": str(ws2_dir)}).json()[
            "id"
        ]
        child = app.state.sessions.create(
            workspace_id=wid2, title="child", parent_session_id=parent
        )
        app.state.agent_task_registry.register(
            AgentTask(
                task_id="t1", parent_session_id=parent, child_session_id=child.id, created_at="1"
            )
        )
        f2 = ws2_dir / "child_out.csv"
        f2.write_bytes(b"child rows")
        mint_tool_declared_outputs(
            app,
            child.id,
            tool_name="t",
            effective_args={"output_path": str(f2)},
            call_id="cm2",
            workspace_id=wid2,
        )

        # Flag OFF: parent's own workspace has nothing; body unchanged.
        off = client.get(f"/v1/sessions/{parent}/artifacts")
        assert off.status_code == 200 and off.json()["count"] == 0
        assert "include_children" not in off.json()

        # Flag ON: the child's artifact appears, attributed to the child session.
        on = client.get(f"/v1/sessions/{parent}/artifacts?include_children=true").json()
        # Sabotage: ignore include_children -> count stays 0 -> red.
        assert on["count"] == 1
        row = on["artifacts"][0]
        assert row["name"] == "child_out.csv"
        assert child.id in row["producing_session_ids"]
        assert set(on["child_session_ids"]) == {child.id}


# --------------------------------------------------------------------------- #
# Session-scoped listing (owner defect, 2026-08-05). ``GET /v1/sessions/{sid}/
# artifacts`` must show only records a VERSION of which was produced by THIS
# session (or, with include_children=true, a descendant session) — never every
# record in the shared workspace. Before the fix a brand-new sibling session in a
# workspace with prior artifacts showed them as its own.
# --------------------------------------------------------------------------- #


def test_route_session_artifacts_is_scoped_to_the_producing_session(tmp_path, monkeypatch):
    from clio_agent.gact.agent_tasks import AgentTask

    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        app = client.app
        wid, sid_a = _workspace_session(client, tmp_path)
        sid_b = client.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]

        # Session A mints an artifact in the SHARED workspace.
        a_out = tmp_path / "a_out.csv"
        a_out.write_bytes(b"a rows")
        mint_tool_declared_outputs(
            app,
            sid_a,
            tool_name="t",
            effective_args={"output_path": str(a_out)},
            call_id="call_a",
            workspace_id=wid,
        )

        # A child of A, wired via the agent-task registry, mints its OWN artifact —
        # also in the shared workspace (same-workspace children are common).
        child = app.state.sessions.create(workspace_id=wid, title="child", parent_session_id=sid_a)
        app.state.agent_task_registry.register(
            AgentTask(
                task_id="t1", parent_session_id=sid_a, child_session_id=child.id, created_at="1"
            )
        )
        c_out = tmp_path / "c_out.csv"
        c_out.write_bytes(b"c rows")
        mint_tool_declared_outputs(
            app,
            child.id,
            tool_name="t",
            effective_args={"output_path": str(c_out)},
            call_id="call_c",
            workspace_id=wid,
        )

        # B never minted anything — a brand-new sibling session sees NOTHING from the
        # shared workspace, flag off or on (it has no descendants either).
        b_off = client.get(f"/v1/sessions/{sid_b}/artifacts")
        assert b_off.status_code == 200
        assert b_off.json()["count"] == 0
        assert b_off.json()["artifacts"] == []
        b_on = client.get(f"/v1/sessions/{sid_b}/artifacts?include_children=true").json()
        assert b_on["count"] == 0
        assert b_on["artifacts"] == []

        # A, flag OFF: sees only its OWN artifact — NOT the child's, even though the
        # child wrote into the same workspace (session-scoped, not workspace-scoped).
        a_off = client.get(f"/v1/sessions/{sid_a}/artifacts").json()
        assert a_off["count"] == 1
        assert [r["name"] for r in a_off["artifacts"]] == ["a_out.csv"]

        # A, flag ON: the child's artifact now joins (a declared descendant), so both
        # show up — the child-of-A artifact appears for A ONLY with include_children.
        a_on = client.get(f"/v1/sessions/{sid_a}/artifacts?include_children=true").json()
        assert a_on["count"] == 2
        assert {r["name"] for r in a_on["artifacts"]} == {"a_out.csv", "c_out.csv"}


# --------------------------------------------------------------------------- #
# Boot-fold placement (#971 defects 1b + 2). The artifact registry projection is
# folded ONCE at server boot, OFF the event loop, before the agent is announced
# ready — NEVER lazily on the tool-completion hot path (defect 2) — and a wedged
# ARC store surfaces as a LOUD, typed, actionable boot failure (defect 1b) rather
# than a mid-turn whole-server GIL freeze (the S5 gate2 stall).
# --------------------------------------------------------------------------- #


class _StallingArc:
    """A fake ARC whose ``_events`` reader stalls exactly as a hung native GetBlob does.

    The per-RPC liveness ladder, once exhausted, raises
    :class:`~clio_agent.arc.clio_core_liveness.ClioCoreRuntimeLostError`; this fake
    raises it from ``iter_event_contents`` so the boot fold sees the same typed stall
    the real store surfaces. ``data_dir`` is the on-disk store path the actionable
    error must name.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._live = SimpleNamespace(iter_event_contents=self._iter)

    def _iter(self):
        from clio_agent.arc.clio_core_liveness import ClioCoreRuntimeLostError

        raise ClioCoreRuntimeLostError(
            "clio-core RPC 'get' produced no response; the peer appears to be a zombie",
            reason="clio_core_rpc_stalled",
            port=9413,
        )
        yield {}  # pragma: no cover - makes _iter a generator (reader() is iterated)


def test_boot_fold_then_observer_never_pays_the_fold(tmp_path, monkeypatch):
    """Defect 2: after the boot fold stamps the projection, the tool observer reuses it
    and pays ZERO additional folds — the O(corpus) rebuild is off the turn hot path."""
    app, sess, _arc = _make_app(tmp_path)
    import clio_agent.gact.artifacts.registry as reg_mod

    calls = {"n": 0}
    real = reg_mod.rebuild_registry_at_boot

    def counting(a):
        calls["n"] += 1
        return real(a)

    monkeypatch.setattr(reg_mod, "rebuild_registry_at_boot", counting)

    # Boot fold, as the lifespan runs it: off the event loop, exactly once.
    async def boot():
        return await asyncio.to_thread(reg_mod.rebuild_registry_at_boot, app)

    reg = asyncio.run(boot())
    assert calls["n"] == 1
    assert app.state.artifact_registry is reg

    # The tool-completion hot path (twice) must find the pre-built projection and NOT
    # trigger a lazy rebuild.
    observe_tool_transform(app, sess.id, "plot", {"x": "y"}, "c1", True, None)
    observe_tool_transform(app, sess.id, "plot2", {"a": "b"}, "c2", True, None)
    # Sabotage: revert the boot-fold wiring so observe folds lazily -> n > 1 -> red.
    assert calls["n"] == 1


def test_get_registry_on_loop_guard_still_bites(tmp_path):
    """The RegistryFoldOnLoopError backstop remains: a lazy first access on the loop
    thread (the in-process/test fallback path) is still LOUD, not a silent loop stall."""
    from clio_agent.gact.artifacts.registry import RegistryFoldOnLoopError, get_registry

    app, _sess, _arc = _make_app(tmp_path)  # artifact_registry starts None

    async def access():
        return get_registry(app)

    with pytest.raises(RegistryFoldOnLoopError):
        asyncio.run(access())


def test_arc_boot_fold_stall_raises_typed_actionable(tmp_path, monkeypatch):
    """Defect 1b: a wedged ARC store (hung native RPC) makes the boot fold raise a TYPED,
    actionable stall naming the store to rotate — NOT a silently-empty registry."""
    monkeypatch.setattr("clio_agent.gact.runtime.globals._PROCESS_ARC", None)
    from clio_agent.gact.artifacts.registry_boot import (
        ArtifactRegistryBootStalled,
        rebuild_registry_at_boot,
    )

    store_dir = tmp_path / ".clio" / "agent" / "arc"
    state = SimpleNamespace(
        arc=_StallingArc(store_dir),
        semantic_trace_backend=None,
        artifact_registry=None,
    )
    app = SimpleNamespace(state=state)

    with pytest.raises(ArtifactRegistryBootStalled) as ei:
        rebuild_registry_at_boot(app)
    exc = ei.value
    # Sabotage: revert _fold_from_arc to swallow ClioCoreRuntimeLostError into a
    # reachable=False degrade -> rebuild returns an empty capture_released registry
    # instead of raising -> pytest.raises red.
    assert exc.store_path == str(store_dir)
    assert exc.scope == "_events"
    assert exc.reason == "arc_boot_fold_stalled"
    assert "clio doctor" in str(exc)
    assert app.state.artifact_registry is None  # never stamp a half/empty projection on a wedge


def test_construct_agent_aborts_and_stays_unready_on_boot_fold_stall(tmp_path, monkeypatch):
    """Defect 1b end-to-end: the boot-fold helper the lifespan calls returns False on a
    wedged store, stamps an actionable agent_init_error, and never announces readiness."""
    monkeypatch.setattr("clio_agent.gact.runtime.globals._PROCESS_ARC", None)
    from clio_agent.gact.artifacts.registry_boot import boot_fold_artifact_registry_offloop

    store_dir = tmp_path / ".clio" / "agent" / "arc"
    state = SimpleNamespace(
        arc=_StallingArc(store_dir),
        semantic_trace_backend=None,
        artifact_registry=None,
        agent_init_error=None,
    )
    app = SimpleNamespace(state=state)

    async def run():
        loop = asyncio.get_running_loop()
        return await boot_fold_artifact_registry_offloop(app, loop)

    ok = asyncio.run(run())
    # Sabotage: swallow the stall into a capture_released empty registry -> ok True -> red.
    assert ok is False
    assert app.state.artifact_registry is None  # readiness gated: no projection stamped
    assert str(store_dir) in app.state.agent_init_error
    assert "clio doctor" in app.state.agent_init_error


def test_boot_fold_helper_returns_true_and_stamps_on_healthy_store(tmp_path, monkeypatch):
    """The helper returns True (agent may be announced) and stamps the projection when the
    fold completes — the normal boot path with a reachable store."""
    import json

    monkeypatch.setattr("clio_agent.gact.runtime.globals._PROCESS_ARC", None)
    from clio_agent.gact.artifacts.registry_boot import boot_fold_artifact_registry_offloop

    trace_dir = tmp_path / "semantic_traces"
    trace_dir.mkdir()
    line = {
        "event_type": "artifact.created",
        "event_id": "e1",
        "payload": {
            "workspace_id": "ws1",
            "name": "d.csv",
            "version": 1,
            "sha256": "a" * 64,
            "kind": "dataset",
        },
    }
    (trace_dir / "sess_x.semantic.jsonl").write_text(json.dumps(line) + "\n", "utf-8")
    state = SimpleNamespace(
        arc=None,
        semantic_trace_backend=SimpleNamespace(path=trace_dir),
        artifact_registry=None,
    )
    app = SimpleNamespace(state=state)

    async def run():
        return await boot_fold_artifact_registry_offloop(app, asyncio.get_running_loop())

    ok = asyncio.run(run())
    assert ok is True
    assert app.state.artifact_registry is not None
    assert app.state.artifact_registry.count() == 1


# --------------------------------------------------------------------------- #
# S5 gate3 C5 regression — trace-only provenance must NEVER reach the SSE wire,
# even when its status is "failed". The gate3 canary caught ONE
# ``artifact.transform.recorded`` frame leaking onto a child session's SSE
# stream in run 6 (a FAILED/contended transform, status="failed") while run 5
# leaked zero — a data-dependent clean-stream violation. Root cause: the
# ``_SSE_ALWAYS_STATUSES`` override in ``event_reaches_ui`` lifted ANY event
# onto the wire on a failure status, bypassing the allow-list; a transform that
# failed (generated 0 outputs) tripped it. The fix excludes the trace-only
# provenance substrate FIRST, so no status can lift it.
# --------------------------------------------------------------------------- #


def test_trace_only_provenance_never_reaches_ui_even_on_failure():
    """Deterministic allow-list lock across EVERY status (the path the S2/S5
    locks missed: they only called ``event_reaches_ui`` with the default status).

    Sabotage: delete the ``SSE_TRACE_ONLY_EVENT_TYPES`` short-circuit in
    ``event_reaches_ui`` -> the ``"failed"``/``"error"``/``"cancelled"`` rows go
    red (the exact leak the gate3 canary observed).
    """
    trace_only = (
        "artifact.used",
        ARTIFACT_TRANSFORM_RECORDED_EVENT,  # "artifact.transform.recorded"
        "artifact.transform.failed",
        "artifact.proposed",
    )
    for et in trace_only:
        assert et in SSE_TRACE_ONLY_EVENT_TYPES
        assert et not in SSE_UI_EVENT_TYPES
        # NO status lifts a provenance record onto the served wire.
        for status in ("failed", "error", "cancelled", "completed", "", "FAILED"):
            assert event_reaches_ui(et, status) is False, (et, status)

    # Positive control: the ``_SSE_ALWAYS_STATUSES`` override is INTACT for real
    # action/lifecycle errors (a failed step that is not otherwise allow-listed
    # must still surface). Sabotage: over-broaden the exclusion -> these go red.
    assert event_reaches_ui("turn.failed", "failed") is True
    assert event_reaches_ui("lm.call", "error") is True
    assert event_reaches_ui("tool.call", "cancelled") is True
    # Allow-listed atoms still pass on their normal completion status.
    assert event_reaches_ui("artifact.created", "completed") is True
    assert event_reaches_ui("react.step.completed", "completed") is True


def _sem_event_frames(bus: EventBus, sid: str):
    """Every ``semantic.event`` frame recorded in the bus replay history."""
    return [
        ev
        for ev in bus._history.get(sid, [])  # noqa: SLF001 - test asserts on served history
        if getattr(ev, "type", "") == "semantic.event"
    ]


def _frame_event_type(ev) -> str:
    payload = getattr(ev, "payload", None) or {}
    return str(payload.get("event_type", ""))


@pytest.mark.asyncio
@pytest.mark.concurrency
async def test_failed_transform_never_leaks_to_sse_under_attach_race():
    """Hammer the child-session attach window with a REAL bus + sink.

    Emit many FAILED ``artifact.transform.recorded`` events off worker threads
    (as the executor emits them) concurrently with fresh SSE subscribers
    attaching — each attach replays the bus history (the exact "just-attached
    stream serving raw history" hypothesis). A failed provenance record must
    reach NEITHER the live queue NOR the replay snapshot; the allow-listed
    ``artifact.created`` frames must still get through (non-vacuous).

    Sabotage: delete the ``SSE_TRACE_ONLY_EVENT_TYPES`` short-circuit in
    ``event_reaches_ui`` -> the ``status="failed"`` transforms trip
    ``_SSE_ALWAYS_STATUSES``, land in history, and the leak asserts go red.
    """
    bus = EventBus()
    sink = SemanticEventSink(bus=bus, trace_backend=NoopSemanticTraceBackend(), capture=False)
    sid = "sess_leakrace"
    loop = asyncio.get_running_loop()
    n = 150

    def emit_failed_transform(i: int) -> None:
        sink.emit(
            SemanticEvent(
                event_type=ARTIFACT_TRANSFORM_RECORDED_EVENT,
                session_id=sid,
                trace_id="trace_x",
                turn_id="turn_x",
                status="failed",
                summary=f"Transform recorded ({i}).",
                payload={"call_id": f"call_{i}", "status": "failed", "kind": "contended"},
            )
        )

    def emit_created(i: int) -> None:
        sink.emit(
            SemanticEvent(
                event_type="artifact.created",
                session_id=sid,
                trace_id="trace_x",
                turn_id="turn_x",
                status="completed",
                payload={"name": f"a{i}.csv", "version": 1},
            )
        )

    live_leaks: list[str] = []
    replay_snapshots: list[list[str]] = []

    async def attach_and_drain() -> None:
        """Attach a fresh subscriber, drain its replay snapshot + any live tail."""
        got: list[str] = []
        agen = bus.subscribe(sid)
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(agen.__anext__(), timeout=0.02)
                except (asyncio.TimeoutError, StopAsyncIteration):
                    break
                if getattr(ev, "type", "") == "semantic.event":
                    et = _frame_event_type(ev)
                    got.append(et)
                    if et in SSE_TRACE_ONLY_EVENT_TYPES:
                        live_leaks.append(et)
        finally:
            await agen.aclose()
        replay_snapshots.append(got)

    # Interleave foreign-thread emits (bridged onto the loop) with fresh attaches.
    emit_futs = []
    attach_tasks = []
    for i in range(n):
        emit_futs.append(loop.run_in_executor(None, emit_failed_transform, i))
        if i % 15 == 0:
            emit_created(i)
            attach_tasks.append(asyncio.create_task(attach_and_drain()))
        await asyncio.sleep(0)
    await asyncio.gather(*emit_futs)
    # Drain any bridged publishes still queued on the loop.
    for _ in range(5):
        await asyncio.sleep(0)
    # Final round of fresh attaches now that ALL history is present.
    await asyncio.gather(*(attach_and_drain() for _ in range(8)))
    await asyncio.gather(*attach_tasks)

    # (1) Live queues never carried a trace-only frame.
    assert live_leaks == [], f"trace-only frames leaked to live SSE: {live_leaks}"
    # (2) The served replay history is clean of EVERY trace-only atom...
    history_frames = _sem_event_frames(bus, sid)
    history_types = [_frame_event_type(ev) for ev in history_frames]
    assert not (set(history_types) & SSE_TRACE_ONLY_EVENT_TYPES), (
        f"trace-only frame in replay history: {history_types}"
    )
    # (3) ...and no fresh-attach snapshot ever replayed one.
    for snap in replay_snapshots:
        assert not (set(snap) & SSE_TRACE_ONLY_EVENT_TYPES), snap
    # (4) Non-vacuous: the allow-listed artifact.created frames DID reach the wire.
    assert history_types.count("artifact.created") >= 1
    # Every history frame is an allow-listed atom (or a failure of a listed type).
    assert all(_frame_event_type(ev) not in SSE_TRACE_ONLY_EVENT_TYPES for ev in history_frames)
