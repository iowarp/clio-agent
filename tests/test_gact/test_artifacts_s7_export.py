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
import os
import subprocess
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_per_stage_env_tier_gates_determinism_independently(tmp_path: Path) -> None:
    """Finding [6]: each stage's DETERMINISTIC/RE_RUNNABLE verdict comes from THAT
    stage's OWN transform environment tier, never a blanket tier from the first
    transform. Two pandas stages sharing a hash-pinned input but recorded under
    different env tiers must get DIFFERENT verdicts in ONE compile (no override).
    """
    app, store = _export_app(tmp_path)
    sid = store.create(workspace_id="ws1", title="t").id
    src = tmp_path / "src.csv"
    src.write_text("time,east\n0,1\n1,2\n")
    a = tmp_path / "a.csv"
    a.write_text("time,east\n0,1\n")
    b = tmp_path / "b.csv"
    b.write_text("time,east\n1,2\n")
    v_src = _mint(app, sid, name="src.csv", path=str(src), kind=ArtifactKind.DATASET,
                  producer_call_id="t_src")
    v_a = _mint(app, sid, name="a.csv", path=str(a), kind=ArtifactKind.DATASET,
                producer_call_id="t_weak")
    v_b = _mint(app, sid, name="b.csv", path=str(b), kind=ArtifactKind.DATASET,
                producer_call_id="t_strong")
    reg = app.state.artifact_registry
    declared_env = EnvironmentRecord(tier=EnvironmentTier.DECLARED, clio_version="0.7.12")
    # Stage under a WEAK (declared) env → its OWN tier downgrades it.
    reg.record_transform(_transform(
        call_id="t_weak", sid=sid, tool="pandas_filter_data",
        args={"expression": "east < 2"}, used=[_use_edge(v_src)], generated=[_gen_edge(v_a)],
        env=declared_env,
    ))
    # Stage under a STRONG (lockfile-hash) env, SAME compile → stays deterministic.
    reg.record_transform(_transform(
        call_id="t_strong", sid=sid, tool="pandas_filter_data",
        args={"expression": "east < 2"}, used=[_use_edge(v_src)], generated=[_gen_edge(v_b)],
        env=_lockfile_env(),
    ))
    nodes = {
        v.artifact_id: ArtifactNode(
            artifact_id=v.artifact_id, name=r.name, version=v.version, sha256=v.sha256,
            kind=v.kind.value, custody=v.custody.value, mechanism=v.mechanism.value,
            path=v.path, bundle_path=f"data/{r.name}",
        )
        for r in reg.list_for_workspace("ws1") for v in r.versions
    }
    # NO global environment override — each stage decides from its own record.
    script = compile_reproduce(reg.transforms_for_session(sid), nodes)
    by_call = {s.call_id: s for s in script.stages}
    # The weak-env stage downgrades; the strong-env stage does NOT. Sabotage: gate on
    # ordered[0].environment (a blanket tier) and both verdicts collapse to one.
    assert by_call["t_weak"].verdict is StageVerdict.RE_RUNNABLE
    assert by_call["t_weak"].reason == "env_below_lockfile_hash"
    assert by_call["t_strong"].verdict is StageVerdict.DETERMINISTIC
    assert v_src.sha256 and v_a.sha256 and v_b.sha256  # inputs/outputs are hash-pinned


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
    # Corrupt the shipped bytes AFTER export (the pin no longer matches). Bytes ship
    # under a workspace-namespaced data/<ws>/ subdir now (finding [11]), so recurse.
    data_file = next((crate / "data").rglob("report*"))
    data_file.write_text("# Tampered\n")
    proc = subprocess.run(
        [sys.executable, "reproduce.py"], cwd=crate, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode != 0
    assert "sha256 MISMATCH" in (proc.stdout + proc.stderr)


# --------------------------------------------------------------------------- #
# Executable proof: a REAL translated multi-stage chain runs end-to-end (findings
# [7]/[12]) — a temp-http requests download, the pandas filter, the matplotlib plot,
# with the compiled per-stage sha pins asserted in a temp subprocess.
# --------------------------------------------------------------------------- #


def test_translated_download_pandas_plot_chain_runs_and_pins_pass(tmp_path: Path) -> None:
    import functools
    import http.server
    import threading

    import pandas as pd

    app, store = _export_app(tmp_path)
    sid = store.create(workspace_id="ws1", title="t").id

    # Serve the staged CSV over a real local HTTP server (the download stage fetches it).
    serve_dir = tmp_path / "remote"
    serve_dir.mkdir()
    stations = serve_dir / "stations.csv"
    stations.write_text("time,east\n0,1\n1,2\n2,5\n")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(serve_dir))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        # Build the fixture outputs by running the SAME operations the translators emit,
        # so the recorded pins are exactly what a faithful reproduction reproduces.
        ws_stations = tmp_path / "stations.csv"
        ws_stations.write_bytes(stations.read_bytes())
        clean = tmp_path / "clean.csv"
        _df = pd.read_csv(ws_stations)
        _df = _df.query("east < 2")
        _df = _df[["time", "east"]]
        _df.to_csv(clean, index=False)
        plot = tmp_path / "plot.png"
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        _df2 = pd.read_csv(clean)
        _fig, _ax = plt.subplots()
        _ax.plot(_df2["time"], _df2["east"])
        _fig.savefig(plot)

        v_stations = _mint(app, sid, name="stations.csv", path=str(ws_stations),
                           kind=ArtifactKind.DATASET, producer_call_id="t_dl")
        v_clean = _mint(app, sid, name="clean.csv", path=str(clean),
                        kind=ArtifactKind.DATASET, producer_call_id="t_pd")
        v_plot = _mint(app, sid, name="plot.png", path=str(plot),
                       kind=ArtifactKind.IMAGE, producer_call_id="t_plot")
        reg = app.state.artifact_registry
        reg.record_transform(_transform(
            call_id="t_dl", sid=sid, tool="ndp_stage_resource",
            args={"source_url": f"http://127.0.0.1:{port}/stations.csv"},
            used=[], generated=[_gen_edge(v_stations)],
        ))
        reg.record_transform(_transform(
            call_id="t_pd", sid=sid, tool="pandas_filter_data",
            args={"expression": "east < 2", "columns": ["time", "east"]},
            used=[_use_edge(v_stations)], generated=[_gen_edge(v_clean)],
        ))
        reg.record_transform(_transform(
            call_id="t_plot", sid=sid, tool="plot_plot_timeseries",
            args={"x": "time", "y": "east"},
            used=[_use_edge(v_clean)], generated=[_gen_edge(v_plot)],
        ))

        bundle = build_session_bundle(app, sid)
        assert bundle is not None
        crate = tmp_path / "crate"
        crate.mkdir()
        with zipfile.ZipFile(BytesIO(bundle.to_zip())) as zf:
            zf.extractall(crate)
        proc = subprocess.run(
            [sys.executable, "reproduce.py"], cwd=crate,
            capture_output=True, text=True, timeout=180,
        )
    finally:
        httpd.shutdown()
        thread.join(timeout=5)

    # The real translated download + pandas + plot stages ran and every compiled pin
    # PASSED end-to-end. Sabotage: break any translator's codegen (wrong arg key, wrong
    # I/O format) and the reproduced bytes diverge → a sha256 MISMATCH reddens this.
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all stages complete" in proc.stdout
    assert proc.stdout.count("sha256 OK") == 3  # stations, clean, plot all pinned + matched
    # The chain actually materialized each stage's output at the crate root.
    assert (crate / "stations.csv").is_file()
    assert (crate / "clean.csv").read_text() == clean.read_text()
    assert (crate / "plot.png").read_bytes() == plot.read_bytes()


