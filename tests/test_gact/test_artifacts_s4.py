"""Unit + route tests for S4 artifacts (#966/#970) — version chains, dedup, aliases.

Covers the ONE version-decision point (:mod:`clio_agent.gact.artifacts.versions`):
W&B dedup, v(n+1) revision edges (``wasRevisionOf``), the created/version.added
event split, the kind-lock warning, custody-gap re-link by hash, the
undesignated-overwrite gap-vs-auto-mint decision, alias movement + fold determinism
(order-shuffled replay → identical chain + aliases), the live ``?ref`` resolution +
alias-move route (the S2 409 placeholder dies), and the structural single-decision
lock (no version-number assignment outside ``versions.py``).

Each key lock carries a sabotage note: the referenced neutralization turns the named
assertion red, proving the test binds the invariant (not a tautology).
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.minting import (
    mint_artifact,
    mint_tool_declared_outputs,
)
from clio_agent.gact.artifacts.records import (
    ArtifactKind,
    ArtifactRecord,
    ArtifactVersion,
    IdentityEvidence,
    Mechanism,
    new_artifact_id,
)
from clio_agent.gact.artifacts.registry import ArtifactRegistry, get_registry
from clio_agent.gact.artifacts.versions import (
    VersionAction,
    decide_version,
    reconcile_designated_path,
)
from clio_agent.gact.semantic_events import event_reaches_ui
from clio_agent.gact.sessions import SessionStore

# --------------------------------------------------------------------------- #
# Fakes (parity with the S1/S2 harness).
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
    )
    return SimpleNamespace(state=state), sess, arc


def _events(arc: _CapturingArc, event_type: str) -> list[Any]:
    return [e for e in arc.events if getattr(e, "event_type", "") == event_type]


def _ev(sha: str | None, size: int = 10) -> IdentityEvidence:
    if sha is None:
        return IdentityEvidence.stat_pinned(size_bytes=size)
    return IdentityEvidence.hashed_at_use(sha256=sha, size_bytes=size)


def _record(*versions: ArtifactVersion) -> ArtifactRecord:
    rec = ArtifactRecord(workspace_id="ws1", name="d.csv")
    for v in versions:
        rec.add_version(v)
    return rec


def _version(
    version: int, sha: str | None, kind: ArtifactKind = ArtifactKind.DATASET
) -> ArtifactVersion:
    return ArtifactVersion(version=version, kind=kind, evidence=_ev(sha))


# --------------------------------------------------------------------------- #
# 1. The ONE decision point — pure decide_version()
# --------------------------------------------------------------------------- #


def test_decide_version_v1_on_empty_chain():
    d = decide_version(
        None,
        sha256="a" * 64,
        requested_kind=ArtifactKind.DATASET,
        requested_mechanism=Mechanism.TOOL_SCHEMA,
    )
    assert d.action is VersionAction.NEW_VERSION
    assert d.version_number == 1
    assert d.prior_version is None and d.prior_sha256 is None
    assert d.kind is ArtifactKind.DATASET


def test_decide_version_dedup_same_sha():
    rec = _record(_version(1, "a" * 64))
    d = decide_version(
        rec,
        sha256="a" * 64,
        requested_kind=ArtifactKind.DATASET,
        requested_mechanism=Mechanism.TOOL_SCHEMA,
    )
    # Sabotage: drop the `existing is not None` producing-dedup branch -> a v2 with a
    # duplicate sha is assigned -> action is NEW_VERSION -> red (W&B dedup lock).
    assert d.action is VersionAction.DEDUP
    assert d.deduped_onto is not None and d.deduped_onto.version == 1


def test_decide_version_new_content_stamps_revision_edge():
    rec = _record(_version(1, "a" * 64))
    d = decide_version(
        rec,
        sha256="b" * 64,
        requested_kind=ArtifactKind.DATASET,
        requested_mechanism=Mechanism.TOOL_SCHEMA,
    )
    # Sabotage: stop setting prior_version/prior_sha256 in decide_version -> the
    # wasRevisionOf edge assertions go red.
    assert d.action is VersionAction.NEW_VERSION
    assert d.version_number == 2
    assert d.prior_version == 1
    assert d.prior_sha256 == "a" * 64


def test_decide_version_kind_locked_at_v1_warns():
    rec = _record(_version(1, "a" * 64, kind=ArtifactKind.DATASET))
    d = decide_version(
        rec,
        sha256="b" * 64,
        requested_kind=ArtifactKind.IMAGE,
        requested_mechanism=Mechanism.MODEL,
    )
    # Sabotage: return requested_kind (image) instead of the locked kind in
    # _kind_lock -> the kind-lock + warning assertions go red.
    assert d.kind is ArtifactKind.DATASET  # locked at v1, never a new kind
    assert "locked at v1" in d.kind_warning
    assert "image" in d.kind_warning


def test_decide_version_stat_pinned_never_dedups():
    rec = _record(_version(1, None))  # stat-pinned v1 (no sha)
    d = decide_version(
        rec,
        sha256=None,
        requested_kind=ArtifactKind.DATASET,
        requested_mechanism=Mechanism.TOOL_SCHEMA,
    )
    assert d.action is VersionAction.NEW_VERSION  # unknown identity -> always mints
    assert d.version_number == 2


# --------------------------------------------------------------------------- #
# 2. Drift observation (producing=False): relink / gap / auto-mint
# --------------------------------------------------------------------------- #


def test_decide_version_drift_unchanged_head_is_noop():
    rec = _record(_version(1, "a" * 64), _version(2, "b" * 64))
    d = decide_version(
        rec,
        sha256="b" * 64,
        requested_kind=ArtifactKind.DATASET,
        requested_mechanism=Mechanism.TOOL_SCHEMA,
        producing=False,
    )
    assert d.action is VersionAction.DEDUP
    assert d.reason == "unchanged_head"


def test_decide_version_drift_relink_by_hash_records_custody_gap():
    rec = _record(_version(1, "a" * 64), _version(2, "b" * 64))
    # On-disk content reverted to v1's known bytes after a gap.
    d = decide_version(
        rec,
        sha256="a" * 64,
        requested_kind=ArtifactKind.DATASET,
        requested_mechanism=Mechanism.TOOL_SCHEMA,
        producing=False,
    )
    # Sabotage: collapse the drift-relink branch into producing-dedup -> a re-link is
    # silently healed onto v1 (created=False, no marker) -> these go red.
    assert d.action is VersionAction.RELINK
    assert d.version_number == 3  # a NEW immutable version, old ones untouched
    assert d.prior_version == 2  # revises the head
    assert d.custody_gap == {
        "reason": "relink_by_hash",
        "matched_version": 1,
        "matched_sha256": "a" * 64,
    }


def test_decide_version_drift_undesignated_overwrite_gap_when_lease_dirty():
    rec = _record(_version(1, "a" * 64))
    d = decide_version(
        rec,
        sha256="z" * 64,
        requested_kind=ArtifactKind.DATASET,
        requested_mechanism=Mechanism.TOOL_SCHEMA,
        producing=False,
        lease_clean=False,
    )
    # Sabotage: attribute a lease-dirty change to the observing mechanism -> mechanism
    # is not NONE / action is not GAP -> red (precision-over-recall lock).
    assert d.action is VersionAction.GAP
    assert d.mechanism is Mechanism.NONE  # actor unknown, not falsely attributed
    assert d.custody_gap["lease"] == "dirty"
    assert d.custody_gap["actor"] == "unknown"
    assert d.version_number == 2  # never mutates v1


def test_decide_version_drift_undesignated_overwrite_auto_mint_when_lease_clean():
    rec = _record(_version(1, "a" * 64))
    d = decide_version(
        rec,
        sha256="z" * 64,
        requested_kind=ArtifactKind.DATASET,
        requested_mechanism=Mechanism.TOOL_SCHEMA,
        producing=False,
        lease_clean=True,
    )
    assert d.action is VersionAction.NEW_VERSION  # single-writer provable -> auto v2
    assert d.mechanism is Mechanism.TOOL_SCHEMA  # attributed to the observing seam
    assert d.custody_gap["lease"] == "clean"
    assert d.prior_version == 1


# --------------------------------------------------------------------------- #
# 3. Mint funnel: created (v1) vs version.added (v2+) + alias.moved
# --------------------------------------------------------------------------- #


def test_chain_v1_created_v2_version_added_and_alias_moved(tmp_path):
    app, sess, arc = _make_app(tmp_path)
    csv = tmp_path / "d.csv"
    csv.write_text("a,b\n1,2\n", encoding="utf-8")
    args = {"output_path": str(csv)}
    mint_tool_declared_outputs(
        app, sess.id, tool_name="t", effective_args=args, call_id="c1", workspace_id="ws1"
    )
    # New bytes -> a genuine v2.
    csv.write_text("a,b\n9,9\n", encoding="utf-8")
    mint_tool_declared_outputs(
        app, sess.id, tool_name="t", effective_args=args, call_id="c2", workspace_id="ws1"
    )

    created = _events(arc, "artifact.created")
    added = _events(arc, "artifact.version.added")
    moved = _events(arc, "artifact.alias.moved")
    # Sabotage: emit artifact.created for every version (drop the is_v1 split) ->
    # created==2 / added==0 -> these go red (the created/version.added split lock).
    assert len(created) == 1  # only v1
    assert len(added) == 1  # v2
    assert len(moved) == 1  # latest moved v1 -> v2
    # version.added carries the wasRevisionOf edge (prior head's recorded sha).
    v1_sha = get_registry(app).get("ws1", "d.csv").versions[0].sha256
    assert added[0].payload["prior_version"] == 1
    assert added[0].payload["prior_sha256"] == v1_sha
    assert added[0].payload["version"] == 2
    # alias.moved: latest v1 -> v2.
    assert moved[0].payload["alias"] == "latest"
    assert moved[0].payload["from_version"] == 1
    assert moved[0].payload["to_version"] == 2


def test_dedup_same_bytes_twice_one_version_via_brain(tmp_path):
    app, sess, arc = _make_app(tmp_path)
    csv = tmp_path / "d.csv"
    csv.write_text("same\n", encoding="utf-8")
    args = {"output_path": str(csv)}
    mint_tool_declared_outputs(
        app, sess.id, tool_name="t", effective_args=args, call_id="c1", workspace_id="ws1"
    )
    mint_tool_declared_outputs(
        app, sess.id, tool_name="t", effective_args=args, call_id="c2", workspace_id="ws1"
    )
    reg = get_registry(app)
    # Sabotage: bypass decide_version dedup in registry.mint -> a phantom v2 with the
    # same sha appears -> versions != [1] and a stray version.added fires -> red.
    assert [v.version for v in reg.get("ws1", "d.csv").versions] == [1]
    assert len(_events(arc, "artifact.created")) == 1
    assert len(_events(arc, "artifact.version.added")) == 0


def test_kind_mismatch_on_mint_keeps_v1_kind_and_warns(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    # v1 minted as a dataset.
    mint_artifact(
        app,
        sess.id,
        name="thing.bin",
        workspace_id="ws1",
        evidence=_ev("a" * 64),
        kind=ArtifactKind.DATASET,
        mechanism=Mechanism.HARNESS,
        path=str(tmp_path / "thing.bin"),
    )
    # v2 designated as an image (new content) -> keeps dataset + records the warning.
    v2 = mint_artifact(
        app,
        sess.id,
        name="thing.bin",
        workspace_id="ws1",
        evidence=_ev("b" * 64),
        kind=ArtifactKind.IMAGE,
        mechanism=Mechanism.MODEL,
        path=str(tmp_path / "thing.bin"),
    )
    assert v2 is not None
    # Sabotage: apply the requested kind on v2 -> kind is image / warning empty -> red.
    assert v2.kind is ArtifactKind.DATASET
    assert "locked at v1" in v2.kind_warning


# --------------------------------------------------------------------------- #
# 4. reconcile_designated_path (item 4) — the honest drift observer
# --------------------------------------------------------------------------- #


def _seed_v1(app, sid, tmp_path, name="s.csv", body="v1-bytes\n"):
    from clio_agent.gact.artifacts.minting import compute_identity

    f = tmp_path / name
    f.write_text(body, encoding="utf-8")
    # Hash the ACTUAL on-disk bytes (Windows write_text translates \n->\r\n) so v1's
    # recorded identity matches what a later reconcile computes from the same file.
    mint_artifact(
        app,
        sid,
        name=name,
        workspace_id="ws1",
        evidence=compute_identity(f),
        kind=ArtifactKind.DATASET,
        mechanism=Mechanism.TOOL_SCHEMA,
        path=str(f),
    )
    return f


def test_reconcile_unchanged_head_is_noop(tmp_path):
    app, sess, arc = _make_app(tmp_path)
    f = _seed_v1(app, sess.id, tmp_path)
    out = reconcile_designated_path(
        app,
        sess.id,
        name="s.csv",
        workspace_id="ws1",
        path=str(f),
        mechanism=Mechanism.TOOL_SCHEMA,
    )
    assert out is not None and out.created is False and out.reason == "unchanged_head"
    reg = get_registry(app)
    assert len(reg.get("ws1", "s.csv").versions) == 1


def test_reconcile_relink_by_hash_records_custody_gap(tmp_path):
    app, sess, arc = _make_app(tmp_path)
    import hashlib

    f = _seed_v1(app, sess.id, tmp_path)
    v1_sha = get_registry(app).get("ws1", "s.csv").versions[0].sha256
    # v2 designated (new content) via a normal producing mint.
    f.write_text("v2-bytes\n", encoding="utf-8")
    mint_artifact(
        app,
        sess.id,
        name="s.csv",
        workspace_id="ws1",
        evidence=_ev(hashlib.sha256(b"v2-bytes\n").hexdigest()),
        kind=ArtifactKind.DATASET,
        mechanism=Mechanism.TOOL_SCHEMA,
        path=str(f),
    )
    # The file reverts to v1's exact bytes after a gap; a re-observation re-links.
    f.write_text("v1-bytes\n", encoding="utf-8")
    out = reconcile_designated_path(
        app,
        sess.id,
        name="s.csv",
        workspace_id="ws1",
        path=str(f),
        mechanism=Mechanism.TOOL_SCHEMA,
    )
    # Sabotage: route reconcile through producing=True -> the revert deduplicates onto
    # v1 (created=False, no marker) -> the relink + custody_gap assertions go red.
    assert out is not None and out.created is True
    v3 = out.version
    assert v3.version == 3
    assert v3.custody_gap["reason"] == "relink_by_hash"
    assert v3.custody_gap["matched_version"] == 1
    assert v3.custody_gap["matched_sha256"] == v1_sha


def test_reconcile_gap_version_when_lease_dirty(tmp_path):
    app, sess, arc = _make_app(tmp_path)
    f = _seed_v1(app, sess.id, tmp_path)
    # Undesignated overwrite: content changed with no seam minting it, lease dirty.
    f.write_text("mystery-overwrite\n", encoding="utf-8")
    out = reconcile_designated_path(
        app,
        sess.id,
        name="s.csv",
        workspace_id="ws1",
        path=str(f),
        mechanism=Mechanism.TOOL_SCHEMA,
        lease_clean=False,
    )
    assert out is not None and out.created is True
    gap = out.version
    assert gap.mechanism is Mechanism.NONE  # actor unknown, never mis-attributed
    assert gap.custody_gap["lease"] == "dirty"
    assert gap.version == 2  # v1 untouched


def test_reconcile_auto_mint_when_lease_clean(tmp_path):
    app, sess, arc = _make_app(tmp_path)
    f = _seed_v1(app, sess.id, tmp_path)
    f.write_text("clean-overwrite\n", encoding="utf-8")
    out = reconcile_designated_path(
        app,
        sess.id,
        name="s.csv",
        workspace_id="ws1",
        path=str(f),
        mechanism=Mechanism.TOOL_SCHEMA,
        lease_clean=True,
    )
    assert out is not None and out.created is True
    v2 = out.version
    assert v2.mechanism is Mechanism.TOOL_SCHEMA  # attributed to the observing seam
    assert v2.custody_gap["lease"] == "clean"
    assert v2.version == 2


def test_reconcile_unknown_name_is_skip(tmp_path):
    app, sess, _arc = _make_app(tmp_path)
    f = tmp_path / "never_registered.csv"
    f.write_text("x\n", encoding="utf-8")
    out = reconcile_designated_path(
        app,
        sess.id,
        name="never_registered.csv",
        workspace_id="ws1",
        path=str(f),
        mechanism=Mechanism.TOOL_SCHEMA,
    )
    assert out is None  # reconciliation is only for an established identity


# --------------------------------------------------------------------------- #
# 5. Fold determinism — replay in any order rebuilds the identical chain+aliases
# --------------------------------------------------------------------------- #


def _created_payload(name, version, sha, event_id, **extra) -> dict[str, Any]:
    p = {
        "event_id": event_id,
        "artifact_id": new_artifact_id(),
        "workspace_id": "ws1",
        "name": name,
        "version": version,
        "kind": "dataset",
        "custody": "workspace-referenced",
        "mechanism": "tool-schema",
        "sha256": sha,
        "size_bytes": 10,
        "evidence": {"evidence_class": "hashed-at-use"},
    }
    p.update(extra)
    return p


def test_fold_version_added_rebuilds_chain_with_edge():
    reg = ArtifactRegistry()
    reg.fold_payload(_created_payload("d.csv", 1, "a" * 64, "e1"))
    reg.fold_event_by_type(
        "artifact.version.added",
        _created_payload("d.csv", 2, "b" * 64, "e2", prior_version=1, prior_sha256="a" * 64),
    )
    rec = reg.get("ws1", "d.csv")
    assert [v.version for v in rec.versions] == [1, 2]
    assert rec.head.prior_version == 1  # the revision edge survives replay
    assert rec.aliases["latest"] == 2


def test_fold_out_of_order_version_events_rebuild_same_head():
    """v2 folded BEFORE v1 still yields head=v2, latest=2 (order-independent)."""
    reg = ArtifactRegistry()
    reg.fold_event_by_type(
        "artifact.version.added",
        _created_payload("d.csv", 2, "b" * 64, "e2", prior_version=1, prior_sha256="a" * 64),
    )
    reg.fold_payload(_created_payload("d.csv", 1, "a" * 64, "e1"))
    rec = reg.get("ws1", "d.csv")
    # Sabotage: revert add_version to a plain append (no sort) -> head is v1, latest=1
    # -> these go red (the replay-order-independence lock).
    assert [v.version for v in rec.versions] == [1, 2]
    assert rec.head.version == 2
    assert rec.aliases["latest"] == 2


def _alias_moved(name, alias, to_version, at, event_id) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "workspace_id": "ws1",
        "name": name,
        "alias": alias,
        "from_version": None,
        "to_version": to_version,
        "at": at,
    }


def test_fold_alias_moved_last_writer_wins_order_independent():
    """Same-timestamp alias moves resolve to the SAME winner regardless of order."""
    moves = [
        _alias_moved("d.csv", "release", 1, "2026-07-21T00:00:00Z", "m1"),
        _alias_moved("d.csv", "release", 2, "2026-07-21T00:00:00Z", "m2"),
        _alias_moved("d.csv", "release", 3, "2026-07-21T00:00:00Z", "m3"),
    ]

    def fold_order(order):
        reg = ArtifactRegistry()
        for i in order:
            reg.fold_event_by_type("artifact.alias.moved", moves[i])
        return reg.get("ws1", "d.csv").aliases["release"]

    forward = fold_order([0, 1, 2])
    shuffled = fold_order([2, 0, 1])
    reverse = fold_order([1, 2, 0])
    # Sabotage: apply alias moves unconditionally (drop the (at,event_id) last-writer
    # guard) -> the shuffled orders land different winners -> equality goes red.
    assert forward == shuffled == reverse
    # (at,event_id) total order: m3 has the max event_id among equal timestamps.
    assert forward == 3


def test_fold_full_log_shuffled_is_deterministic():
    """A whole created/version.added/alias.moved log replays identically shuffled."""
    log = [
        ("artifact.created", _created_payload("d.csv", 1, "a" * 64, "e1")),
        (
            "artifact.version.added",
            _created_payload("d.csv", 2, "b" * 64, "e2", prior_version=1, prior_sha256="a" * 64),
        ),
        (
            "artifact.version.added",
            _created_payload("d.csv", 3, "c" * 64, "e3", prior_version=2, prior_sha256="b" * 64),
        ),
        ("artifact.alias.moved", _alias_moved("d.csv", "release", 2, "2026-07-21T01:00:00Z", "m1")),
        ("artifact.alias.moved", _alias_moved("d.csv", "release", 3, "2026-07-21T02:00:00Z", "m2")),
    ]

    def fold(order):
        reg = ArtifactRegistry()
        for i in order:
            et, p = log[i]
            reg.fold_event_by_type(et, p)
        rec = reg.get("ws1", "d.csv")
        return ([v.version for v in rec.versions], dict(rec.aliases))

    ordered = fold([0, 1, 2, 3, 4])
    shuffled = fold([4, 2, 0, 3, 1])
    assert ordered == shuffled
    versions, aliases = ordered
    assert versions == [1, 2, 3]
    assert aliases["latest"] == 3
    assert aliases["release"] == 3  # m2 (later timestamp) is the last writer


def test_fold_stale_alias_move_is_typed_noop():
    reg = ArtifactRegistry()
    reg.fold_event_by_type(
        "artifact.alias.moved", _alias_moved("d.csv", "rel", 3, "2026-07-21T02:00:00Z", "m2")
    )
    stale = reg.fold_alias_moved(_alias_moved("d.csv", "rel", 1, "2026-07-21T01:00:00Z", "m1"))
    assert stale.applied is False
    assert stale.reason == "stale_alias_move"
    assert reg.get("ws1", "d.csv").aliases["rel"] == 3  # winner unchanged


# --------------------------------------------------------------------------- #
# 6. Structural lock — the single version-decision point
# --------------------------------------------------------------------------- #


def test_single_version_decision_point_no_number_assignment_elsewhere():
    """Grep-lock: next_version_number() is CALLED only in versions.py (#970)."""
    src = Path(__file__).resolve().parents[2] / "src" / "clio_agent"
    call_sites: dict[str, int] = {}
    for py in src.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        n = len(re.findall(r"\.next_version_number\s*\(", text))
        if n:
            call_sites[py.name] = n
    # Sabotage: assign a version number in registry.mint or minting (a second
    # decision point) -> another file appears here -> the single-point lock goes red.
    assert set(call_sites) == {"versions.py"}, call_sites
    # And the arithmetic helper is DEFINED once, in the record model.
    defs = [p.name for p in src.rglob("*.py") if "def next_version_number" in p.read_text("utf-8")]
    assert defs == ["records.py"]


# --------------------------------------------------------------------------- #
# 7. SSE allow-list + wire shapes
# --------------------------------------------------------------------------- #


def test_version_added_and_alias_moved_reach_ui():
    # Sabotage: remove artifact.version.added / artifact.alias.moved from
    # SSE_UI_EVENT_TYPES -> these go red (the S2 allow-list gets its S4 emit sites).
    assert event_reaches_ui("artifact.version.added") is True
    assert event_reaches_ui("artifact.alias.moved") is True


# --------------------------------------------------------------------------- #
# 8. Routes — live ?ref resolution + the alias-move route
# --------------------------------------------------------------------------- #


def _client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


def _workspace_session(c: TestClient, root: Path) -> tuple[str, str]:
    wid = c.post("/v1/workspaces", json={"name": "w", "root_path": str(root)}).json()["id"]
    sid = c.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]
    return wid, sid


