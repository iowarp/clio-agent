"""Tests for the S6 artifacts CAS slice (#966/#972).

Covers the content-addressed store and its budget discipline: ingest-at-mint (the
teed identity hash), the size threshold (over → referenced + typed
``not_ingested_size``), natural dedup + refcount, idempotent double-ingest, the
``/bytes`` CAS-first resolution ladder (served from CAS; still served after the
workspace file is deleted; a flipped blob byte → 409 ``integrity_violation``), the
reachability GC matrix (alias-pinned survives, unreachable evicted with a
trace-only event, used-by-retained REFUSED — each typed), the config knobs, and the
structural one-ladder guard (no second byte-serving path).

Each key lock carries a sabotage note: the referenced neutralization turns the
named assertion red, proving the test binds the invariant (not a tautology).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.cas import (
    CASStore,
    cas_budget_bytes,
    cas_max_file_bytes,
    cas_root_for,
    hash_stat_cache,
    ingest_identity,
)
from clio_agent.gact.artifacts.cas_gc import (
    CAS_EVICTED_EVENT,
    USED_BY_RETAINED_REASON,
    alias_reachable_shas,
    enforce_cas_budget,
    run_boot_cas_gc,
)
from clio_agent.gact.artifacts.minting import mint_artifact, mint_tool_declared_outputs
from clio_agent.gact.artifacts.records import (
    ArtifactKind,
    Custody,
    IdentityEvidence,
    Mechanism,
)
from clio_agent.gact.artifacts.registry import ArtifactRegistry
from clio_agent.gact.artifacts.versions import version_record_payload
from clio_agent.gact.semantic_events import (
    SSE_TRACE_ONLY_EVENT_TYPES,
    event_reaches_ui,
)
from clio_agent.gact.sessions import SessionStore

# --------------------------------------------------------------------------- #
# Harnesses
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


def _mint_app(tmp_path: Path):
    """A lightweight app with a real registry + session store + capturing ARC."""
    store = SessionStore(path=tmp_path / "sessions.json")
    arc = _CapturingArc()
    state = SimpleNamespace(
        sessions=store,
        arc=arc,
        workspaces=_FakeWorkspaces({"ws1": str(tmp_path)}),
        semantic_event_sink=object(),
        semantic_trace_detail_level="semantic",
        semantic_trace_backend=None,
        artifact_registry=ArtifactRegistry(),
    )
    return SimpleNamespace(state=state), store, arc


def _client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


def _workspace_session(c: TestClient, root: Path) -> tuple[str, str]:
    wid = c.post("/v1/workspaces", json={"name": "w", "root_path": str(root)}).json()["id"]
    sid = c.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]
    return wid, sid


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# --------------------------------------------------------------------------- #
# 1. Config knobs (file -> env -> default)
# --------------------------------------------------------------------------- #


def test_knob_defaults():
    # Sabotage: change the in-code defaults -> these go red (the documented default lock;
    # they also mirror the budget-gate + env reference).
    assert cas_max_file_bytes() == 16 * 1024 * 1024
    assert cas_budget_bytes() == 512 * 1024 * 1024
    assert hash_stat_cache() is False  # distrust unreliable mtimes by default


def test_knob_resolution_from_config_layer():
    from tests import _config_layer

    _config_layer.set_config("artifacts.cas_max_file_bytes", 2048)
    _config_layer.set_config("artifacts.cas_budget_bytes", 4096)
    _config_layer.set_config("artifacts.hash_stat_cache", True)
    try:
        # Sabotage: hardcode the knob instead of conf.resolve -> the config layer no
        # longer wins -> these go red (the config-first lock).
        assert cas_max_file_bytes() == 2048
        assert cas_budget_bytes() == 4096
        assert hash_stat_cache() is True
    finally:
        _config_layer.set_config("artifacts.cas_max_file_bytes", 16 * 1024 * 1024)
        _config_layer.set_config("artifacts.cas_budget_bytes", 512 * 1024 * 1024)
        _config_layer.set_config("artifacts.hash_stat_cache", False)


# --------------------------------------------------------------------------- #
# 2. Ingest / threshold / dedup / refcount / idempotency
# --------------------------------------------------------------------------- #


def test_ingest_small_file_writes_addressed_blob(tmp_path: Path):
    f = tmp_path / "plot.png"
    content = b"\x89PNG small"
    f.write_bytes(content)
    out = ingest_identity(f, workspace_root=tmp_path)
    # Sabotage: return WORKSPACE_REFERENCED for a small file -> custody goes red.
    assert out.custody == Custody.CAS
    assert out.reason == "ingested"
    assert out.evidence.sha256 == _sha(content)
    blob = cas_root_for(tmp_path) / _sha(content)[:2] / _sha(content)
    assert blob.is_file()
    assert blob.read_bytes() == content


def test_ingest_over_threshold_stays_referenced_with_typed_size(tmp_path: Path):
    f = tmp_path / "big.csv"
    content = b"x" * 5000
    f.write_bytes(content)
    out = ingest_identity(f, workspace_root=tmp_path, cas_max_bytes=1000)
    # Sabotage: ingest over-threshold anyway -> custody flips to CAS -> these go red
    # (the size-threshold lock: big datasets stay referenced with a typed reason).
    assert out.custody == Custody.WORKSPACE_REFERENCED
    assert out.reason == "not_ingested_size"
    assert out.not_ingested_size == 5000
    assert out.evidence.sha256 == _sha(content)  # still hashed (identity known)
    assert not (cas_root_for(tmp_path) / _sha(content)[:2]).exists()


def test_ingest_over_hash_ceiling_is_stat_pinned(tmp_path: Path):
    f = tmp_path / "huge.bin"
    f.write_bytes(b"y" * 4000)
    out = ingest_identity(f, workspace_root=tmp_path, hash_max_bytes=1000)
    # Over the hash ceiling: never read whole -> stat-pinned, referenced, typed.
    assert out.custody == Custody.WORKSPACE_REFERENCED
    assert out.reason == "over_hash_threshold"
    assert out.evidence.sha256 is None
    assert out.not_ingested_size == 4000


def test_dedup_same_content_two_names_one_blob_refcount_two(tmp_path: Path):
    content = b"identical bytes"
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_bytes(content)
    b.write_bytes(content)
    out_a = ingest_identity(a, workspace_root=tmp_path)
    out_b = ingest_identity(b, workspace_root=tmp_path)
    sha = _sha(content)
    assert out_a.evidence.sha256 == sha == out_b.evidence.sha256
    # Sabotage: key the blob by name instead of hash -> two blobs -> this goes red
    # (the content-address lock: same content is ONE blob).
    shard = cas_root_for(tmp_path) / sha[:2]
    assert [p.name for p in shard.iterdir()] == [sha]
    # Refcount is a registry query: two versions reference the one blob.
    reg = ArtifactRegistry()
    for name in ("a.txt", "b.txt"):
        reg.mint(
            workspace_id="ws1",
            name=name,
            event_id=f"e-{name}",
            kind=ArtifactKind.OTHER,
            custody=Custody.CAS,
            mechanism=Mechanism.HARNESS,
            evidence=out_a.evidence,
            producer={},
            path=str(tmp_path / name),
            created_at="",
            annotation="",
        )
    refcount = sum(
        1 for rec in reg.list_for_workspace("ws1") for v in rec.versions if v.sha256 == sha
    )
    assert refcount == 2


def test_idempotent_double_ingest_is_dedup_existing(tmp_path: Path):
    f = tmp_path / "once.txt"
    f.write_bytes(b"write once")
    first = ingest_identity(f, workspace_root=tmp_path)
    second = ingest_identity(f, workspace_root=tmp_path)
    # Sabotage: os.replace unconditionally (no existing-blob check) still yields one
    # file, but the typed reason distinguishes the second as a dedup no-op.
    assert first.reason == "ingested"
    assert second.reason == "dedup_existing"
    assert first.evidence.sha256 == second.evidence.sha256


def test_ingest_no_cas_root_is_typed_referenced(tmp_path: Path):
    f = tmp_path / "orphan.txt"
    f.write_bytes(b"no store")
    out = ingest_identity(f, workspace_root=None)
    # No resolvable workspace root -> referenced with a TYPED reason (no silent skip).
    assert out.custody == Custody.WORKSPACE_REFERENCED
    assert out.reason == "cas_store_unavailable"
    assert out.evidence.sha256 == _sha(b"no store")


# --------------------------------------------------------------------------- #
# 3. Mint seam: tool-declared outputs auto-ingest
# --------------------------------------------------------------------------- #


def test_tool_declared_output_auto_ingested_to_cas(tmp_path: Path):
    app, store, _arc = _mint_app(tmp_path)
    sess = store.create(workspace_id="ws1", title="t")
    png = tmp_path / "chart.png"
    content = b"\x89PNG\r\nchartbytes"
    png.write_bytes(content)
    minted = mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="plot",
        effective_args={"output_path": str(png)},
        call_id="c1",
        workspace_id="ws1",
    )
    assert len(minted) == 1
    # Sabotage: pass custody=WORKSPACE_REFERENCED at the tool seam -> custody goes red
    # (the ingest-at-mint lock: a small tool output lands in CAS).
    assert minted[0].custody == Custody.CAS
    assert minted[0].not_ingested_size is None
    blob = CASStore(tmp_path).blob_path(_sha(content))
    assert blob.is_file()


# --------------------------------------------------------------------------- #
# 4. /bytes CAS-first ladder + self-validation
# --------------------------------------------------------------------------- #


def test_bytes_served_from_cas_and_survives_workspace_deletion(tmp_path: Path):
    c = _client(tmp_path)
    wid, sid = _workspace_session(c, tmp_path)
    f = tmp_path / "fig.png"
    content = b"\x89PNG durable"
    f.write_bytes(content)
    pinned = c.post(f"/v1/sessions/{sid}/artifacts/pin", json={"path": "fig.png"}).json()["pinned"]
    assert pinned["custody"] == "cas"
    aid = pinned["artifact_id"]
    # Served from CAS.
    r = c.get(f"/v1/artifacts/{aid}/bytes")
    assert r.status_code == 200
    assert r.content == content
    # DELETE the workspace file — CAS custody means the bytes survive workspace churn.
    f.unlink()
    r2 = c.get(f"/v1/artifacts/{aid}/bytes")
    # Sabotage: resolve /bytes from version.path first (not the CAS blob) -> this goes
    # red once the workspace file is gone (the CAS-first ladder lock).
    assert r2.status_code == 200
    assert r2.content == content


def test_bytes_flipped_blob_byte_is_integrity_violation(tmp_path: Path):
    c = _client(tmp_path)
    _wid, sid = _workspace_session(c, tmp_path)
    f = tmp_path / "series.csv"
    content = b"a,b\n1,2\n"
    f.write_bytes(content)
    pinned = c.post(f"/v1/sessions/{sid}/artifacts/pin", json={"path": "series.csv"}).json()[
        "pinned"
    ]
    aid = pinned["artifact_id"]
    sha = pinned["sha256"]
    # Flip a byte IN THE CAS BLOB (the app-owned store) — corruption the read must catch.
    blob = CASStore(tmp_path).blob_path(sha)
    raw = bytearray(blob.read_bytes())
    raw[0] ^= 0xFF
    blob.write_bytes(bytes(raw))
    r = c.get(f"/v1/artifacts/{aid}/bytes")
    # Sabotage: drop the re-hash on the CAS rung -> corruption served as-is -> red
    # (the self-validation lock: detection is the universal guarantee, §7).
    assert r.status_code == 409
    body = r.json()["error"]
    assert body["error"] == "integrity_violation"
    assert body["details"]["recorded_sha256"] != body["details"]["actual_sha256"]


# --------------------------------------------------------------------------- #
# 5. Reachability GC matrix + budget enforcement
# --------------------------------------------------------------------------- #


def _mint_cas_version(app, reg, tmp_path, *, name, content, producer_sid):
    """Ingest ``content`` into CAS and mint a CAS version onto ``name``'s chain."""
    fpath = tmp_path / f"{name}.{abs(hash(content)) % 9999}"
    fpath.write_bytes(content)
    out = ingest_identity(fpath, workspace_root=tmp_path)
    version = mint_artifact(
        app,
        producer_sid,
        name=name,
        workspace_id="ws1",
        evidence=out.evidence,
        kind=ArtifactKind.DATASET,
        mechanism=Mechanism.HARNESS,
        producer={"session_id": producer_sid},
        custody=Custody.CAS,
        path=str(fpath),
    )
    return version


