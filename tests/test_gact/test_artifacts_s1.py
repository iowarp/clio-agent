"""Unit tests for the S1 artifacts floor (#966/#967).

Covers the record model, the registry fold semantics (idempotency + conflict),
the three-seam minting funnel (observer tool-declared + fs_write harness + pack
declared), identity hashing incl. the oversize stat-pin, the SessionStore badge
index round-trip, and the boot fold (JSONL fallback + typed capture_released).

Each key lock carries a sabotage note: the referenced neutralization turns the
named assertion red, proving the test binds the invariant (not a tautology).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

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
    EvidenceClass,
    IdentityEvidence,
    Mechanism,
    new_artifact_id,
)
from clio_agent.gact.artifacts.registry import (
    ArtifactRegistry,
    build_session_index,
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


def _make_app(tmp_path: Path, *, with_sink: bool = True):
    """Build a fake app with a real SessionStore + capturing arc.

    With ``with_sink=False`` the semantic sink is absent, so ``_emit_semantic_event``
    short-circuits (mint still folds the payload it builds itself).
    """
    store = SessionStore(path=tmp_path / "sessions.json")
    sess = store.create(workspace_id="ws1", title="t")
    arc = _CapturingArc()
    state = SimpleNamespace(
        sessions=store,
        arc=arc,
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
    v1 = ArtifactVersion(version=1, evidence=IdentityEvidence.stat_pinned(size_bytes=1), kind=ArtifactKind.IMAGE)
    rec.add_version(v1)
    assert rec.next_version_number() == 2
    assert rec.aliases["latest"] == 1
    assert rec.head is v1


# --------------------------------------------------------------------------- #
# Registry fold semantics (replay from synthetic events)
# --------------------------------------------------------------------------- #


def _payload(*, event_id: str, name: str, version: int, sha: str | None, ws: str = "ws1",
             kind: str = "image", mechanism: str = "tool-schema") -> dict[str, Any]:
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
            app, sess.id, name="p", workspace_id="ws1", evidence=ev,
            kind=ArtifactKind.PLAN, mechanism=Mechanism.MODEL,
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
        app, sess.id,
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
        app, sess.id, tool_name="t",
        effective_args={"output_path": str(png)}, call_id="c1", workspace_id="ws1",
    )
    index = rehydrate_session_index(app, sess.id)
    assert index["count"] == 1
    assert index["names"]["plot.png"]["kind"] == "image"
    assert index["names"]["plot.png"]["v"] == 1
    assert index["names_truncated"] is False


def test_session_index_is_bounded():
    reg = ArtifactRegistry()
    for i in range(70):
        reg.fold_payload(_payload(event_id=f"e{i}", name=f"f{i:03d}.png", version=1, sha=f"{i:064x}"))
    index = build_session_index(reg, "ws1")
    assert index["count"] == 70
    assert len(index["names"]) == 64  # _SESSION_INDEX_NAME_CAP
    assert index["names_truncated"] is True


# --------------------------------------------------------------------------- #
# Boot fold (JSONL fallback + capture_released)
# --------------------------------------------------------------------------- #


def test_boot_fold_from_jsonl_trace(tmp_path):
    trace_dir = tmp_path / "semantic_traces"
    trace_dir.mkdir()
    line = {
        "event_type": "artifact.created",
        "event_id": "e1",
        "payload": _payload(event_id="", name="d.csv", version=1, sha="a" * 64, kind="dataset"),
    }
    (trace_dir / "sess_x.semantic.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
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


def test_boot_fold_capture_released_when_no_source(tmp_path):
    state = SimpleNamespace(arc=None, semantic_trace_backend=None, artifact_registry=None)
    app = SimpleNamespace(state=state)
    reg = rebuild_registry_at_boot(app)
    # Sabotage: silently return an empty registry without the capture_released tag
    # -> this typed-degrade assertion red (no-silent-fallback lock).
    assert reg.capture_released is not None
    assert reg.capture_released["reason"] == "capture_released"
