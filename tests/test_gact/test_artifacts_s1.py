"""Unit tests for the S1 artifacts floor (#966/#967).

Covers the record model, the registry fold semantics (idempotency + conflict),
the three-seam minting funnel (observer tool-declared + fs_write harness + pack
declared), identity hashing incl. the oversize stat-pin, the SessionStore badge
index round-trip, and the boot fold (JSONL fallback + typed capture_released).

Each key lock carries a sabotage note: the referenced neutralization turns the
named assertion red, proving the test binds the invariant (not a tautology).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc.live import build_event_content
from clio_agent.gact.artifacts import designation
from clio_agent.gact.artifacts.minting import (
    compute_identity,
    mint_artifact,
    mint_pack_declared_paths,
    mint_tool_declared_outputs,
)
from clio_agent.gact.artifacts.records import (
    ArtifactKind,
    ArtifactRecord,
    ArtifactVersion,
    Custody,
    EvidenceClass,
    IdentityEvidence,
    Mechanism,
    new_artifact_id,
)
from clio_agent.gact.artifacts.registry import (
    ArtifactRegistry,
    RegistryFoldOnLoopError,
    build_session_index,
    get_registry,
    rebuild_registry_at_boot,
    rehydrate_session_index,
)
from clio_agent.gact.sessions import SessionStore

# --------------------------------------------------------------------------- #
# Fakes: a minimal app whose state carries just what mint/emit read.
# --------------------------------------------------------------------------- #


class _CapturingArc:
    """Fake ARC whose ``record_semantic_event`` captures the routed event."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def record_semantic_event(self, event: Any) -> Any:
        self.events.append(event)
        return event


class _FakeWorkspaces:
    """Minimal workspace store: maps ``workspace_id -> root_path`` for containment."""

    def __init__(self, roots: dict[str, str]) -> None:
        self._roots = roots

    def get(self, wid: str) -> Any:
        root = self._roots.get(wid)
        return SimpleNamespace(root_path=root) if root else None


class _FoldArc:
    """Fake ARC exposing ``iter_event_contents`` in the ``build_event_content`` shape.

    ``_fold_from_arc`` reaches ``arc._live.iter_event_contents()`` — this fake
    yields the exact content dicts ARC's live observer persists per ``_events``
    segment, so a boot fold reads the ARC primary source under test.
    """

    def __init__(self, contents: list[dict[str, Any]]) -> None:
        self._contents = contents
        self._live = SimpleNamespace(iter_event_contents=lambda: iter(self._contents))


def _make_app(tmp_path: Path, *, with_sink: bool = True, workspace_root: Path | None = None):
    """Build a fake app with a real SessionStore + capturing arc + workspaces.

    With ``with_sink=False`` the semantic sink is absent, so ``_emit_semantic_event``
    short-circuits (mint still folds the payload it builds itself). ``ws1``'s
    containment root defaults to ``tmp_path`` so designated outputs written there
    are inside the workspace (owner decision 10).
    """
    store = SessionStore(path=tmp_path / "sessions.json")
    sess = store.create(workspace_id="ws1", title="t")
    arc = _CapturingArc()
    root = workspace_root if workspace_root is not None else tmp_path
    state = SimpleNamespace(
        sessions=store,
        arc=arc,
        workspaces=_FakeWorkspaces({"ws1": str(root)}),
        semantic_event_sink=(object() if with_sink else None),
        semantic_trace_detail_level="semantic",
        semantic_trace_backend=None,
        artifact_registry=None,
    )
    app = SimpleNamespace(state=state)
    return app, sess, arc


def _artifact_events(arc: _CapturingArc) -> list[Any]:
    return [e for e in arc.events if getattr(e, "event_type", "") == "artifact.created"]


# --------------------------------------------------------------------------- #
# Record model
# --------------------------------------------------------------------------- #


def test_new_artifact_id_is_relay_format():
    aid = new_artifact_id()
    assert aid.startswith("artifact_")
    assert len(aid) == len("artifact_") + 32  # uuid4 hex