def test_gc_reachability_matrix(tmp_path: Path):
    app, store, arc = _mint_app(tmp_path)
    reg = app.state.artifact_registry
    retained = store.create(workspace_id="ws1", title="retained")

    # pinned.csv: v1 kept by a USER alias 'gold'; v2 is head (latest).
    _mint_cas_version(
        app, reg, tmp_path, name="pinned.csv", content=b"P1", producer_sid=retained.id
    )
    _mint_cas_version(
        app, reg, tmp_path, name="pinned.csv", content=b"P2", producer_sid=retained.id
    )
    reg.move_alias("ws1", "pinned.csv", alias="gold", to_version=1, at="2026", event_id="a-gold")
    sha_p1, sha_p2 = _sha(b"P1"), _sha(b"P2")

    # scratch.csv: v1 produced by a DELETED (non-retained) session -> unreachable+evictable.
    ghost = store.create(workspace_id="ws1", title="ghost")
    _mint_cas_version(app, reg, tmp_path, name="scratch.csv", content=b"S1", producer_sid=ghost.id)
    _mint_cas_version(
        app, reg, tmp_path, name="scratch.csv", content=b"S2", producer_sid=retained.id
    )
    store.delete(ghost.id)
    sha_s1, sha_s2 = _sha(b"S1"), _sha(b"S2")

    # used.csv: v1 produced by a RETAINED session -> unreachable-by-alias but REFUSED.
    _mint_cas_version(app, reg, tmp_path, name="used.csv", content=b"U1", producer_sid=retained.id)
    _mint_cas_version(app, reg, tmp_path, name="used.csv", content=b"U2", producer_sid=retained.id)
    sha_u1, sha_u2 = _sha(b"U1"), _sha(b"U2")

    # Reachable-by-alias set: every head (latest) + the user-pinned v1.
    reachable = alias_reachable_shas(reg.list_for_workspace("ws1"))
    assert reachable == {sha_p1, sha_p2, sha_s2, sha_u2}

    store_cas = CASStore(tmp_path)
    result = enforce_cas_budget(app, "ws1", tmp_path, sid=retained.id, budget_bytes=0)

    evicted = {e["sha256"] for e in result.evicted}
    refused = {(r["sha256"], r["reason"]) for r in result.refused}
    # Sabotage: fold used-by-retained into 'reachable' silently -> the refused set
    # empties -> the typed-refusal assertion goes red.
    assert evicted == {sha_s1}
    assert refused == {(sha_u1, USED_BY_RETAINED_REASON)}
    # Survivors on disk: alias-pinned (both), latest of each, and the REFUSED blob.
    assert store_cas.has_blob(sha_p1) and store_cas.has_blob(sha_p2)
    assert store_cas.has_blob(sha_s2)
    assert store_cas.has_blob(sha_u1) and store_cas.has_blob(sha_u2)
    # The evicted scratch v1 blob is gone.
    assert not store_cas.has_blob(sha_s1)
    # The eviction fired a trace-only artifact.cas.evicted event.
    evicted_events = [e for e in arc.events if getattr(e, "event_type", "") == CAS_EVICTED_EVENT]
    assert len(evicted_events) == 1


