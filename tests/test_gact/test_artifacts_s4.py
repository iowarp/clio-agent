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

import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.minting import (
    compute_identity,
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
from clio_agent.gact.artifacts.registry import (
    ArtifactRegistry,
    InvalidAliasError,
    get_registry,
)
from clio_agent.gact.artifacts.versions import (
    VersionAction,
    decide_version,
    reconcile_designated_path,
    workspace_lease_clean,
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


@pytest.fixture(autouse=True)
def _reset_observer_call_stamp():
    """Isolate the observer thread-local across tests (it persists per worker thread).

    The lease/drift tests read (and the lease tests set) ``_OBSERVER_CALL_T0``; a stamp
    leaked from one test would make another's ``workspace_lease_clean`` non-deterministic.
    """
    from clio_agent.gact import tool_observer

    tool_observer._OBSERVER_CALL_T0.value = None
    yield
    tool_observer._OBSERVER_CALL_T0.value = None


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


def test_no_inline_version_number_arithmetic_outside_the_helper():
    """Finding [9]: the grep-lock also forbids INLINE version-number arithmetic.

    ``next_version_number`` being the only *named* call site does not stop a second
    decision point from computing ``head.version + 1`` / ``max(v.version…) + 1`` /
    ``len(versions) + 1`` inline. Scan the concrete inline forms across ``src`` and
    allow-list only the ONE legitimate producer — ``records.py``'s
    ``next_version_number`` (``self.head.version + 1``). Any other file that grows a
    version number by arithmetic turns this red.
    """
    src = Path(__file__).resolve().parents[2] / "src" / "clio_agent"
    # Inline "grow a version number by one" forms — the natural sabotage twins the
    # named-helper lock misses (the finding's own failure scenario).
    patterns = [
        re.compile(r"\.version\s*\+\s*1"),  # head.version + 1
        re.compile(r"max\([^)]*\.version[^)]*\)\s*\+\s*1"),  # max(v.version …) + 1
        re.compile(r"len\([^)]*versions[^)]*\)\s*\+\s*1"),  # len(versions) + 1
    ]
    offenders: dict[str, list[str]] = {}
    for py in src.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        hits = [m.group(0) for pat in patterns for m in pat.finditer(text)]
        if hits:
            offenders[py.name] = hits
    # Allow-list: the sole arithmetic producer is records.next_version_number.
    assert set(offenders) == {"records.py"}, offenders
    # And that single legitimate site is exactly the helper's ``head.version + 1``.
    assert offenders["records.py"] == [".version + 1"], offenders["records.py"]


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


# --------------------------------------------------------------------------- #
# 9. Finding [1]: relink re-observation is idempotent (chain grows by ONE)
# --------------------------------------------------------------------------- #


def test_relink_reobservation_is_idempotent_grows_by_exactly_one(tmp_path):
    """Revert -> relink -> re-reconcile x3 grows the chain by exactly ONE version.

    Finding [1]: the drift no-op is gated on ``head.sha256 == sha256`` (not on the
    first-match version number). After a relink the head shares v1's hash; without the
    fix each later reconcile of the stable file re-mints a spurious relink unbounded.
    """
    app, sess, _arc = _make_app(tmp_path)
    f = _seed_v1(app, sess.id, tmp_path, name="s.csv", body="v1-bytes\n")
    # A genuine v2 (new content).
    f.write_text("v2-bytes\n", encoding="utf-8")
    mint_artifact(
        app,
        sess.id,
        name="s.csv",
        workspace_id="ws1",
        evidence=compute_identity(f),
        kind=ArtifactKind.DATASET,
        mechanism=Mechanism.TOOL_SCHEMA,
        path=str(f),
    )
    # Revert to v1's bytes: the FIRST reconcile relinks to v3 (head now shares v1's sha).
    f.write_text("v1-bytes\n", encoding="utf-8")

    def _reconcile():
        return reconcile_designated_path(
            app,
            sess.id,
            name="s.csv",
            workspace_id="ws1",
            path=str(f),
            mechanism=Mechanism.TOOL_SCHEMA,
        )

    first = _reconcile()
    assert first is not None and first.created is True and first.version.version == 3
    # Re-observe the STABLE reverted file three more times — each a clean no-op.
    # Sabotage: gate the no-op on ``existing.version == head.version`` -> v1 binds and
    # every reconcile mints a phantom v4, v5, v6 -> the length assertion goes red.
    for _ in range(3):
        out = _reconcile()
        assert out is not None and out.created is False and out.reason == "unchanged_head"
    reg = get_registry(app)
    assert [v.version for v in reg.get("ws1", "s.csv").versions] == [1, 2, 3]


# --------------------------------------------------------------------------- #
# 10. Finding [2/6]: the observer seam WIRES reconcile — no false tool mint
# --------------------------------------------------------------------------- #


def test_seam_a_external_overwrite_reobservation_is_gap_not_false_tool_mint(tmp_path):
    """External overwrite re-observed by a tool -> GAP, never a tool-schema mint.

    Finding [2/6]: a designated path the tool did NOT write this call (mtime predates
    the call, unknown content) must route through reconcile (producing=False) and
    become a GAP — mechanism ``none``, no ``call_id`` — not a false producing
    tool-schema version carrying the tool's call id (owner decision #966.10).
    """
    app, sess, _arc = _make_app(tmp_path)
    csv = tmp_path / "d.csv"
    csv.write_text("genuine-v1\n", encoding="utf-8")
    args = {"output_path": str(csv)}
    mint_tool_declared_outputs(
        app, sess.id, tool_name="t", effective_args=args, call_id="c1", workspace_id="ws1"
    )
    # An external process overwrites with UNKNOWN content BEFORE the next call starts.
    csv.write_text("external-mystery\n", encoding="utf-8")
    past = time.time() - 100
    os.utime(csv, (past, past))
    minted = mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="reader",
        effective_args=args,
        call_id="c2",
        workspace_id="ws1",
        call_started_at=time.time() + 100,
    )
    reg = get_registry(app)
    versions = reg.get("ws1", "d.csv").versions
    assert [v.version for v in versions] == [1, 2]
    gap = versions[-1]
    # Sabotage: mint the drifted path producing=True with the tool call -> mechanism is
    # tool-schema and producer carries c2 -> these go red (the false-attribution lock).
    assert gap.mechanism is Mechanism.NONE
    assert gap.custody_gap["reason"] == "undesignated_overwrite"
    assert gap.custody_gap["lease"] == "dirty"
    assert "call_id" not in gap.producer
    assert all(v.producer.get("call_id") != "c2" for v in versions)
    assert minted == [gap]


def test_seam_a_revert_reobservation_relinks_not_false_tool_mint(tmp_path):
    """A tool re-observing a REVERTED file relinks by hash — never a false tool mint."""
    app, sess, _arc = _make_app(tmp_path)
    csv = tmp_path / "d.csv"
    csv.write_text("A-bytes\n", encoding="utf-8")
    args = {"output_path": str(csv)}
    mint_tool_declared_outputs(
        app, sess.id, tool_name="t", effective_args=args, call_id="c1", workspace_id="ws1"
    )
    v1_sha = get_registry(app).get("ws1", "d.csv").versions[0].sha256
    # A genuine v2 (new content, no call window -> normal mint).
    csv.write_text("B-bytes\n", encoding="utf-8")
    mint_tool_declared_outputs(
        app, sess.id, tool_name="t", effective_args=args, call_id="c2", workspace_id="ws1"
    )
    # The file reverts to v1's bytes BEFORE the next call -> a drift re-observation.
    csv.write_text("A-bytes\n", encoding="utf-8")
    past = time.time() - 100
    os.utime(csv, (past, past))
    mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="reader",
        effective_args=args,
        call_id="c3",
        workspace_id="ws1",
        call_started_at=time.time() + 100,
    )
    reg = get_registry(app)
    versions = reg.get("ws1", "d.csv").versions
    assert [v.version for v in versions] == [1, 2, 3]
    relink = versions[-1]
    assert relink.custody_gap["reason"] == "relink_by_hash"
    assert relink.custody_gap["matched_version"] == 1
    assert relink.custody_gap["matched_sha256"] == v1_sha
    assert "call_id" not in relink.producer  # never a false tool mint with c3