def test_version_relay_ref_carries_id_and_sha():
    ev = IdentityEvidence.hashed_at_use(sha256="a" * 64, size_bytes=12)
    ver = ArtifactVersion(evidence=ev, kind=ArtifactKind.IMAGE, mechanism=Mechanism.TOOL_SCHEMA)
    ref = ver.to_artifact_ref()
    assert ref["artifact_id"] == ver.artifact_id
    assert ref["sha256"] == "a" * 64
    assert ref["metadata"]["kind"] == "image"


def test_record_chain_increments_and_tracks_latest():
    rec = ArtifactRecord(workspace_id="ws1", name="plot.png")
    assert rec.next_version_number() == 1
    v1 = ArtifactVersion(
        version=1, evidence=IdentityEvidence.stat_pinned(size_bytes=1), kind=ArtifactKind.IMAGE
    )
    rec.add_version(v1)
    assert rec.next_version_number() == 2
    assert rec.aliases["latest"] == 1
    assert rec.head is v1


# --------------------------------------------------------------------------- #
# Registry fold semantics (replay from synthetic events)
# --------------------------------------------------------------------------- #


def _payload(
    *,
    event_id: str,
    name: str,
    version: int,
    sha: str | None,
    ws: str = "ws1",
    kind: str = "image",
    mechanism: str = "tool-schema",
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "artifact_id": new_artifact_id(),
        "workspace_id": ws,
        "name": name,
        "version": version,
        "kind": kind,
        "custody": "workspace-referenced",
        "mechanism": mechanism,
        "sha256": sha,
        "size_bytes": 10,
        "evidence": {"evidence_class": "hashed-at-use" if sha else "stat-pinned"},
    }


def test_replay_builds_version_chain():
    reg = ArtifactRegistry()
    reg.fold_payload(_payload(event_id="e1", name="d.csv", version=1, sha="a" * 64, kind="dataset"))
    reg.fold_payload(_payload(event_id="e2", name="d.csv", version=2, sha="b" * 64, kind="dataset"))
    rec = reg.get("ws1", "d.csv")
    assert rec is not None
    assert [v.version for v in rec.versions] == [1, 2]
    assert rec.head.sha256 == "b" * 64
    assert reg.count() == 1


def test_fold_dedup_by_event_id():
    reg = ArtifactRegistry()
    p = _payload(event_id="dup", name="d.csv", version=1, sha="a" * 64)
    assert reg.fold_payload(p).applied is True
    r2 = reg.fold_payload(p)
    # Sabotage: drop the `event.event_id in self._seen_event_ids` guard in
    # ArtifactRegistry._fold_event -> this becomes applied/second version -> red.
    assert r2.applied is False
    assert r2.reason == "duplicate_event_id"
    assert len(reg.get("ws1", "d.csv").versions) == 1


def test_fold_same_sha_replay_is_noop():
    reg = ArtifactRegistry()
    reg.fold_payload(_payload(event_id="e1", name="d.csv", version=1, sha="a" * 64))
    r2 = reg.fold_payload(_payload(event_id="e2", name="d.csv", version=1, sha="a" * 64))
    assert r2.applied is False
    assert r2.reason == "same_sha_replay"
    assert len(reg.get("ws1", "d.csv").versions) == 1


def test_fold_conflict_keeps_first_and_records_typed_reason():
    reg = ArtifactRegistry()
    reg.fold_payload(_payload(event_id="e1", name="d.csv", version=1, sha="a" * 64))
    r2 = reg.fold_payload(_payload(event_id="e2", name="d.csv", version=1, sha="c" * 64))
    # Sabotage: make the conflict branch overwrite the head sha -> kept_sha assertion red.
    assert r2.applied is False
    assert r2.reason == "fold_conflict"
    assert reg.get("ws1", "d.csv").head.sha256 == "a" * 64  # first kept
    assert len(reg.fold_conflicts) == 1
    assert reg.fold_conflicts[0]["kept_sha256"] == "a" * 64
    assert reg.fold_conflicts[0]["rejected_sha256"] == "c" * 64


def test_fold_malformed_payload_typed_not_crash():
    reg = ArtifactRegistry()
    assert reg.fold_payload({"workspace_id": "ws1"}).reason == "malformed"  # no name
    assert reg.fold_payload({"name": "x", "version": 0}).reason == "malformed"  # bad version