def test_notebook_first_cell_runs_without_dunder_file(tmp_path: Path) -> None:
    """Finding [4]: a Jupyter kernel has NO ``__file__``. The notebook's first code cell
    must run cwd-anchored (asserting the crate layout), never crash on ``__file__``.

    Executed in a ``__file__``-less namespace (exactly a kernel's globals). The SABOTAGE
    twin proves the original script preamble WOULD have crashed there.
    """
    from clio_agent.gact.artifacts.reproduce import _NOTEBOOK_PREAMBLE, _PREAMBLE

    script = compile_reproduce([], {})
    nb = compile_notebook(script)
    code_cells = [c for c in nb["cells"] if c["cell_type"] == "code"]
    first_cell = "".join(code_cells[0]["source"])
    # No __file__ USED as code (a comment may name it); a cwd-anchored layout assertion.
    import ast as _ast

    names = {n.id for n in _ast.walk(_ast.parse(first_cell)) if isinstance(n, _ast.Name)}
    assert "__file__" not in names
    assert 'os.path.isdir("data")' in first_cell  # cwd-anchored layout assertion

    crate = tmp_path / "crate"
    (crate / "data").mkdir(parents=True)
    cwd = os.getcwd()
    os.chdir(crate)
    try:
        # A kernel namespace: NO __file__. The notebook cell must NOT raise.
        exec(first_cell, {"__name__": "__main__"})  # noqa: S102
        # Sabotage twin: the SCRIPT preamble's __file__ bootstrap DOES raise here.
        with pytest.raises(NameError):
            exec(_PREAMBLE, {"__name__": "__main__"})  # noqa: S102
        assert _NOTEBOOK_PREAMBLE  # the two preambles are distinct sources
    finally:
        os.chdir(cwd)