def test_seam_a_genuine_write_during_call_mints_normally(tmp_path):
    """A file (re)written DURING the call window keeps the ordinary producing mint."""
    app, sess, _arc = _make_app(tmp_path)
    csv = tmp_path / "d.csv"
    csv.write_text("v1\n", encoding="utf-8")
    args = {"output_path": str(csv)}
    mint_tool_declared_outputs(
        app, sess.id, tool_name="t", effective_args=args, call_id="c1", workspace_id="ws1"
    )
    # v2 genuinely written now (mtime >= a call that started in the past).
    csv.write_text("v2\n", encoding="utf-8")
    minted = mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="t",
        effective_args=args,
        call_id="c2",
        workspace_id="ws1",
        call_started_at=time.time() - 100,
    )
    reg = get_registry(app)
    versions = reg.get("ws1", "d.csv").versions
    assert [v.version for v in versions] == [1, 2]
    v2 = versions[-1]
    assert v2.mechanism is Mechanism.TOOL_SCHEMA
    assert v2.producer["call_id"] == "c2"  # genuine attribution preserved
    assert v2.custody_gap is None
    assert minted == [v2]


# --------------------------------------------------------------------------- #
# 11. Finding [3/4]+[8]: the REAL lease predicate (agent-task registry seams)
# --------------------------------------------------------------------------- #


