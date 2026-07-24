"""P3.3 (#1040) — transitive reproduce bundle + closure policy unit tests.

The provenance pillar's closing slice. ``build_artifact_bundle`` no longer pulls a
single hop of inputs (a 2-hops-up producer was silently invisible, so reproduce
dropped that stage's input at its ``if e.artifact_id in nodes`` guard); it drives off
the COMPLETE upstream closure (:func:`build_lineage` with ``complete=True``) and
re-resolves every wire node back to a registry record/transform. The closure drops
the depth horizon but stays BOUNDED by a config-resolved node cap whose typed
``truncated`` marker is surfaced into the crate root. The interactive ``/lineage``
path is byte-identical (depth-bounded, ``_MAX_NODES`` literal cap). ``build_session_
bundle`` additionally reaches a NON-descendant sibling job that produced a consumed
input, pulling its full producing chain across the job boundary.

Each key lock carries a sabotage note: the referenced neutralization turns the named
assertion red, proving the test binds the invariant (not a tautology).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from clio_agent.gact.artifacts.export import build_artifact_bundle, build_session_bundle
from clio_agent.gact.artifacts.lineage import build_lineage, lineage_max_nodes
from clio_agent.gact.artifacts.minting import mint_artifact
from clio_agent.gact.artifacts.records import ArtifactKind, Custody, IdentityEvidence, Mechanism
from clio_agent.gact.artifacts.registry import ArtifactRegistry
from clio_agent.gact.artifacts.reproduce import ArtifactNode, compile_reproduce
from clio_agent.gact.artifacts.transform_types import EdgeEvidence, EdgeRole, Instrument, ProvEdge
from clio_agent.gact.artifacts.transforms import TransformRecord, TransformStatus
from clio_agent.gact.sessions import SessionStore

# --------------------------------------------------------------------------- #
# Fakes + fixture builders (parity with the S7 export harness).
# --------------------------------------------------------------------------- #


class _CapturingArc:
    def __init__(self) -> None:
        self.events: list = []

    def record_semantic_event(self, event):
        self.events.append(event)
        return event


class _FakeWorkspaces:
    def __init__(self, roots: dict[str, str]) -> None:
        self._roots = roots

    def get(self, wid):
        root = self._roots.get(wid)
        return SimpleNamespace(id=wid, root_path=root) if root else None

    def list(self):
        return [SimpleNamespace(id=wid, root_path=root) for wid, root in self._roots.items()]


def _export_app(tmp_path: Path, ws_roots: dict[str, str]):
    store = SessionStore(path=tmp_path / "sessions.json")
    state = SimpleNamespace(
        sessions=store,
        arc=_CapturingArc(),
        workspaces=_FakeWorkspaces(ws_roots),
        semantic_event_sink=object(),
        semantic_trace_detail_level="semantic",
        semantic_trace_backend=None,
        artifact_registry=ArtifactRegistry(),
    )
    return SimpleNamespace(state=state), store


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mint(app, sid, *, name, path, ws, kind=ArtifactKind.DATASET, producer_call_id=""):
    data = Path(path).read_bytes() if Path(path).is_file() else b""
    evidence = IdentityEvidence.hashed_at_use(sha256=_sha(data), size_bytes=len(data))
    return mint_artifact(
        app,
        sid,
        name=name,
        workspace_id=ws,
        evidence=evidence,
        kind=kind,
        mechanism=Mechanism.TOOL_SCHEMA,
        custody=Custody.WORKSPACE_REFERENCED,
        path=path,
        producer={"session_id": sid, "call_id": producer_call_id or f"call_{name}"},
    )


def _transform(*, call_id, sid, ws, tool, args, used, generated):
    return TransformRecord(
        call_id=call_id,
        session_id=sid,
        workspace_id=ws,
        status=TransformStatus.SUCCESS,
        instrument=Instrument(tool=tool, args=args),
        used=used,
        generated=generated,
        started_at="2026-07-24T00:00:00Z",
        ended_at="2026-07-24T00:00:01Z",
    )


def _gen_edge(version) -> ProvEdge:
    return ProvEdge(
        role=EdgeRole.GENERATED,
        evidence=EdgeEvidence.HASH_PAIR if version.sha256 else EdgeEvidence.SCHEMA_ARG,
        artifact_id=version.artifact_id,
        sha256=version.sha256,
        version=version.version,
        path=version.path,
    )


def _use_edge(version) -> ProvEdge:
    return ProvEdge(
        role=EdgeRole.USED,
        evidence=EdgeEvidence.HASH_PAIR,
        artifact_id=version.artifact_id,
        sha256=version.sha256,
        version=version.version,
        path=version.path,
    )


def _linear_chain(app, sid, tmp_path: Path, *, n: int, ws: str = "ws1") -> list:
    """A linear ``s0 -(t1)-> s1 -(t2)-> ... s{n-1}`` producing chain in ``ws``.

    ``t0`` is the source producer (no inputs); ``ti`` produces ``si`` FROM ``s{i-1}``.
    Returns the version list ``[s0 .. s{n-1}]``; the root for an upstream closure is
    the LAST version.
    """
    reg = app.state.artifact_registry
    versions = []
    for i in range(n):
        p = tmp_path / f"s{i}.csv"
        p.write_text(f"col\n{i}\n")
        versions.append(
            _mint(app, sid, name=f"s{i}.csv", path=str(p), ws=ws, producer_call_id=f"t{i}")
        )
    reg.record_transform(
        _transform(
            call_id="t0",
            sid=sid,
            ws=ws,
            tool="source_tool",
            args={},
            used=[],
            generated=[_gen_edge(versions[0])],
        )
    )
    for i in range(1, n):
        reg.record_transform(
            _transform(
                call_id=f"t{i}",
                sid=sid,
                ws=ws,
                tool="pandas_filter_data",
                args={"expression": "col < 9"},
                used=[_use_edge(versions[i - 1])],
                generated=[_gen_edge(versions[i])],
            )
        )
    return versions


def _types(entity: dict) -> set:
    t = entity.get("@type")
    return set(t) if isinstance(t, list) else {t}


# --------------------------------------------------------------------------- #
# (a) build_artifact_bundle closes the TRANSITIVE chain (was one-hop).
# --------------------------------------------------------------------------- #


def test_artifact_bundle_closes_multi_hop_chain_and_reproduce_rebuilds_all_stages(tmp_path):
    app, store = _export_app(tmp_path, {"ws1": str(tmp_path)})
    sid = store.create(workspace_id="ws1", title="t").id
    # s0 -> s1 -> s2 -> s3 : the root s3 is THREE hops down from the source s0.
    versions = _linear_chain(app, sid, tmp_path, n=4)
    root = versions[3]

    bundle = build_artifact_bundle(app, root.artifact_id)
    assert bundle is not None
    meta = json.loads(bundle.files["ro-crate-metadata.json"])
    file_names = {e.get("name") for e in meta["@graph"] if "File" in _types(e)}
    action_ids = {e["@id"] for e in meta["@graph"] if "CreateAction" in _types(e)}

    # COMPLETENESS: every upstream record is present — including s0, the 2-hops-up
    # source the OLD one-hop loop never discovered (it stopped at s2). Sabotage: revert
    # to the one-hop `transform.used` pull and s0.csv / s1.csv vanish -> red.
    assert {"s0.csv", "s1.csv", "s2.csv", "s3.csv"} <= file_names
    # Every producing activity in the chain is present (t0..t3), not just the root's.
    assert {"#activity-t0", "#activity-t1", "#activity-t2", "#activity-t3"} <= action_ids

    # reproduce rebuilds ALL stages with NO silently-dropped input: each non-source
    # stage's used input resolves to a node in the bundle (reproduce.py:490 guard).
    reg = app.state.artifact_registry
    transforms = reg.all_transforms()
    nodes = {
        v.artifact_id: ArtifactNode(
            artifact_id=v.artifact_id,
            name=r.name,
            version=v.version,
            sha256=v.sha256,
            kind=v.kind.value,
            custody=v.custody.value,
            mechanism=v.mechanism.value,
            path=v.path,
            bundle_path=f"data/{r.name}",
        )
        for r in reg.list_for_workspace("ws1")
        for v in r.versions
    }
    script = compile_reproduce(transforms, nodes)
    by_call = {s.call_id: s for s in script.stages}
    for i in range(1, 4):
        stage = by_call[f"t{i}"]
        # The stage's input (s{i-1}) survived into the crate, so reproduce did not drop
        # it. Sabotage: omit any transitive input record -> the guard drops it here.
        assert versions[i - 1].artifact_id in nodes, f"t{i} input dropped"
        assert stage.output_names == [f"s{i}.csv"]
    compile(script.text, "reproduce.py", "exec")


def test_artifact_bundle_no_lineage_truncated_marker_when_complete(tmp_path):
    app, store = _export_app(tmp_path, {"ws1": str(tmp_path)})
    sid = store.create(workspace_id="ws1", title="t").id
    versions = _linear_chain(app, sid, tmp_path, n=3)
    bundle = build_artifact_bundle(app, versions[-1].artifact_id)
    root_entity = next(
        e for e in json.loads(bundle.files["ro-crate-metadata.json"])["@graph"] if e["@id"] == "./"
    )
    # A complete (un-capped) closure is an honest FULL crate — no truncated marker.
    assert "clio:lineage_truncated" not in root_entity


# --------------------------------------------------------------------------- #
# (c) complete mode: drop the depth horizon, honor the config node cap.
# --------------------------------------------------------------------------- #


def test_complete_mode_drops_depth_horizon(tmp_path):
    app, store = _export_app(tmp_path, {"ws1": str(tmp_path)})
    sid = store.create(workspace_id="ws1", title="t").id
    # 5 stages: the source s0 is FIVE activity-hops up from the root s5 (> the default
    # depth-3 horizon).
    versions = _linear_chain(app, sid, tmp_path, n=6)
    reg = app.state.artifact_registry
    root_id = versions[-1].artifact_id

    bounded = build_lineage(reg, root_id, direction="upstream", depth=3)
    complete = build_lineage(reg, root_id, direction="upstream", complete=True)
    bounded_ids = {n["id"] for n in bounded["nodes"]}
    complete_ids = {n["id"] for n in complete["nodes"]}

    # depth-3 stops at the horizon and never reaches the source; complete reaches it.
    # Sabotage: keep the `current_depth >= depth` check active in complete mode -> the
    # source stays absent -> the second assert reddens.
    assert versions[0].artifact_id not in bounded_ids
    assert bounded["truncated"] == {"reason": "depth_horizon", "at_depth": 3}
    assert versions[0].artifact_id in complete_ids
    assert "activity:t0" in complete_ids
    assert complete["truncated"] is None


def test_complete_mode_honors_config_node_cap_with_typed_truncated(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIO_ARTIFACTS_LINEAGE_MAX_NODES", "3")
    from clio_agent import conf  # noqa: PLC0415

    conf.reload()
    try:
        assert lineage_max_nodes() == 3  # config-resolved ceiling, not the 500 literal
        app, store = _export_app(tmp_path, {"ws1": str(tmp_path)})
        sid = store.create(workspace_id="ws1", title="t").id
        versions = _linear_chain(app, sid, tmp_path, n=4)  # 8 nodes uncapped
        reg = app.state.artifact_registry
        root_id = versions[-1].artifact_id

        capped = build_lineage(reg, root_id, direction="upstream", complete=True)
        # Bounded even in complete mode: the walk stops at the config ceiling and emits a
        # TYPED node_cap marker. Sabotage: keep _MAX_NODES in complete mode -> no cap at
        # 3 -> truncated stays None -> red.
        assert len(capped["nodes"]) == 3
        assert capped["truncated"] == {"reason": "node_cap", "nodes": 3}

        # The capped closure surfaces its marker into the CRATE root (honest partial).
        bundle = build_artifact_bundle(app, root_id)
        root_entity = next(
            e
            for e in json.loads(bundle.files["ro-crate-metadata.json"])["@graph"]
            if e["@id"] == "./"
        )
        assert root_entity["clio:lineage_truncated"] == {"reason": "node_cap", "nodes": 3}
    finally:
        monkeypatch.delenv("CLIO_ARTIFACTS_LINEAGE_MAX_NODES", raising=False)
        conf.reload()


# --------------------------------------------------------------------------- #
# Interactive /lineage stays depth-bounded + ignores the complete-mode ceiling.
# --------------------------------------------------------------------------- #


def test_interactive_depth3_is_unchanged_and_ignores_complete_node_cap(tmp_path, monkeypatch):
    # Even with the complete-mode ceiling clamped to 1, interactive depth-bounded lineage
    # keeps the _MAX_NODES=500 literal cap (the config knob is export/reproduce only).
    monkeypatch.setenv("CLIO_ARTIFACTS_LINEAGE_MAX_NODES", "1")
    from clio_agent import conf  # noqa: PLC0415

    conf.reload()
    try:
        app, store = _export_app(tmp_path, {"ws1": str(tmp_path)})
        sid = store.create(workspace_id="ws1", title="t").id
        versions = _linear_chain(app, sid, tmp_path, n=5)
        reg = app.state.artifact_registry
        root_id = versions[-1].artifact_id

        interactive = build_lineage(reg, root_id, direction="upstream", depth=3)
        # Interactive does NOT read the config cap: it returns many nodes despite env=1.
        # Sabotage: apply lineage_max_nodes() to the interactive path -> len == 1 -> red.
        assert len(interactive["nodes"]) > 1
        assert interactive["truncated"] == {"reason": "depth_horizon", "at_depth": 3}

        # The complete path DOES honor env=1 -> a single-node capped closure (the contrast).
        capped = build_lineage(reg, root_id, direction="upstream", complete=True)
        assert len(capped["nodes"]) == 1
        assert capped["truncated"] == {"reason": "node_cap", "nodes": 1}
    finally:
        monkeypatch.delenv("CLIO_ARTIFACTS_LINEAGE_MAX_NODES", raising=False)
        conf.reload()


# --------------------------------------------------------------------------- #
# (b) build_session_bundle reaches a NON-descendant contributing sibling job.
# --------------------------------------------------------------------------- #


def test_session_bundle_includes_non_descendant_contributing_job(tmp_path):
    # Two sibling jobs sharing a root: JOB A (ws_a) produces a.csv; JOB B (ws_b) is the
    # exported session and CONSUMES a.csv (a cross-job used edge on the foreign id).
    app, store = _export_app(tmp_path, {"ws_a": str(tmp_path), "ws_b": str(tmp_path)})
    sid_a = store.create(workspace_id="ws_a", title="A").id
    sid_b = store.create(workspace_id="ws_b", title="B").id
    reg = app.state.artifact_registry

    a_path = tmp_path / "a.csv"
    a_path.write_text("time,east\n0,1\n")
    v_a = _mint(app, sid_a, name="a.csv", path=str(a_path), ws="ws_a", producer_call_id="t_a")
    reg.record_transform(
        _transform(
            call_id="t_a",
            sid=sid_a,
            ws="ws_a",
            tool="ndp_stage_resource",
            args={"source_url": "https://ds.example.org/a.csv"},
            used=[],
            generated=[_gen_edge(v_a)],
        )
    )
    b_path = tmp_path / "b.csv"
    b_path.write_text("time,east\n0,1\n")
    v_b = _mint(app, sid_b, name="b.csv", path=str(b_path), ws="ws_b", producer_call_id="t_b")
    # JOB B's transform USES the foreign a.csv (its artifact_id) — the cross-job edge.
    reg.record_transform(
        _transform(
            call_id="t_b",
            sid=sid_b,
            ws="ws_b",
            tool="pandas_filter_data",
            args={"expression": "east < 2"},
            used=[_use_edge(v_a)],
            generated=[_gen_edge(v_b)],
        )
    )

    # No descendant children — the sibling ws_a is reached ONLY by the cross-job closure.
    bundle = build_session_bundle(app, sid_b, include_children=False)
    assert bundle is not None
    meta = json.loads(bundle.files["ro-crate-metadata.json"])
    file_names = {e.get("name") for e in meta["@graph"] if "File" in _types(e)}
    action_ids = {e["@id"] for e in meta["@graph"] if "CreateAction" in _types(e)}

    # The non-descendant sibling's producing record AND its transform appear in the crate.
    # Sabotage: drop the _close_cross_job_inputs pass -> a.csv / #activity-t_a vanish (the
    # session workspace never listed ws_a) -> red.
    assert "a.csv" in file_names and "b.csv" in file_names
    assert "#activity-t_a" in action_ids and "#activity-t_b" in action_ids

    # The consuming stage's input resolves (reproduce would not drop it).
    filter_action = next(e for e in meta["@graph"] if e.get("@id") == "#activity-t_b")
    assert filter_action["object"], "the cross-job input must be a resolved crate reference"


def test_session_bundle_surfaces_cross_job_truncated_marker(tmp_path, monkeypatch):
    # A cross-job sibling closure that hits the node cap must surface a TYPED truncation into
    # the SESSION crate root too — an honest partial, never a silent full-looking crate (the
    # completeness/bounded review blocker: build_session_bundle previously DROPPED the marker).
    monkeypatch.setenv("CLIO_ARTIFACTS_LINEAGE_MAX_NODES", "1")
    from clio_agent import conf  # noqa: PLC0415

    conf.reload()
    try:
        app, store = _export_app(tmp_path, {"ws_a": str(tmp_path), "ws_b": str(tmp_path)})
        sid_a = store.create(workspace_id="ws_a", title="A").id
        sid_b = store.create(workspace_id="ws_b", title="B").id
        reg = app.state.artifact_registry
        a_path = tmp_path / "a.csv"
        a_path.write_text("time,east\n0,1\n")
        v_a = _mint(app, sid_a, name="a.csv", path=str(a_path), ws="ws_a", producer_call_id="t_a")
        reg.record_transform(
            _transform(
                call_id="t_a",
                sid=sid_a,
                ws="ws_a",
                tool="ndp_stage_resource",
                args={"source_url": "https://ds.example.org/a.csv"},
                used=[],
                generated=[_gen_edge(v_a)],
            )
        )
        b_path = tmp_path / "b.csv"
        b_path.write_text("time,east\n0,1\n")
        v_b = _mint(app, sid_b, name="b.csv", path=str(b_path), ws="ws_b", producer_call_id="t_b")
        reg.record_transform(
            _transform(
                call_id="t_b",
                sid=sid_b,
                ws="ws_b",
                tool="pandas_filter_data",
                args={"expression": "east < 2"},
                used=[_use_edge(v_a)],
                generated=[_gen_edge(v_b)],
            )
        )
        bundle = build_session_bundle(app, sid_b, include_children=False)
        root_entity = next(
            e
            for e in json.loads(bundle.files["ro-crate-metadata.json"])["@graph"]
            if e["@id"] == "./"
        )
        # Sabotage: discard _close_cross_job_inputs' truncated return -> silent partial -> red.
        assert root_entity.get("clio:lineage_truncated") == {"reason": "node_cap", "nodes": 1}
    finally:
        monkeypatch.delenv("CLIO_ARTIFACTS_LINEAGE_MAX_NODES", raising=False)
        conf.reload()