# --------------------------------------------------------------------------- #
# Cross-workspace bundle collisions (finding [11]).
# --------------------------------------------------------------------------- #


class _FakeTask:
    def __init__(self, child_session_id: str) -> None:
        self.child_session_id = child_session_id


class _FakeTaskRegistry:
    def __init__(self, by_parent: dict[str, list[str]]) -> None:
        self._by_parent = by_parent

    def for_parent(self, parent: str) -> list[_FakeTask]:
        return [_FakeTask(c) for c in self._by_parent.get(parent, [])]


def test_same_name_across_unioned_workspaces_do_not_collide(tmp_path: Path) -> None:
    """Finding [11]: a parent's include_children export unions delegate workspaces; two
    DIFFERENT ``plot.png`` v1 records (parent + child) must ship distinct bytes under
    distinct data/ paths AND distinct JSON-LD @ids — never a silent overwrite.
    """
    parent_root = tmp_path / "parent"
    child_root = tmp_path / "child"
    parent_root.mkdir()
    child_root.mkdir()
    store = SessionStore(path=tmp_path / "sessions.json")
    state = SimpleNamespace(
        sessions=store,
        arc=_CapturingArc(),
        workspaces=_FakeWorkspaces({"ws_parent": str(parent_root), "ws_child": str(child_root)}),
        semantic_event_sink=object(),
        semantic_trace_detail_level="semantic",
        semantic_trace_backend=None,
        artifact_registry=ArtifactRegistry(),
    )
    app = SimpleNamespace(state=state)
    parent_sid = store.create(workspace_id="ws_parent", title="t").id
    child_sid = store.create(workspace_id="ws_child", title="t").id
    app.state.agent_task_registry = _FakeTaskRegistry({parent_sid: [child_sid]})

    parent_png = parent_root / "plot.png"
    parent_png.write_bytes(b"\x89PNG-PARENT-BYTES")
    child_png = child_root / "plot.png"
    child_png.write_bytes(b"\x89PNG-CHILD-DIFFERENT")
    mint_artifact(app, parent_sid, name="plot.png", path=str(parent_png),
                  workspace_id="ws_parent", kind=ArtifactKind.IMAGE,
                  evidence=IdentityEvidence.hashed_at_use(
                      sha256=_sha(parent_png.read_bytes()), size_bytes=parent_png.stat().st_size),
                  mechanism=Mechanism.TOOL_SCHEMA, custody=Custody.WORKSPACE_REFERENCED,
                  producer={"tool": "plot_plot_timeseries", "call_id": "p1"})
    mint_artifact(app, child_sid, name="plot.png", path=str(child_png),
                  workspace_id="ws_child", kind=ArtifactKind.IMAGE,
                  evidence=IdentityEvidence.hashed_at_use(
                      sha256=_sha(child_png.read_bytes()), size_bytes=child_png.stat().st_size),
                  mechanism=Mechanism.TOOL_SCHEMA, custody=Custody.WORKSPACE_REFERENCED,
                  producer={"tool": "plot_plot_timeseries", "call_id": "c1"})

    bundle = build_session_bundle(app, parent_sid)
    assert bundle is not None
    data_files = [n for n in bundle.files if n.startswith("data/")]
    # BOTH records' bytes ship, to DISTINCT workspace-namespaced paths (no overwrite).
    assert len(data_files) == 2, data_files
    shipped = {bundle.files[n] for n in data_files}
    assert parent_png.read_bytes() in shipped
    assert child_png.read_bytes() in shipped  # sabotage: name-only path → child clobbers parent
    # The two File entities carry DISTINCT @ids (a duplicate @id is a malformed crate).
    meta = json.loads(bundle.files["ro-crate-metadata.json"])
    png_ids = [e["@id"] for e in meta["@graph"]
               if "File" in _types(e) and e.get("name") == "plot.png"]
    assert len(png_ids) == 2 and len(set(png_ids)) == 2, png_ids