def _pin_two_versions(c: TestClient, tmp_path: Path, wid: str, sid: str) -> None:
    f = tmp_path / "d.csv"
    f.write_text("v1\n", encoding="utf-8")
    c.post(f"/v1/sessions/{sid}/artifacts/pin", json={"path": "d.csv"})
    f.write_text("v2\n", encoding="utf-8")
    c.post(f"/v1/sessions/{sid}/artifacts/pin", json={"path": "d.csv"})


def test_alias_move_route_and_ref_resolution_live(tmp_path):
    c = _client(tmp_path)
    wid, sid = _workspace_session(c, tmp_path)
    _pin_two_versions(c, tmp_path, wid, sid)
    # latest resolves to v2, v1/v2 resolve, an unknown alias 404s with the honest set.
    latest = c.get(f"/v1/workspaces/{wid}/artifacts/d.csv", params={"ref": "latest"}).json()
    assert latest["resolved"]["version"] == 2
    unknown = c.get(f"/v1/workspaces/{wid}/artifacts/d.csv", params={"ref": "release"})
    assert unknown.status_code == 404
    assert unknown.json()["error"]["details"]["available"] == ["latest", "v1", "v2"]
    # Move a custom alias 'release' -> v1, then ?ref=release resolves it (live in S4).
    mv = c.post(
        f"/v1/workspaces/{wid}/artifacts/d.csv/aliases", json={"alias": "release", "ref": "v1"}
    )
    assert mv.status_code == 200, mv.text
    assert mv.json()["to_version"] == 1
    # Sabotage: leave _resolve_ref without the alias branch -> ?ref=release 404s -> red.
    got = c.get(f"/v1/workspaces/{wid}/artifacts/d.csv", params={"ref": "release"}).json()
    assert got["resolved"]["version"] == 1
    # 'release' now appears in the honest available set.
    unknown2 = c.get(f"/v1/workspaces/{wid}/artifacts/d.csv", params={"ref": "nope"})
    assert unknown2.json()["error"]["details"]["available"] == ["latest", "release", "v1", "v2"]


def test_move_latest_alias_is_refused_422(tmp_path):
    c = _client(tmp_path)
    wid, sid = _workspace_session(c, tmp_path)
    _pin_two_versions(c, tmp_path, wid, sid)
    r = c.post(
        f"/v1/workspaces/{wid}/artifacts/d.csv/aliases", json={"alias": "latest", "ref": "v1"}
    )
    # Sabotage: allow moving 'latest' by hand -> the reserved-alias guard is gone -> red.
    assert r.status_code == 422
    assert r.json()["error"]["error"] == "reserved_alias"


def test_version_wire_carries_revision_edge(tmp_path):
    c = _client(tmp_path)
    wid, sid = _workspace_session(c, tmp_path)
    _pin_two_versions(c, tmp_path, wid, sid)
    rec = c.get(f"/v1/workspaces/{wid}/artifacts/d.csv", params={"ref": "v2"}).json()
    resolved = rec["resolved"]
    # Sabotage: drop the S4 edge fields from _version_wire -> these KeyErrors go red.
    assert resolved["prior_version"] == 1
    assert resolved["prior_sha256"] is not None
    assert "kind_warning" in resolved
    assert "custody_gap" in resolved