def _lease_app(tmp_path):
    from clio_agent.gact.agent_tasks import AgentTaskRegistry

    app, sess, arc = _make_app(tmp_path)
    app.state.in_flight_turns = {}
    app.state.agent_task_registry = AgentTaskRegistry()
    return app, sess, arc


@contextmanager
def _active_observer_call():
    """Stamp the observer thread-local as if a tool call is running on this thread."""
    from clio_agent.gact import tool_observer

    tool_observer._OBSERVER_CALL_T0.value = time.time()
    try:
        yield
    finally:
        tool_observer._OBSERVER_CALL_T0.value = None


def test_lease_clean_single_quiet_session(tmp_path):
    app, sess, _arc = _lease_app(tmp_path)
    app.state.in_flight_turns = {sess.id: object()}  # only the current writer
    with _active_observer_call():
        assert workspace_lease_clean(app, "ws1", session_id=sess.id) is True


def test_lease_dirty_two_active_tasks_one_workspace(tmp_path):
    from clio_agent.gact.agent_tasks import STATUS_RUNNING, AgentTask

    app, sess, _arc = _lease_app(tmp_path)
    # A genuine concurrent child task RUNNING in the SAME workspace (via the real
    # agent-task registry seam, not an injected boolean) — a second writer.
    child = app.state.sessions.create(workspace_id="ws1", title="child")
    app.state.agent_task_registry.register(
        AgentTask(
            task_id="t1",
            parent_session_id=sess.id,
            child_session_id=child.id,
            status=STATUS_RUNNING,
        )
    )
    app.state.in_flight_turns = {sess.id: object()}
    with _active_observer_call():
        # Sabotage: return `_observer_call_started_at() is not None` (the old latch) ->
        # the concurrent child is ignored and the lease reads CLEAN -> this goes red.
        assert workspace_lease_clean(app, "ws1", session_id=sess.id) is False


def test_lease_clean_ignores_terminal_task_and_other_workspace(tmp_path):
    from clio_agent.gact.agent_tasks import STATUS_COMPLETED, STATUS_RUNNING, AgentTask

    app, sess, _arc = _lease_app(tmp_path)
    done_child = app.state.sessions.create(workspace_id="ws1", title="done")
    app.state.agent_task_registry.register(
        AgentTask(
            task_id="t1",
            parent_session_id=sess.id,
            child_session_id=done_child.id,
            status=STATUS_COMPLETED,  # terminal -> not a live writer
        )
    )
    other_child = app.state.sessions.create(workspace_id="ws2", title="other")
    app.state.agent_task_registry.register(
        AgentTask(
            task_id="t2",
            parent_session_id=sess.id,
            child_session_id=other_child.id,
            status=STATUS_RUNNING,  # active but a DIFFERENT workspace
        )
    )
    app.state.in_flight_turns = {sess.id: object(), other_child.id: object()}
    with _active_observer_call():
        assert workspace_lease_clean(app, "ws1", session_id=sess.id) is True


def test_lease_dirty_concurrent_session_same_workspace(tmp_path):
    app, sess, _arc = _lease_app(tmp_path)
    peer = app.state.sessions.create(workspace_id="ws1", title="peer")
    app.state.in_flight_turns = {sess.id: object(), peer.id: object()}
    with _active_observer_call():
        assert workspace_lease_clean(app, "ws1", session_id=sess.id) is False


def test_lease_dirty_outside_active_call_is_not_latched(tmp_path):
    """Finding [3] latch regression: no active call on this thread -> DIRTY.

    The stamp is cleared at ``completed`` (never latched), so a warm worker thread with
    no active call proves nothing and the lease is DIRTY even with no other writers.
    """
    from clio_agent.gact import tool_observer

    app, sess, _arc = _lease_app(tmp_path)
    app.state.in_flight_turns = {sess.id: object()}
    tool_observer._OBSERVER_CALL_T0.value = None  # the call completed -> stamp cleared
    assert workspace_lease_clean(app, "ws1", session_id=sess.id) is False