# --------------------------------------------------------------------------- #
# Injection safety + translation fidelity (finding [9]).
# --------------------------------------------------------------------------- #


def test_query_arg_cannot_escape_its_string_literal(tmp_path: Path) -> None:
    """SECURITY (finding [9]): a recorded query arg is model/data-derived text compiled
    into user-runnable Python. Quotes / newlines / an ``import os`` payload must be
    contained in a string literal and NEVER become executable code.
    """
    import ast

    app, store = _export_app(tmp_path)
    sid = store.create(workspace_id="ws1", title="t").id
    src = tmp_path / "src.csv"
    src.write_text("time,east\n0,1\n")
    out = tmp_path / "out.csv"
    out.write_text("time,east\n0,1\n")
    v_src = _mint(app, sid, name="src.csv", path=str(src), kind=ArtifactKind.DATASET,
                  producer_call_id="t_src")
    v_out = _mint(app, sid, name="out.csv", path=str(out), kind=ArtifactKind.DATASET,
                  producer_call_id="t_evil")
    payload = "east < 2') ; import os ; os.system('touch /tmp/pwned')\n#"
    reg = app.state.artifact_registry
    reg.record_transform(_transform(
        call_id="t_evil", sid=sid, tool="pandas_filter_data",
        args={"expression": payload}, used=[_use_edge(v_src)], generated=[_gen_edge(v_out)],
    ))
    nodes = {
        v.artifact_id: ArtifactNode(
            artifact_id=v.artifact_id, name=r.name, version=v.version, sha256=v.sha256,
            kind=v.kind.value, custody=v.custody.value, mechanism=v.mechanism.value,
            path=v.path, bundle_path=f"data/{r.name}",
        )
        for r in reg.list_for_workspace("ws1") for v in r.versions
    }
    script = compile_reproduce(reg.transforms_for_session(sid), nodes)
    # (1) The whole script is still valid Python — the payload did not break syntax.
    tree = ast.parse(script.text)
    # (2) The ONLY os-import in the script is the preamble's own; the payload's
    # "import os" is inert text inside a string literal, not an executable statement.
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imported == {"hashlib", "os", "shutil", "sys", "pandas"}, imported
    # (3) The df.query argument is a single string CONSTANT equal to the raw payload —
    # data, never code. Sabotage: f-string the value raw and this is no longer a
    # Constant (or the syntax breaks).
    query_consts = [
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "query"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ]
    assert payload in query_consts