def test_gc_under_budget_is_noop(tmp_path: Path):
    app, store, _arc = _mint_app(tmp_path)
    reg = app.state.artifact_registry
    s = store.create(workspace_id="ws1", title="t")
    _mint_cas_version(app, reg, tmp_path, name="a.csv", content=b"one", producer_sid=s.id)
    # Budget far above the tiny store -> nothing scanned, nothing evicted.
    result = enforce_cas_budget(app, "ws1", tmp_path, budget_bytes=10 * 1024 * 1024)
    assert result.reason == "under_budget"
    assert result.evicted == []
    assert CASStore(tmp_path).has_blob(_sha(b"one"))


def test_boot_cas_gc_iterates_workspaces(tmp_path: Path):
    app, store, _arc = _mint_app(tmp_path)
    reg = app.state.artifact_registry
    store.create(workspace_id="ws1", title="t")
    # An unreachable non-head blob under a zero budget -> boot GC evicts it.
    _mint_cas_version(app, reg, tmp_path, name="d.csv", content=b"V1", producer_sid="gone")
    _mint_cas_version(app, reg, tmp_path, name="d.csv", content=b"V2", producer_sid="gone")
    results = run_boot_cas_gc(app, budget_bytes=0)
    assert len(results) == 1
    assert _sha(b"V1") in {e["sha256"] for e in results[0].evicted}


