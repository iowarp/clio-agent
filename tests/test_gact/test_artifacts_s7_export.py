"""S7 RO-Crate export + deterministic reproduction renderer (#973, item 3).

Covers the "give me the scripts" surface: the RO-Crate bundle (metadata schema +
round-trip — export → parse → artifacts + PROV edges present), the ``reproduce.py``
compilation matrix (a deterministic stage, an untranslatable/agentic-only stage, a
gap break, and the sha256 asserts actually executable — one compiled script run
against its own exported bytes in a temp env), and export-manifest GC-root
registration (closing S6's loop).

Each lock carries a sabotage note in its assertion comment: neutralizing the named
behaviour reddens the assertion.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.cas_gc import export_manifest_shas
from clio_agent.gact.artifacts.environment import EnvironmentRecord, EnvironmentTier
from clio_agent.gact.artifacts.export import (
    build_artifact_bundle,
    build_session_bundle,
    register_export_gc_roots,
)
from clio_agent.gact.artifacts.minting import mint_artifact
from clio_agent.gact.artifacts.records import (
    ArtifactKind,
    Custody,
    IdentityEvidence,
    Mechanism,
)
from clio_agent.gact.artifacts.registry import ArtifactRegistry
from clio_agent.gact.artifacts.reproduce import (
    ArtifactNode,
    StageVerdict,
    compile_notebook,
    compile_reproduce,
)
from clio_agent.gact.artifacts.transform_types import (
    EdgeEvidence,
    EdgeRole,
    Instrument,
    ProvEdge,
)
from clio_agent.gact.artifacts.transforms import TransformRecord, TransformStatus
from clio_agent.gact.sessions import SessionStore


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


def _export_app(tmp_path: Path):
    store = SessionStore(path=tmp_path / "sessions.json")
    state = SimpleNamespace(
        sessions=store,
        arc=_CapturingArc(),
        workspaces=_FakeWorkspaces({"ws1": str(tmp_path)}),
        semantic_event_sink=object(),
        semantic_trace_detail_level="semantic",
        semantic_trace_backend=None,
        artifact_registry=ArtifactRegistry(),
    )
    return SimpleNamespace(state=state), store


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mint(app, sid, *, name, path, kind, mechanism=Mechanism.TOOL_SCHEMA,
          custody=Custody.WORKSPACE_REFERENCED, gap=False, producer_call_id=""):
    data = Path(path).read_bytes() if Path(path).is_file() else b""
    evidence = IdentityEvidence.hashed_at_use(sha256=_sha(data), size_bytes=len(data))
    if gap:
        mechanism = Mechanism.NONE
    version = mint_artifact(
        app,
        sid,
        name=name,
        workspace_id="ws1",
        evidence=evidence,
        kind=kind,
        mechanism=mechanism,
        custody=custody,
        path=path,
        # In production the observer mints the version AND records the transform
        # under the SAME call_id, so wasGeneratedBy links to the CreateAction.
        producer={"session_id": sid, "call_id": producer_call_id or f"call_{name}"},
    )
    return version


def _lockfile_env() -> EnvironmentRecord:
    return EnvironmentRecord(
        tier=EnvironmentTier.LOCKFILE_HASH,
        clio_version="0.7.12",
        lockfile_sha256="a" * 64,
        model_id="anthropic/claude",
        provider_id="anthropic",
        python_version="3.12.0",
        os="Windows",
    )


def _transform(*, call_id, sid, tool, args, used, generated, env=None, status=TransformStatus.SUCCESS):
    return TransformRecord(
        call_id=call_id,
        session_id=sid,
        workspace_id="ws1",
        status=status,
        instrument=Instrument(tool=tool, args=args),
        environment=env or _lockfile_env(),
        used=used,
        generated=generated,
        started_at="2026-07-22T00:00:00Z",
        ended_at="2026-07-22T00:00:01Z",
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


def _build_ndp_scenario(app, sid, tmp_path: Path):
    """A stage → filter → plot chain + an untranslatable + a gap + an inline report."""
    reg = app.state.artifact_registry
    stations = tmp_path / "stations.csv"
    stations.write_text("time,east\n0,1\n1,2\n")
    clean = tmp_path / "clean.csv"
    clean.write_text("time,east\n0,1\n")
    plot = tmp_path / "plot.png"
    plot.write_bytes(b"\x89PNG" + b"0" * 40)
    weird = tmp_path / "weird.bin"
    weird.write_bytes(b"\x00\x01\x02")
    gapf = tmp_path / "gap.csv"
    gapf.write_text("x\n1\n")

    v_stations = _mint(app, sid, name="stations.csv", path=str(stations),
                       kind=ArtifactKind.DATASET, producer_call_id="t_stage")
    v_clean = _mint(app, sid, name="clean.csv", path=str(clean),
                    kind=ArtifactKind.DATASET, producer_call_id="t_filter")
    v_plot = _mint(app, sid, name="plot.png", path=str(plot),
                   kind=ArtifactKind.IMAGE, producer_call_id="t_plot")
    v_weird = _mint(app, sid, name="weird.bin", path=str(weird),
                    kind=ArtifactKind.OTHER, producer_call_id="t_weird")
    v_gap = _mint(app, sid, name="gap.csv", path=str(gapf), kind=ArtifactKind.DATASET,
                  gap=True, producer_call_id="t_gap")

    reg.record_transform(_transform(
        call_id="t_stage", sid=sid, tool="ndp_stage_resource",
        args={"source_url": "https://ds.example.org/stations.csv"},
        used=[], generated=[_gen_edge(v_stations)],
    ))
    reg.record_transform(_transform(
        call_id="t_filter", sid=sid, tool="pandas_filter_data",
        args={"expression": "east < 2", "columns": ["time", "east"]},
        used=[_use_edge(v_stations)], generated=[_gen_edge(v_clean)],
    ))
    reg.record_transform(_transform(
        call_id="t_plot", sid=sid, tool="plot_plot_timeseries",
        args={"x": "time", "y": "east"},
        used=[_use_edge(v_clean)], generated=[_gen_edge(v_plot)],
    ))
    reg.record_transform(_transform(
        call_id="t_weird", sid=sid, tool="quantum_frobnicator",
        args={"knob": 7}, used=[], generated=[_gen_edge(v_weird)],
    ))
    reg.record_transform(_transform(
        call_id="t_gap", sid=sid, tool="mystery_writer",
        args={}, used=[], generated=[_gen_edge(v_gap)],
    ))
    return {
        "stations": v_stations, "clean": v_clean, "plot": v_plot,
        "weird": v_weird, "gap": v_gap,
    }


# --------------------------------------------------------------------------- #
# RO-Crate metadata schema + round-trip.
# --------------------------------------------------------------------------- #


def test_ro_crate_roundtrip_artifacts_and_edges_present(tmp_path: Path) -> None:
    app, store = _export_app(tmp_path)
    sid = store.create(workspace_id="ws1", title="t").id
    versions = _build_ndp_scenario(app, sid, tmp_path)

    bundle = build_session_bundle(app, sid)
    assert bundle is not None
    zbytes = bundle.to_zip()

    # Round-trip: unzip → parse ro-crate-metadata.json.
    with zipfile.ZipFile(BytesIO(zbytes)) as zf:
        names = set(zf.namelist())
        meta = json.loads(zf.read("ro-crate-metadata.json"))
    assert "reproduce.py" in names
    assert "reproduce.ipynb" in names
    # Bytes shipped under data/ for each shipped artifact.
    assert any(n.startswith("data/") for n in names)

    graph = {e["@id"]: e for e in meta["@graph"]}
    # The metadata descriptor + root dataset conform to RO-Crate 1.1.
    assert graph["ro-crate-metadata.json"]["conformsTo"]["@id"] == "https://w3id.org/ro/crate/1.1"
    assert graph["./"]["@type"] == "Dataset"

    # Every registered artifact is a File entity carrying its sha256.
    file_entities = [e for e in meta["@graph"] if "File" in _types(e)]
    file_names = {e["name"] for e in file_entities}
    assert {"stations.csv", "clean.csv", "plot.png"} <= file_names
    csv_files = [e for e in file_entities if e.get("name") == "clean.csv"]
    assert csv_files and csv_files[0].get("sha256") == versions["clean"].sha256

    # TransformRecords serialize as CreateActions with object/result/instrument/agent.
    actions = [e for e in meta["@graph"] if "CreateAction" in _types(e)]
    assert len(actions) == 5
    filter_action = next(a for a in actions if "pandas_filter_data" in a["name"])
    assert filter_action["object"]  # used inputs (the stations CSV)
    assert filter_action["result"]  # generated outputs (the clean CSV)
    assert filter_action["instrument"]["@id"].startswith("#instrument-")
    assert filter_action["agent"]["@id"].startswith("#agent-")
    assert filter_action["startTime"] == "2026-07-22T00:00:00Z"

    # PROV: a File carries wasGeneratedBy pointing at its producing activity.
    clean_entity = csv_files[0]
    assert clean_entity["wasGeneratedBy"]["@id"] == "#activity-t_filter"

    # Gap version → its activity's agent is the unknown Agent (never a false author).
    unknown = [e for e in meta["@graph"] if e.get("@id") == "#agent-unknown"]
    assert unknown, "a gap version must attribute to the unknown Agent"
    gap_action = next(a for a in actions if a["name"].startswith("mystery_writer"))
    assert gap_action["agent"]["@id"] == "#agent-unknown"


def _types(entity: dict) -> set:
    t = entity.get("@type")
    return set(t) if isinstance(t, list) else {t}


def test_wasrevisionof_edge_present_for_v2(tmp_path: Path) -> None:
    app, store = _export_app(tmp_path)
    sid = store.create(workspace_id="ws1", title="t").id
    f = tmp_path / "series.csv"
    f.write_text("v1\n")
    _mint(app, sid, name="series.csv", path=str(f), kind=ArtifactKind.DATASET)
    f.write_text("v2 revised\n")  # new content → v2
    _mint(app, sid, name="series.csv", path=str(f), kind=ArtifactKind.DATASET)

    bundle = build_session_bundle(app, sid)
    meta = json.loads(bundle.files["ro-crate-metadata.json"])
    files = [e for e in meta["@graph"] if "File" in _types(e) and e.get("name") == "series.csv"]
    revisions = [e for e in files if "wasRevisionOf" in e]
    # v2 carries the PROV wasRevisionOf edge to v1. Sabotage: drop prior_version and
    # this reddens.
    assert len(revisions) == 1


# --------------------------------------------------------------------------- #
# reproduce.py compilation matrix.
# --------------------------------------------------------------------------- #


def test_reproduce_compilation_matrix(tmp_path: Path) -> None:
    app, store = _export_app(tmp_path)
    sid = store.create(workspace_id="ws1", title="t").id
    _build_ndp_scenario(app, sid, tmp_path)
    reg = app.state.artifact_registry
    transforms = reg.transforms_for_session(sid)
    # Resolve nodes as the export layer does.
    nodes: dict[str, ArtifactNode] = {}
    for record in reg.list_for_workspace("ws1"):
        for v in record.versions:
            nodes[v.artifact_id] = ArtifactNode(
                artifact_id=v.artifact_id, name=record.name, version=v.version,
                sha256=v.sha256, kind=v.kind.value, custody=v.custody.value,
                mechanism=v.mechanism.value, path=v.path,
                bundle_path=f"data/{record.name}",
            )
    script = compile_reproduce(transforms, nodes, environment=_lockfile_env())
    by_tool = {s.tool: s for s in script.stages}

    # Deterministic: the pandas filter (translatable + hash-pinned input + env ok).
    assert by_tool["pandas_filter_data"].verdict is StageVerdict.DETERMINISTIC
    # Re-runnable: the staged download (authority/mutable remote input).
    assert by_tool["ndp_stage_resource"].verdict is StageVerdict.RE_RUNNABLE
    # Re-runnable: the raster plot (env-sensitive rendering).
    assert by_tool["plot_plot_timeseries"].verdict is StageVerdict.RE_RUNNABLE
    # Agentic-only: the untranslatable tool.
    assert by_tool["quantum_frobnicator"].verdict is StageVerdict.AGENTIC_ONLY
    # Gap break: the mystery writer produced a mechanism=none version.
    assert by_tool["mystery_writer"].verdict is StageVerdict.GAP_BREAK

    # Every executable stage ends with a sha256 assert; the gap/agentic stages raise.
    assert "_assert_sha" in "\n".join(by_tool["pandas_filter_data"].code)
    assert "SystemExit" in "\n".join(by_tool["mystery_writer"].code)
    assert "AGENTIC-ONLY" in "\n".join(by_tool["quantum_frobnicator"].code)
    # The whole script is valid Python (compiles).
    compile(script.text, "reproduce.py", "exec")
    # The notebook variant is well-formed nbformat 4.
    nb = compile_notebook(script)
    assert nb["nbformat"] == 4 and nb["cells"]


def test_env_below_lockfile_downgrades_deterministic_to_rerunnable(tmp_path: Path) -> None:
    app, store = _export_app(tmp_path)
    sid = store.create(workspace_id="ws1", title="t").id
    _build_ndp_scenario(app, sid, tmp_path)
    reg = app.state.artifact_registry
    nodes = {
        v.artifact_id: ArtifactNode(
            artifact_id=v.artifact_id, name=r.name, version=v.version, sha256=v.sha256,
            kind=v.kind.value, custody=v.custody.value, mechanism=v.mechanism.value,
            path=v.path, bundle_path=f"data/{r.name}",
        )
        for r in reg.list_for_workspace("ws1") for v in r.versions
    }
    declared_env = EnvironmentRecord(tier=EnvironmentTier.DECLARED, clio_version="0.7.12")
    script = compile_reproduce(reg.transforms_for_session(sid), nodes, environment=declared_env)
    by_tool = {s.tool: s for s in script.stages}
    # env < lockfile-hash → the filter can no longer be bit-identical.
    assert by_tool["pandas_filter_data"].verdict is StageVerdict.RE_RUNNABLE
    assert by_tool["pandas_filter_data"].reason == "env_below_lockfile_hash"


# --------------------------------------------------------------------------- #
# Executable sha asserts — run a compiled script against its own bytes.
# --------------------------------------------------------------------------- #


def test_compiled_write_bytes_stage_executes_and_sha_asserts_pass(tmp_path: Path) -> None:
    app, store = _export_app(tmp_path)
    sid = store.create(workspace_id="ws1", title="t").id
    report = tmp_path / "report.md"
    report.write_text("# Findings\n\nThe station moved 3mm/yr.\n")
    # A model-designated inline artifact (no producing transform) → WRITE_BYTES stage.
    _mint(app, sid, name="report.md", path=str(report), kind=ArtifactKind.REPORT,
          mechanism=Mechanism.MODEL)

    bundle = build_session_bundle(app, sid)
    assert bundle is not None
    # Unzip the crate into a temp dir and RUN reproduce.py there.
    crate = tmp_path / "crate"
    crate.mkdir()
    with zipfile.ZipFile(BytesIO(bundle.to_zip())) as zf:
        zf.extractall(crate)
    # Sanity: the write-bytes stage is present and deterministic.
    script_text = (crate / "reproduce.py").read_text(encoding="utf-8")
    assert "write-bytes" in script_text

    proc = subprocess.run(
        [sys.executable, "reproduce.py"],
        cwd=crate,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # The compiled sha asserts execute and PASS against the crate's own bytes.
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "sha256 OK" in proc.stdout
    assert "all stages complete" in proc.stdout
    # The reproduced artifact was actually written with the pinned content.
    assert (crate / "report.md").read_text(encoding="utf-8") == report.read_text(encoding="utf-8")


def test_compiled_script_detects_a_tampered_pin(tmp_path: Path) -> None:
    """Sabotage twin: corrupt the exported bytes and the sha assert must FAIL."""
    app, store = _export_app(tmp_path)
    sid = store.create(workspace_id="ws1", title="t").id
    report = tmp_path / "report.md"
    report.write_text("# Findings\n")
    _mint(app, sid, name="report.md", path=str(report), kind=ArtifactKind.REPORT,
          mechanism=Mechanism.MODEL)
    bundle = build_session_bundle(app, sid)
    crate = tmp_path / "crate"
    crate.mkdir()
    with zipfile.ZipFile(BytesIO(bundle.to_zip())) as zf:
        zf.extractall(crate)
    # Corrupt the shipped bytes AFTER export (the pin no longer matches).
    data_file = next((crate / "data").glob("report*"))
    data_file.write_text("# Tampered\n")
    proc = subprocess.run(
        [sys.executable, "reproduce.py"], cwd=crate, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode != 0
    assert "sha256 MISMATCH" in (proc.stdout + proc.stderr)


# --------------------------------------------------------------------------- #
# GC-root registration (closing S6's loop).
# --------------------------------------------------------------------------- #


def test_export_registers_cas_gc_roots(tmp_path: Path) -> None:
    app, store = _export_app(tmp_path)
    sid = store.create(workspace_id="ws1", title="t").id
    versions = _build_ndp_scenario(app, sid, tmp_path)

    bundle = build_session_bundle(app, sid)
    assert bundle.crate_shas  # the shipped content hashes
    register_export_gc_roots(app, "ws1", bundle.crate_shas)

    roots = export_manifest_shas(app, "ws1")
    # The exported artifact hashes are now GC roots (never evicted out from a bundle).
    assert versions["clean"].sha256 in roots
    assert versions["plot"].sha256 in roots
    # Idempotent union: a second export never loses earlier roots.
    register_export_gc_roots(app, "ws1", {"deadbeef" * 8})
    assert versions["clean"].sha256 in export_manifest_shas(app, "ws1")


def test_export_unknown_ids_return_none(tmp_path: Path) -> None:
    app, _store = _export_app(tmp_path)
    assert build_session_bundle(app, "sess_missing") is None
    assert build_artifact_bundle(app, "artifact_missing") is None


def test_export_routes_serve_zip_and_pin_gc_roots(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as c:
        wid = c.post("/v1/workspaces", json={"name": "w", "root_path": str(tmp_path)}).json()["id"]
        sid = c.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]
        (tmp_path / "fig.png").write_bytes(b"\x89PNG route")
        pinned = c.post(
            f"/v1/sessions/{sid}/artifacts/pin", json={"path": "fig.png"}
        ).json()["pinned"]
        aid = pinned["artifact_id"]

        # Per-artifact export route → a parseable RO-Crate zip.
        r = c.get(f"/v1/artifacts/{aid}/export")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(BytesIO(r.content)) as zf:
            assert "ro-crate-metadata.json" in zf.namelist()
            assert "reproduce.py" in zf.namelist()

        # Per-session bundle route → a zip.
        rs = c.get(f"/v1/sessions/{sid}/export/bundle")
        assert rs.status_code == 200
        with zipfile.ZipFile(BytesIO(rs.content)) as zf:
            json.loads(zf.read("ro-crate-metadata.json"))

        # Unknown ids are honest 404s.
        assert c.get("/v1/artifacts/artifact_nope/export").status_code == 404
        assert c.get("/v1/sessions/sess_nope/export/bundle").status_code == 404


def test_single_artifact_bundle_carries_its_lineage(tmp_path: Path) -> None:
    app, store = _export_app(tmp_path)
    sid = store.create(workspace_id="ws1", title="t").id
    versions = _build_ndp_scenario(app, sid, tmp_path)

    bundle = build_artifact_bundle(app, versions["plot"].artifact_id)
    assert bundle is not None
    meta = json.loads(bundle.files["ro-crate-metadata.json"])
    actions = [e for e in meta["@graph"] if "CreateAction" in _types(e)]
    # The plot's producing activity is present in its lineage bundle.
    assert any(a["@id"] == "#activity-t_plot" for a in actions)
    # The one-hop input (clean.csv) is pulled in so the reproduce chain is closed.
    files = {e.get("name") for e in meta["@graph"] if "File" in _types(e)}
    assert "clean.csv" in files