# --------------------------------------------------------------------------- #
# Identity hashing + oversize stat-pin
# --------------------------------------------------------------------------- #


def test_compute_identity_hashes_small_file(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"hello")
    ev = compute_identity(f)
    import hashlib

    assert ev.evidence_class == EvidenceClass.HASHED_AT_USE
    assert ev.sha256 == hashlib.sha256(b"hello").hexdigest()
    assert ev.size_bytes == 5


def test_compute_identity_oversize_is_stat_pinned(tmp_path):
    f = tmp_path / "big.nc"
    f.write_bytes(b"0123456789")
    ev = compute_identity(f, max_bytes=4)  # below the 10-byte file
    # Sabotage: drop the `size > max_bytes` branch in _stat_and_hash -> class HASHED -> red.
    assert ev.evidence_class == EvidenceClass.STAT_PINNED
    assert ev.sha256 is None
    assert ev.size_bytes == 10


# --------------------------------------------------------------------------- #
# Seam (a): observer tool-declared mint
# --------------------------------------------------------------------------- #


def test_mint_tool_declared_output_emits_event_with_call_id_and_sha256(tmp_path):
    app, sess, arc = _make_app(tmp_path)
    png = tmp_path / "timeseries.png"
    png.write_bytes(b"\x89PNG\r\n")
    minted = mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="plot_timeseries",
        effective_args={"data_path": "/data/in.csv", "output_path": str(png)},
        call_id="call_abc123",
        workspace_id="ws1",
    )
    assert len(minted) == 1
    events = _artifact_events(arc)
    # Sabotage: neutralize the mint funnel (skip _emit_semantic_event in
    # minting.mint_artifact, or the observe_tool_completion call in the observer)
    # -> zero events -> this assertion red (the observer-mint lock).
    assert len(events) == 1
    payload = events[0].payload
    assert payload["producer"]["call_id"] == "call_abc123"
    assert payload["mechanism"] == "tool-schema"
    assert payload["kind"] == "image"
    import hashlib

    assert payload["sha256"] == hashlib.sha256(b"\x89PNG\r\n").hexdigest()
    assert payload["name"] == "timeseries.png"


def test_mint_skips_non_artifact_and_absent_paths(tmp_path):
    app, sess, arc = _make_app(tmp_path)
    minted = mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="t",
        # output="stdout" is not an artifact suffix; missing.png does not exist.
        effective_args={"output": "stdout", "save_path": str(tmp_path / "missing.png")},
        call_id="c1",
        workspace_id="ws1",
    )
    assert minted == []
    assert _artifact_events(arc) == []