# --------------------------------------------------------------------------- #
# 6. not_ingested_size fold round-trip + trace-only + structural guard
# --------------------------------------------------------------------------- #


def test_not_ingested_size_folds_round_trip(tmp_path: Path):
    """The typed over-threshold marker survives the event -> boot-fold round trip."""
    reg = ArtifactRegistry()
    out = reg.mint(
        workspace_id="ws1",
        name="big.nc",
        event_id="e1",
        kind=ArtifactKind.DATASET,
        custody=Custody.WORKSPACE_REFERENCED,
        mechanism=Mechanism.TOOL_SCHEMA,
        evidence=IdentityEvidence.hashed_at_use(sha256="a" * 64, size_bytes=99),
        producer={},
        path="/ws/big.nc",
        created_at="",
        annotation="",
        not_ingested_size=99,
    )
    payload = version_record_payload("e1", "ws1", "big.nc", out.version, revision_edge=False)
    assert payload["not_ingested_size"] == 99
    # Fold into a fresh registry — the marker must reconstruct on the rebuilt version.
    reg2 = ArtifactRegistry()
    reg2.fold_payload(payload)
    v = reg2.get("ws1", "big.nc").versions[0]
    # Sabotage: drop not_ingested_size from the payload/parser -> this goes red (the
    # typed-marker durability lock).
    assert v.not_ingested_size == 99