def test_pandas_unrecognized_filter_shape_is_rerunnable_without_sha_assert(tmp_path: Path) -> None:
    """Finding [9]: the real ``pandas_filter_data`` takes a structured ``filter_conditions``
    DSL, not a query expression. That shape is NOT reproduced — the stage must be a
    re-runnable MCP reference with NO sha assert it cannot back, never a DETERMINISTIC
    bare CSV round-trip that silently drops the filter.
    """
    app, store = _export_app(tmp_path)
    sid = store.create(workspace_id="ws1", title="t").id
    src = tmp_path / "in.csv"
    src.write_text("time,east\n0,1\n")
    out = tmp_path / "filtered.csv"
    out.write_text("time,east\n0,1\n")
    v_src = _mint(app, sid, name="in.csv", path=str(src), kind=ArtifactKind.DATASET,
                  producer_call_id="t_src")
    v_out = _mint(app, sid, name="filtered.csv", path=str(out), kind=ArtifactKind.DATASET,
                  producer_call_id="t_f")
    reg = app.state.artifact_registry
    reg.record_transform(_transform(
        call_id="t_f", sid=sid, tool="pandas_filter_data",
        args={"file_path": str(src), "filter_conditions": {"east": {"lt": 2}},
              "output_file": str(out)},
        used=[_use_edge(v_src)], generated=[_gen_edge(v_out)],
    ))
    nodes = {
        v.artifact_id: ArtifactNode(
            artifact_id=v.artifact_id, name=r.name, version=v.version, sha256=v.sha256,
            kind=v.kind.value, custody=v.custody.value, mechanism=v.mechanism.value,
            path=v.path, bundle_path=f"data/{r.name}",
        )
        for r in reg.list_for_workspace("ws1") for v in r.versions
    }
    script = compile_reproduce(reg.transforms_for_session(sid), nodes)
    stage = next(s for s in script.stages if s.call_id == "t_f")
    code = "\n".join(stage.code)
    assert stage.verdict is StageVerdict.RE_RUNNABLE
    assert stage.reason == "pandas_filter_shape_not_reproduced"
    assert "read_csv" not in code  # no bogus deterministic round-trip
    assert "_assert_sha" not in code  # NO sha assert it cannot back
    assert "NOT REPRODUCED" in code


def test_configure_tool_is_not_mistranslated_as_a_plot(tmp_path: Path) -> None:
    """Finding [3]: exact tool-name matching. ``reconfigure_workspace`` (contains the
    substring ``figure``) must NOT route to the plot translator — it is agentic-only.
    """
    app, store = _export_app(tmp_path)
    sid = store.create(workspace_id="ws1", title="t").id
    out = tmp_path / "cfg.json"
    out.write_text("{}\n")
    v_out = _mint(app, sid, name="cfg.json", path=str(out), kind=ArtifactKind.CONFIG,
                  producer_call_id="t_cfg")
    reg = app.state.artifact_registry
    reg.record_transform(_transform(
        call_id="t_cfg", sid=sid, tool="reconfigure_workspace", args={"knob": 1},
        used=[], generated=[_gen_edge(v_out)],
    ))
    nodes = {
        v.artifact_id: ArtifactNode(
            artifact_id=v.artifact_id, name=r.name, version=v.version, sha256=v.sha256,
            kind=v.kind.value, custody=v.custody.value, mechanism=v.mechanism.value,
            path=v.path, bundle_path=f"data/{r.name}",
        )
        for r in reg.list_for_workspace("ws1") for v in r.versions
    }
    script = compile_reproduce(reg.transforms_for_session(sid), nodes)
    stage = next(s for s in script.stages if s.call_id == "t_cfg")
    code = "\n".join(stage.code)
    # Sabotage: revert to substring matching and 'figure' in 'reconfigure' → a bogus
    # matplotlib plot stage with an un-passable sha assert reddens this.
    assert stage.verdict is StageVerdict.AGENTIC_ONLY
    assert "matplotlib" not in code
    assert "savefig" not in code


# --------------------------------------------------------------------------- #
# Multi-output stages assert only what they write (findings [8]/[14]).
# --------------------------------------------------------------------------- #