def test_reserved_plan_kind_cannot_mint(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    ev = IdentityEvidence.stat_pinned(size_bytes=1)
    with pytest.raises(ValueError, match="reserved"):
        mint_artifact(
            app,
            sess.id,
            name="p",
            workspace_id="ws1",
            evidence=ev,
            kind=ArtifactKind.PLAN,
            mechanism=Mechanism.MODEL,
        )


# --------------------------------------------------------------------------- #
# Seam (b): fs_write sha256
# --------------------------------------------------------------------------- #


def test_fs_write_returns_sha256_of_on_disk_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    from clio_agent.tools.fs_write import write_text_with_policy

    target = tmp_path / "report.md"
    result = write_text_with_policy(str(target), "# Title\nbody\n")
    import hashlib

    on_disk = target.read_bytes()
    # Sabotage: drop the sha256 key from write_text_with_policy -> KeyError -> red.
    assert result["sha256"] == hashlib.sha256(on_disk).hexdigest()
    assert result["size_bytes"] == len(on_disk)


# --------------------------------------------------------------------------- #
# Seam (c): pack-declared paths
# --------------------------------------------------------------------------- #


def test_pack_declared_paths_extracts_declared_specs():
    ws = {"outputs": {"report_path": "/w/report.md", "plot_path": "/w/p.png"}, "misc": {}}
    specs = [("outputs", "report_path"), ("outputs", "plot_path"), ("outputs", "absent")]
    assert designation.pack_declared_paths(ws, specs) == ["/w/report.md", "/w/p.png"]


def test_mint_pack_declared_paths(tmp_path):
    app, sess, arc = _make_app(tmp_path)
    report = tmp_path / "report.md"
    report.write_text("# r", encoding="utf-8")
    ws_state = {"outputs": {"report_path": str(report)}}
    minted = mint_pack_declared_paths(
        app,
        sess.id,
        workflow_state=ws_state,
        path_specs=[("outputs", "report_path")],
        workspace_id="ws1",
    )
    assert len(minted) == 1
    ev = _artifact_events(arc)[0].payload
    assert ev["mechanism"] == "harness"
    assert ev["producer"]["designation"] == "pack-declared"
    assert ev["kind"] == "report"


# --------------------------------------------------------------------------- #
# SessionStore badge index round-trip
# --------------------------------------------------------------------------- #


def test_session_index_stamp_and_rehydrate_roundtrip(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    png = tmp_path / "plot.png"
    png.write_bytes(b"x")
    mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="t",
        effective_args={"output_path": str(png)},
        call_id="c1",
        workspace_id="ws1",
    )
    index = rehydrate_session_index(app, sess.id)
    assert index["count"] == 1
    assert index["names"]["plot.png"]["kind"] == "image"
    assert index["names"]["plot.png"]["v"] == 1
    assert index["names_truncated"] is False


def test_session_index_is_bounded():
    reg = ArtifactRegistry()
    for i in range(70):
        reg.fold_payload(
            _payload(event_id=f"e{i}", name=f"f{i:03d}.png", version=1, sha=f"{i:064x}")
        )
    index = build_session_index(reg, "ws1")
    assert index["count"] == 70
    assert len(index["names"]) == 64  # _SESSION_INDEX_NAME_CAP
    assert index["names_truncated"] is True


# --------------------------------------------------------------------------- #
# Boot fold (JSONL fallback + capture_released)
# --------------------------------------------------------------------------- #


def test_boot_fold_from_jsonl_trace(tmp_path, monkeypatch):
    trace_dir = tmp_path / "semantic_traces"
    trace_dir.mkdir()
    line = {
        "event_type": "artifact.created",
        "event_id": "e1",
        "payload": _payload(event_id="", name="d.csv", version=1, sha="a" * 64, kind="dataset"),
    }
    (trace_dir / "sess_x.semantic.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    monkeypatch.setattr("clio_agent.gact.runtime.globals._PROCESS_ARC", None)
    state = SimpleNamespace(
        arc=None,
        semantic_trace_backend=SimpleNamespace(path=trace_dir),
        artifact_registry=None,
    )
    app = SimpleNamespace(state=state)
    reg = rebuild_registry_at_boot(app)
    assert reg.count() == 1
    assert reg.get("ws1", "d.csv") is not None
    assert reg.capture_released is None


def test_boot_fold_capture_released_when_no_source(tmp_path, monkeypatch):
    # No process ARC + no trace backend => NEITHER source reachable (finding [11]:
    # unreachable, not merely empty). Null _PROCESS_ARC so the fallback can't make
    # ARC spuriously reachable when a prior suite test booted a real ARC.
    monkeypatch.setattr("clio_agent.gact.runtime.globals._PROCESS_ARC", None)
    state = SimpleNamespace(arc=None, semantic_trace_backend=None, artifact_registry=None)
    app = SimpleNamespace(state=state)
    reg = rebuild_registry_at_boot(app)
    # Sabotage: silently return an empty registry without the capture_released tag
    # -> this typed-degrade assertion red (no-silent-fallback lock).
    assert reg.capture_released is not None
    assert reg.capture_released["reason"] == "capture_released"


# --------------------------------------------------------------------------- #
# Finding [1/6]: content-hash dedup is LIVE at the mint (no phantom v2)
# --------------------------------------------------------------------------- #


def test_same_sha_dedup_no_op_across_seam_a_and_seam_c(tmp_path):
    """seam(a)+seam(c) on one byte-identical file => exactly ONE version + ONE event."""
    app, sess, arc = _make_app(tmp_path)
    report = tmp_path / "report.md"
    report.write_text("# deliverable\n", encoding="utf-8")

    # Seam (a): tool-declared output mints v1 and emits.
    mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="write_report",
        effective_args={"output_path": str(report)},
        call_id="c1",
        workspace_id="ws1",
    )
    # Seam (c): pack declares the SAME unchanged path at finalize -> dedup no-op.
    mint_pack_declared_paths(
        app,
        sess.id,
        workflow_state={"outputs": {"report_path": str(report)}},
        path_specs=[("outputs", "report_path")],
        workspace_id="ws1",
    )
    reg = get_registry(app)
    # Sabotage: revert mint_artifact to next_version_number() without registry.mint
    # dedup -> seam (c) re-mints a phantom v2 with identical sha -> both assertions red.
    assert len(reg.get("ws1", "report.md").versions) == 1
    assert len(_artifact_events(arc)) == 1


def test_idempotent_tool_rerun_does_not_grow_chain_but_changed_content_does(tmp_path):
    app, sess, arc = _make_app(tmp_path)
    csv = tmp_path / "out.csv"
    csv.write_text("a,b\n1,2\n", encoding="utf-8")
    args = {"output_path": str(csv)}
    mint_tool_declared_outputs(
        app, sess.id, tool_name="t", effective_args=args, call_id="c1", workspace_id="ws1"
    )
    mint_tool_declared_outputs(
        app, sess.id, tool_name="t", effective_args=args, call_id="c2", workspace_id="ws1"
    )
    reg = get_registry(app)
    assert len(reg.get("ws1", "out.csv").versions) == 1  # idempotent re-run: no growth
    assert len(_artifact_events(arc)) == 1
    # Changed content mints a genuine v2. Under S4 (#970) v1 stays artifact.created
    # while v2+ emits artifact.version.added, so the created count stays 1.
    csv.write_text("a,b\n9,9\n", encoding="utf-8")
    mint_tool_declared_outputs(
        app, sess.id, tool_name="t", effective_args=args, call_id="c3", workspace_id="ws1"
    )
    assert [v.version for v in reg.get("ws1", "out.csv").versions] == [1, 2]
    assert len(_artifact_events(arc)) == 1  # only v1 emits artifact.created
    added = [e for e in arc.events if getattr(e, "event_type", "") == "artifact.version.added"]
    assert len(added) == 1  # v2 emits artifact.version.added


# --------------------------------------------------------------------------- #
# Finding [3/10]: atomic version assignment under one lock (no TOCTOU)
# --------------------------------------------------------------------------- #


def test_registry_mint_concurrent_distinct_content_assigns_sequential_versions():
    reg = ArtifactRegistry()
    n = 24
    barrier = threading.Barrier(n)
    outcomes: list[Any] = []
    guard = threading.Lock()

    def worker(i: int) -> None:
        ev = IdentityEvidence.hashed_at_use(sha256=f"{i:064x}", size_bytes=10)
        barrier.wait()  # maximize contention on the read-modify-write
        out = reg.mint(
            workspace_id="ws1",
            name="d.csv",
            event_id=f"e{i}",
            kind=ArtifactKind.DATASET,
            custody=Custody.WORKSPACE_REFERENCED,
            mechanism=Mechanism.TOOL_SCHEMA,
            evidence=ev,
            producer={},
            path="",
            created_at="",
            annotation="",
        )
        with guard:
            outcomes.append(out)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rec = reg.get("ws1", "d.csv")
    # Sabotage: split assignment across get()+fold() (the old TOCTOU) -> two threads
    # compute the same version -> a version is lost/keep-first-dropped -> not 1..n.
    assert sorted(v.version for v in rec.versions) == list(range(1, n + 1))
    assert len({v.sha256 for v in rec.versions}) == n  # every distinct content kept
    assert all(o.created for o in outcomes)


def test_registry_mint_concurrent_same_content_collapses_to_one_version():
    reg = ArtifactRegistry()
    n = 16
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        ev = IdentityEvidence.hashed_at_use(sha256="a" * 64, size_bytes=10)
        barrier.wait()
        reg.mint(
            workspace_id="ws1",
            name="d.csv",
            event_id=f"e{i}",
            kind=ArtifactKind.DATASET,
            custody=Custody.WORKSPACE_REFERENCED,
            mechanism=Mechanism.TOOL_SCHEMA,
            evidence=ev,
            producer={},
            path="",
            created_at="",
            annotation="",
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(reg.get("ws1", "d.csv").versions) == 1


# --------------------------------------------------------------------------- #
# Finding [2]: union-fold BOTH sources (deleted-session JSONL not shadowed)
# --------------------------------------------------------------------------- #


def test_boot_fold_unions_arc_and_jsonl_sources(tmp_path):
    arc_payload = _payload(
        event_id="a1", name="from_arc.csv", version=1, sha="a" * 64, kind="dataset"
    )
    arc_content = {"event_type": "artifact.created", "payload": arc_payload}
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    jsonl_payload = _payload(
        event_id="j1", name="from_jsonl.csv", version=1, sha="b" * 64, kind="dataset"
    )
    (trace_dir / "s.semantic.jsonl").write_text(
        json.dumps({"event_type": "artifact.created", "event_id": "j1", "payload": jsonl_payload})
        + "\n",
        encoding="utf-8",
    )
    state = SimpleNamespace(
        arc=_FoldArc([arc_content]),
        semantic_trace_backend=SimpleNamespace(path=trace_dir),
        artifact_registry=None,
    )
    app = SimpleNamespace(state=state)
    reg = rebuild_registry_at_boot(app)
    # Sabotage: restore ARC-first early return (skip JSONL when ARC folded any)
    # -> from_jsonl.csv (a deleted-session artifact) vanishes -> this assertion red.
    assert reg.get("ws1", "from_arc.csv") is not None
    assert reg.get("ws1", "from_jsonl.csv") is not None
    assert reg.count() == 2
    assert reg.capture_released is None


# --------------------------------------------------------------------------- #
# Finding [11]: empty-vs-unknown (capture_released ONLY when unreachable)
# --------------------------------------------------------------------------- #


def test_boot_fold_reachable_but_empty_is_not_a_degrade(tmp_path):
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()  # present + readable, but holds no artifact traces
    state = SimpleNamespace(
        arc=_FoldArc([]),  # reachable ARC, zero artifact events
        semantic_trace_backend=SimpleNamespace(path=trace_dir),
        artifact_registry=None,
    )
    app = SimpleNamespace(state=state)
    reg = rebuild_registry_at_boot(app)
    # Sabotage: treat empty-iter as unreachable (old None conflation) -> a false
    # capture_released fires on a healthy empty boot -> this assertion red.
    assert reg.count() == 0
    assert reg.capture_released is None


def test_boot_fold_unreachable_source_is_a_degrade(tmp_path, monkeypatch):
    monkeypatch.setattr("clio_agent.gact.runtime.globals._PROCESS_ARC", None)
    missing = tmp_path / "does_not_exist"  # backend configured but dir absent
    state = SimpleNamespace(
        arc=None,
        semantic_trace_backend=SimpleNamespace(path=missing),
        artifact_registry=None,
    )
    app = SimpleNamespace(state=state)
    reg = rebuild_registry_at_boot(app)
    assert reg.capture_released is not None
    assert reg.capture_released["reason"] == "capture_released"


# --------------------------------------------------------------------------- #
# Finding [5]: workspace containment before any stat/hash (owner decision 10)
# --------------------------------------------------------------------------- #


def test_pack_declared_containment_rejects_traversal_and_absolute_escape(tmp_path):
    app, sess, arc = _make_app(tmp_path)  # ws1 root == tmp_path
    outside = tmp_path.parent / "secret_outside.md"
    outside.write_text("secret", encoding="utf-8")
    ws_state = {"outputs": {"abs": str(outside), "trav": "../../etc/passwd"}}
    minted = mint_pack_declared_paths(
        app,
        sess.id,
        workflow_state=ws_state,
        path_specs=[("outputs", "abs"), ("outputs", "trav")],
        workspace_id="ws1",
    )
    # Sabotage: drop the _contained() gate in mint_pack_declared_paths -> the outside
    # absolute path is read+hashed+minted -> minted non-empty / an event appears -> red.
    assert minted == []
    assert _artifact_events(arc) == []


def test_tool_declared_containment_rejects_outside_path(tmp_path):
    app, sess, arc = _make_app(tmp_path)
    outside = tmp_path.parent / "escape.png"
    outside.write_bytes(b"\x89PNG")
    minted = mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="plot",
        effective_args={"output_path": str(outside)},
        call_id="c1",
        workspace_id="ws1",
    )
    assert minted == []
    assert _artifact_events(arc) == []


def test_containment_unresolved_workspace_skips_mint(tmp_path):
    # No workspaces store on app.state => root unresolvable => skip (never read).
    app, sess, arc = _make_app(tmp_path)
    app.state.workspaces = None
    png = tmp_path / "plot.png"
    png.write_bytes(b"x")
    minted = mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="t",
        effective_args={"output_path": str(png)},
        call_id="c1",
        workspace_id="ws1",
    )
    assert minted == []
    assert _artifact_events(arc) == []