def test_created_payload_byte_identical_without_marker():
    """A CAS-ingested/small version's created payload adds NO new key (byte-parity)."""
    reg = ArtifactRegistry()
    out = reg.mint(
        workspace_id="ws1",
        name="plot.png",
        event_id="e1",
        kind=ArtifactKind.IMAGE,
        custody=Custody.CAS,
        mechanism=Mechanism.TOOL_SCHEMA,
        evidence=IdentityEvidence.hashed_at_use(sha256="b" * 64, size_bytes=10),
        producer={},
        path="/ws/plot.png",
        created_at="",
        annotation="",
    )
    payload = version_record_payload("e1", "ws1", "plot.png", out.version, revision_edge=False)
    # Sabotage: emit not_ingested_size unconditionally -> this key appears -> red
    # (the byte-parity lock: the S1/S2 created payload shape is unchanged).
    assert "not_ingested_size" not in payload


def test_cas_evicted_is_trace_only():
    # Sabotage: add artifact.cas.evicted to SSE_UI_EVENT_TYPES -> these go red (the
    # trace-only lock: CAS housekeeping never reaches the served wire).
    assert CAS_EVICTED_EVENT in SSE_TRACE_ONLY_EVENT_TYPES
    assert event_reaches_ui(CAS_EVICTED_EVENT) is False
    assert event_reaches_ui(CAS_EVICTED_EVENT, status="failed") is False


def test_one_ladder_no_second_byte_serving_path():
    """Structural: exactly ONE StreamingResponse site in the artifact routes (#972)."""
    routes = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "clio_agent"
        / "gact"
        / "routes"
        / "artifacts.py"
    )
    text = routes.read_text(encoding="utf-8")
    # Sabotage: add a second StreamingResponse (fork a CAS-only byte path) -> this
    # count goes to 2 -> red (the one-ladder review guard: no second byte path).
    assert len(re.findall(r"StreamingResponse\(", text)) == 1
    # And that sole site lives in the shared verify+stream primitive.
    assert "def _open_verify_stream(" in text