def test_multi_output_stage_asserts_only_the_written_output(tmp_path: Path) -> None:
    app, store = _export_app(tmp_path)
    sid = store.create(workspace_id="ws1", title="t").id
    src = tmp_path / "src.csv"
    src.write_text("time,east\n0,1\n1,3\n")
    primary = tmp_path / "kept.csv"
    primary.write_text("time,east\n0,1\n")
    sidecar = tmp_path / "rejects.csv"
    sidecar.write_text("time,east\n1,3\n")
    v_src = _mint(app, sid, name="src.csv", path=str(src), kind=ArtifactKind.DATASET,
                  producer_call_id="t_src")
    v_primary = _mint(app, sid, name="kept.csv", path=str(primary), kind=ArtifactKind.DATASET,
                      producer_call_id="t_multi")
    v_sidecar = _mint(app, sid, name="rejects.csv", path=str(sidecar), kind=ArtifactKind.DATASET,
                      producer_call_id="t_multi")
    reg = app.state.artifact_registry
    reg.record_transform(_transform(
        call_id="t_multi", sid=sid, tool="pandas_filter_data",
        args={"expression": "east < 2"}, used=[_use_edge(v_src)],
        generated=[_gen_edge(v_primary), _gen_edge(v_sidecar)],
    ))
    nodes = {
        v.artifact_id: ArtifactNode(
            artifact_id=v.artifact_id, name=r.name, version=v.version, sha256=v.sha256,
            kind=v.kind.value, custody=v.custody.value, mechanism=v.mechanism.value,
            path=v.path, bundle_path=f"data/{r.name}",
        )
        for r in reg.list_for_workspace("ws1") for v in r.versions
    }
    script = compile_reproduce(reg.transforms_for_session(sid), nodes)
    stage = next(s for s in script.stages if s.call_id == "t_multi")
    code = "\n".join(stage.code)
    # Only the primary output gets a sha assert; the sidecar the translation never
    # writes gets a typed note, NOT an _assert_sha against a missing file (which would
    # crash the whole reproduce.py with FileNotFoundError). Sabotage: assert every
    # output and this stage emits `_assert_sha('rejects.csv', ...)` → reddens.
    assert "_assert_sha('kept.csv'" in code
    assert "_assert_sha('rejects.csv'" not in code
    assert "unreproduced_output: rejects.csv" in code
    # The whole script still compiles.
    compile(script.text, "reproduce.py", "exec")


# --------------------------------------------------------------------------- #
# RO-Crate metadata conformance (findings [5]/[10]).
# --------------------------------------------------------------------------- #


def test_context_maps_both_prov_lineage_terms(tmp_path: Path) -> None:
    """Finding [5]: wasGeneratedBy must be mapped in @context or a strict JSON-LD
    expansion drops every File→producing-Activity edge (only wasRevisionOf was mapped).
    """
    app, store = _export_app(tmp_path)
    sid = store.create(workspace_id="ws1", title="t").id
    _build_ndp_scenario(app, sid, tmp_path)
    bundle = build_session_bundle(app, sid)
    meta = json.loads(bundle.files["ro-crate-metadata.json"])
    ctx = next(c for c in meta["@context"] if isinstance(c, dict))
    assert ctx.get("wasGeneratedBy") == "prov:wasGeneratedBy"  # sabotage: drop → reddens
    assert ctx.get("wasRevisionOf") == "prov:wasRevisionOf"


def test_root_dataset_has_datepublished_and_license(tmp_path: Path, monkeypatch) -> None:
    """Finding [10]: the RO-Crate Root Data Entity carries datePublished (MUST) and
    license (SHOULD; NOASSERTION default, config-overridable)."""
    import re as _re

    app, store = _export_app(tmp_path)
    sid = store.create(workspace_id="ws1", title="t").id
    _build_ndp_scenario(app, sid, tmp_path)
    bundle = build_session_bundle(app, sid)
    root = json.loads(bundle.files["ro-crate-metadata.json"])
    graph = {e["@id"]: e for e in root["@graph"]}
    assert _re.fullmatch(r"\d{4}-\d{2}-\d{2}", graph["./"]["datePublished"])
    assert graph["./"]["license"] == "NOASSERTION"

    # The license knob overrides the default (config-first, #985).
    monkeypatch.setenv("CLIO_ARTIFACTS_EXPORT_LICENSE", "CC-BY-4.0")
    g2 = {
        e["@id"]: e
        for e in json.loads(build_session_bundle(app, sid).files["ro-crate-metadata.json"])["@graph"]
    }
    assert g2["./"]["license"] == "CC-BY-4.0"


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