# --------------------------------------------------------------------------- #
# Finding [9]: boot fold is loop-safe (never runs on the event loop)
# --------------------------------------------------------------------------- #


def test_get_registry_on_event_loop_raises_typed(tmp_path):
    app, _sess, _arc = _make_app(tmp_path)  # artifact_registry starts None

    async def access() -> Any:
        return get_registry(app)  # first access on the loop thread

    with pytest.raises(RegistryFoldOnLoopError):
        asyncio.run(access())


def test_get_registry_offloaded_to_thread_builds_without_raising(tmp_path):
    app, _sess, _arc = _make_app(tmp_path)

    async def access() -> Any:
        return await asyncio.to_thread(get_registry, app)  # off-loop worker

    reg = asyncio.run(access())
    assert reg is not None
    assert getattr(app.state, "artifact_registry", None) is reg


# --------------------------------------------------------------------------- #
# Finding [4]: JSONL boot fold pre-filters before json.loads + streams
# --------------------------------------------------------------------------- #


def test_jsonl_boot_fold_prefilters_before_decode(tmp_path, monkeypatch):
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    lines = [json.dumps({"event_type": "lm.call", "payload": {"i": i}}) for i in range(100_000)]
    for i in range(10):
        lines.append(
            json.dumps(
                {
                    "event_type": "artifact.created",
                    "event_id": f"e{i}",
                    "payload": _payload(
                        event_id="", name=f"a{i}.csv", version=1, sha=f"{i:064x}", kind="dataset"
                    ),
                }
            )
        )
    (trace_dir / "s.semantic.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    calls = {"n": 0}
    real_loads = json.loads

    def counting_loads(s: Any, *a: Any, **k: Any) -> Any:
        calls["n"] += 1
        return real_loads(s, *a, **k)

    monkeypatch.setattr("json.loads", counting_loads)
    monkeypatch.setattr("clio_agent.gact.runtime.globals._PROCESS_ARC", None)
    state = SimpleNamespace(
        arc=None, semantic_trace_backend=SimpleNamespace(path=trace_dir), artifact_registry=None
    )
    app = SimpleNamespace(state=state)
    reg = rebuild_registry_at_boot(app)
    # Sabotage: remove the `if ARTIFACT_CREATED_EVENT not in raw: continue` pre-filter
    # -> all 100_010 lines are decoded -> this bound blows past 12 -> red.
    assert reg.count() == 10
    assert calls["n"] <= 12


# --------------------------------------------------------------------------- #
# Finding [8]: seam (a) skips a pre-existing untouched designated file
# --------------------------------------------------------------------------- #


def test_seam_a_skips_pre_existing_untouched_file(tmp_path):
    app, sess, arc = _make_app(tmp_path)
    png = tmp_path / "plot.png"
    png.write_bytes(b"\x89PNG")
    # A first, genuine mint puts the content in the chain.
    mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="t",
        effective_args={"output_path": str(png)},
        call_id="c1",
        workspace_id="ws1",
    )
    assert len(_artifact_events(arc)) == 1
    # A later call whose start is AFTER the file's mtime, content already versioned.
    later = time.time() + 60
    minted = mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="reader",
        effective_args={"output_path": str(png)},
        call_id="c2",
        workspace_id="ws1",
        call_started_at=later,
    )
    # Sabotage: drop the _is_pre_existing_untouched pre-check -> the file is re-hashed
    # and deduped, appending the existing version to `minted` -> minted != [] -> red.
    assert minted == []
    assert len(_artifact_events(arc)) == 1
    reg = get_registry(app)
    assert len(reg.get("ws1", "plot.png").versions) == 1