def test_observer_completed_clears_the_call_stamp(tmp_path):
    """Finding [3]: the observer's ``completed`` phase resets ``_OBSERVER_CALL_T0``.

    Drives the real observer built by ``_make_tool_observer`` so the reset is bound to
    the live seam, not just the predicate. Sabotage: drop the reset line -> the stamp
    stays set after completion (the latch) -> the final assertion goes red.
    """
    from clio_agent.gact.tool_observer import _OBSERVER_CALL_T0, _make_tool_observer

    c = _client(tmp_path)
    wid, sid = _workspace_session(c, tmp_path)
    app = c.app  # the FastAPI app behind the TestClient
    observe = _make_tool_observer(app)
    _OBSERVER_CALL_T0.value = None
    observe("mytool", {}, "started", None)
    assert _OBSERVER_CALL_T0.value is not None  # stamped at started
    observe("mytool", {}, "completed", None, {"content": [{"type": "text", "text": "ok"}]})
    assert _OBSERVER_CALL_T0.value is None  # cleared at completed (no latch)


# --------------------------------------------------------------------------- #
# 12. Finding [5]: the live alias move applies through the fold's comparator
# --------------------------------------------------------------------------- #


def _two_version_registry() -> ArtifactRegistry:
    reg = ArtifactRegistry()
    reg.fold_payload(_created_payload("d.csv", 1, "a" * 64, "e1"))
    reg.fold_event_by_type(
        "artifact.version.added",
        _created_payload("d.csv", 2, "b" * 64, "e2", prior_version=1, prior_sha256="a" * 64),
    )
    return reg


def test_live_alias_move_stale_refused_identically_to_fold():
    """A live stale move (older (at,event_id)) is refused exactly as the fold refuses it."""
    reg = _two_version_registry()
    # A live move at a NEWER (at, event_id) wins and applies.
    won = reg.move_alias(
        "ws1", "d.csv", alias="rel", to_version=2, at="2026-07-21T02:00:00Z", event_id="m2"
    )
    assert won == (None, 2, True)
    assert reg.get("ws1", "d.csv").aliases["rel"] == 2
    # A live move with an OLDER (at, event_id) is a no-op (applied=False), state kept.
    stale_live = reg.move_alias(
        "ws1", "d.csv", alias="rel", to_version=1, at="2026-07-21T01:00:00Z", event_id="m1"
    )
    assert stale_live is not None and stale_live[2] is False
    assert reg.get("ws1", "d.csv").aliases["rel"] == 2  # winner unchanged
    # The FOLD makes the IDENTICAL decision on the very same older move.
    reg2 = _two_version_registry()
    reg2.fold_event_by_type(
        "artifact.alias.moved", _alias_moved("d.csv", "rel", 2, "2026-07-21T02:00:00Z", "m2")
    )
    folded_stale = reg2.fold_alias_moved(
        _alias_moved("d.csv", "rel", 1, "2026-07-21T01:00:00Z", "m1")
    )
    assert folded_stale.applied is False and folded_stale.reason == "stale_alias_move"
    assert reg2.get("ws1", "d.csv").aliases["rel"] == 2


# --------------------------------------------------------------------------- #
# 13. Finding [7]: vN-grammar aliases refused at the route AND the record layer
# --------------------------------------------------------------------------- #


def test_vn_grammar_alias_rejected_at_route(tmp_path):
    c = _client(tmp_path)
    wid, sid = _workspace_session(c, tmp_path)
    _pin_two_versions(c, tmp_path, wid, sid)
    # A version-shaped alias would be shadowed by _resolve_ref's vN branch -> refused.
    r = c.post(f"/v1/workspaces/{wid}/artifacts/d.csv/aliases", json={"alias": "v2", "ref": "v1"})
    # Sabotage: reject only 'latest' at the route -> 'v2' is accepted 200 -> red.
    assert r.status_code == 422
    assert r.json()["error"]["error"] == "invalid_alias"
    # A vN name past the chain end is equally refused (never advertised-but-unresolvable).
    r2 = c.post(f"/v1/workspaces/{wid}/artifacts/d.csv/aliases", json={"alias": "v99", "ref": "v1"})
    assert r2.status_code == 422
    assert r2.json()["error"]["error"] == "invalid_alias"


def test_vn_and_latest_alias_rejected_at_record_layer():
    reg = _two_version_registry()
    # Sabotage: drop the alias_rejection_reason guard in move_alias -> no raise -> red.
    with pytest.raises(InvalidAliasError) as vn:
        reg.move_alias("ws1", "d.csv", alias="v99", to_version=1, at="t", event_id="x")
    assert vn.value.reason == "invalid_alias"
    with pytest.raises(InvalidAliasError) as latest:
        reg.move_alias("ws1", "d.csv", alias="latest", to_version=1, at="t", event_id="y")
    assert latest.value.reason == "reserved_alias"
    # A normal alias still moves.
    ok = reg.move_alias("ws1", "d.csv", alias="release", to_version=1, at="t", event_id="z")
    assert ok == (None, 1, True)