# --------------------------------------------------------------------------- #
# Finding [12]: ARC-primary fold coverage + emitter<->folder parity
# --------------------------------------------------------------------------- #


def _artifact_event_obj(payload: dict[str, Any]) -> SimpleNamespace:
    """An event object in the shape build_event_content consumes (emitter side)."""
    return SimpleNamespace(
        event_type="artifact.created",
        payload=payload,
        status="completed",
        summary="",
        actor={},
        subject={},
        provider={},
        occurred_at="",
        trace_id="",
    )


def test_boot_fold_from_arc_events_primary_source(tmp_path):
    payload = _payload(event_id="a1", name="d.csv", version=1, sha="a" * 64, kind="dataset")
    content = build_event_content(_artifact_event_obj(payload))
    assert content is not None
    state = SimpleNamespace(
        arc=_FoldArc([content]), semantic_trace_backend=None, artifact_registry=None
    )
    app = SimpleNamespace(state=state)
    reg = rebuild_registry_at_boot(app)
    assert reg.get("ws1", "d.csv") is not None
    assert reg.capture_released is None


def test_arc_fold_matches_direct_fold_parity():
    """The ARC content shape folds identically to the raw payload (emitter<->folder)."""
    payload = _payload(event_id="a1", name="d.csv", version=1, sha="a" * 64, kind="dataset")
    content = build_event_content(_artifact_event_obj(payload))
    state = SimpleNamespace(
        arc=_FoldArc([content]), semantic_trace_backend=None, artifact_registry=None
    )
    app = SimpleNamespace(state=state)
    reg_arc = rebuild_registry_at_boot(app)

    reg_direct = ArtifactRegistry()
    reg_direct.fold_payload(payload)

    va = reg_arc.get("ws1", "d.csv").head
    vd = reg_direct.get("ws1", "d.csv").head
    # Sabotage: if _fold_from_arc read the wrong nesting (payload not under content
    # ['payload']) the ARC record would be missing -> va is None -> red.
    assert (va.version, va.sha256, va.kind, va.artifact_id) == (
        vd.version,
        vd.sha256,
        vd.kind,
        vd.artifact_id,
    )


def test_capture_released_registry_refolds_once_on_first_access(tmp_path, monkeypatch):
    """A capture_released boot (no reachable fold source — observed live as an
    early-boot arc_iter_failed) self-heals: the FIRST off-loop reader triggers
    one refold now that the sources are up; a second failure never loops."""

    from types import SimpleNamespace

    from clio_agent.gact.artifacts import registry as registry_module
    from clio_agent.gact.artifacts.registry import ArtifactRegistry, get_registry

    empty = ArtifactRegistry()
    empty.capture_released = {"reason": "capture_released", "detail": "test"}
    app = SimpleNamespace(state=SimpleNamespace(artifact_registry=empty))

    rebuilt = ArtifactRegistry()
    calls: list[int] = []

    def fake_rebuild(target):
        calls.append(1)
        target.state.artifact_registry = rebuilt
        return rebuilt

    monkeypatch.setattr(
        "clio_agent.gact.artifacts.registry_boot.rebuild_registry_at_boot", fake_rebuild
    )
    assert get_registry(app) is rebuilt
    assert calls == [1]
    # Subsequent access returns the rebuilt registry without another fold.
    assert get_registry(app) is rebuilt
    assert calls == [1]
    del registry_module  # imported for parity with module-local patching


def test_capture_released_refold_is_single_shot_on_failure(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from clio_agent.gact.artifacts.registry import ArtifactRegistry, get_registry

    empty = ArtifactRegistry()
    empty.capture_released = {"reason": "capture_released", "detail": "test"}
    app = SimpleNamespace(state=SimpleNamespace(artifact_registry=empty))
    calls: list[int] = []

    def failing_rebuild(_target):
        calls.append(1)
        raise RuntimeError("still unreadable")

    monkeypatch.setattr(
        "clio_agent.gact.artifacts.registry_boot.rebuild_registry_at_boot", failing_rebuild
    )
    assert get_registry(app) is empty
    assert get_registry(app) is empty
    assert calls == [1]
