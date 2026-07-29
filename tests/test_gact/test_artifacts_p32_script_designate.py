"""P3.2 (#1039) — script designate-on-USE unit tests.

A CONSUMED script (a `.py`/`.sh` a tool executed as an input) is minted as its OWN
first-class SCRIPT artifact version, and the used edge carries that minted
``artifact_id`` so ``_script_instrument`` pins the script as the transform's own
dependency (``script_hash`` + ``script_artifact_id``). Designate-on-USE, not
designate-on-write: a `.py` passed as an OUTPUT arg, or one this call freshly wrote,
is NOT designated (only a consumed script is). The SCRIPT version renders as an
``artifact`` node (TOOL_SCHEMA basis, not a ``gap``) and recurses in lineage; a
second unchanged use dedups (no v2 churn); it composes with the #1038 cross-job bind
(a sibling job's script under a shared root reuses the FOREIGN id — no local fork).

Each key lock carries a sabotage note: the referenced neutralization turns the named
assertion red, proving the test binds the invariant (not a tautology).
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from clio_agent.gact.artifacts.lineage import build_lineage
from clio_agent.gact.artifacts.minting import mint_tool_declared_outputs
from clio_agent.gact.artifacts.records import ArtifactKind, Mechanism
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.artifacts.transform_edges import detect_used_edges
from clio_agent.gact.artifacts.transform_types import EdgeEvidence
from clio_agent.gact.artifacts.transforms import _script_instrument, record_transform
from clio_agent.gact.sessions import SessionStore

# --------------------------------------------------------------------------- #
# Fakes (parity with the P3.1 cross-job harness).
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


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_app(tmp_path: Path, ws_ids, roots=None):
    """App whose workspaces (``ws_ids``) share ``tmp_path`` as root by default."""
    store = SessionStore(path=tmp_path / "sessions.json")
    roots = roots or {wid: str(tmp_path) for wid in ws_ids}
    sessions = {wid: store.create(workspace_id=wid, title=wid) for wid in ws_ids}
    arc = _CapturingArc()
    state = SimpleNamespace(
        sessions=store,
        arc=arc,
        workspaces=_FakeWorkspaces(roots),
        semantic_event_sink=object(),
        semantic_trace_detail_level="semantic",
        semantic_trace_backend=None,
        artifact_registry=None,
        in_flight_turns={},
    )
    return SimpleNamespace(state=state), sessions, arc


def _write_old(path: Path, data: bytes) -> None:
    """Write ``data`` and back-date the mtime so no freshness guard ever flags it."""
    path.write_bytes(data)
    old = time.time() - 10_000.0
    os.utime(path, (old, old))


# --------------------------------------------------------------------------- #
# 1. A consumed script mints a SCRIPT version; the edge + instrument carry its id.
# --------------------------------------------------------------------------- #


def test_used_script_mints_script_version_and_edge_carries_id(tmp_path):
    app, sess, _arc = _make_app(tmp_path, ["ws1"])
    body = b"import sys\nprint('analyze')\n"
    script = tmp_path / "analyze.py"
    _write_old(script, body)
    scan = detect_used_edges(
        app,
        sess["ws1"].id,
        args={"script_path": str(script)},
        workspace_id="ws1",
        turn_id="t1",
        trace_id="tr1",
    )
    assert len(scan.edges) == 1
    e = scan.edges[0]
    # A MATCHED (not external) hash-pair edge pointing at the freshly minted SCRIPT.
    # Sabotage: drop the mint branch -> external:path leaf (empty artifact_id) -> red.
    assert e.evidence is EdgeEvidence.HASH_PAIR
    assert e.artifact_id
    assert e.external_ref == ""
    assert e.sha256 == _sha(body)
    # A first-class SCRIPT version under the LOCAL workspace, TOOL_SCHEMA basis.
    reg = get_registry(app)
    rec = reg.get("ws1", "analyze.py")
    assert rec is not None and rec.head is not None
    assert rec.head.kind is ArtifactKind.SCRIPT
    assert rec.head.mechanism is Mechanism.TOOL_SCHEMA
    assert rec.head.artifact_id == e.artifact_id
    # _script_instrument promotes the used script to {script_hash, script_artifact_id}
    # (BOTH were "" before this slice). Sabotage: leave artifact_id="" on the edge ->
    # script_artifact_id stays "" -> red.
    instrument = _script_instrument("run_python", {"script_path": str(script)}, scan.edges)
    assert instrument.script_hash == e.sha256
    assert instrument.script_artifact_id == e.artifact_id
    assert instrument.script_artifact_id != ""


def test_used_shell_script_designated_too(tmp_path):
    app, sess, _arc = _make_app(tmp_path, ["ws1"])
    body = b"#!/bin/sh\necho hi\n"
    script = tmp_path / "run.sh"
    _write_old(script, body)
    scan = detect_used_edges(
        app,
        sess["ws1"].id,
        args={"cmd_script": str(script)},
        workspace_id="ws1",
        turn_id="t1",
        trace_id="tr1",
    )
    assert len(scan.edges) == 1 and scan.edges[0].artifact_id
    assert get_registry(app).get("ws1", "run.sh").head.kind is ArtifactKind.SCRIPT


# --------------------------------------------------------------------------- #
# 2. Precision — a scratch / output-arg / freshly-written .py is NOT designated.
# --------------------------------------------------------------------------- #


def test_script_under_output_arg_is_not_designated(tmp_path):
    app, sess, _arc = _make_app(tmp_path, ["ws1"])
    script = tmp_path / "generated.py"
    _write_old(script, b"# emitted by the tool\n")
    # Passed under an OUTPUT_PATH_ARG_NAMES key -> _candidate_arg_strings excludes it,
    # so it is never a used-input candidate. Sabotage: walk output args too -> mint -> red.
    scan = detect_used_edges(
        app,
        sess["ws1"].id,
        args={"output_path": str(script)},
        workspace_id="ws1",
        turn_id="t1",
        trace_id="tr1",
    )
    assert scan.edges == []
    assert get_registry(app).get("ws1", "generated.py") is None


def test_freshly_written_script_is_not_designated(tmp_path):
    app, sess, _arc = _make_app(tmp_path, ["ws1"])
    # Fixed synthetic call start; the script's mtime is pinned deterministically PAST
    # it (mirroring test_artifacts_s5's os.utime pattern). Sampling ``time.time()`` and
    # then writing the file microseconds later — the previous form — leaves both
    # timestamps within one float64 ULP (~238 ns at epoch-2026 magnitudes), so a
    # re-statted ``st_mtime`` (an OS 100 ns-tick FILETIME converted to a float) rounds
    # to exactly one ULP BELOW ``call_t0`` ~9% of the time and inverts the
    # ``mtime >= call_started_at`` guard — the historical Windows + CI-Linux flake
    # (a false HASH_PAIR edge). The runtime guard is correct (production tool runs put
    # ms+ between call start and any written output); only this fixture manufactured a
    # sub-ULP gap. os.utime pins mtime = call_t0 + 100 so the comparison is exact.
    call_t0 = 1_000_000.0
    script = tmp_path / "scratch.py"
    # Written AT/AFTER the call start -> the freshness guard flags it as an output
    # candidate, never a used input. Sabotage: drop the mtime>=call_started_at guard
    # -> it mints -> red (and the note disappears).
    script.write_bytes(b"# just written this call\n")
    os.utime(script, (call_t0 + 100, call_t0 + 100))
    scan = detect_used_edges(
        app,
        sess["ws1"].id,
        args={"script_path": str(script)},
        workspace_id="ws1",
        turn_id="t1",
        trace_id="tr1",
        call_started_at=call_t0,
    )
    assert scan.edges == []
    assert any(n["reason"] == "unminted_output_candidate" for n in scan.notes)
    assert get_registry(app).get("ws1", "scratch.py") is None


def test_non_script_input_stays_external_leaf(tmp_path):
    app, sess, _arc = _make_app(tmp_path, ["ws1"])
    data = tmp_path / "input.csv"
    _write_old(data, b"a,b\n1,2\n")
    # A .csv is not a SCRIPT suffix -> no mint, the existing external:path leaf stands.
    scan = detect_used_edges(
        app,
        sess["ws1"].id,
        args={"data_path": str(data)},
        workspace_id="ws1",
        turn_id="t1",
        trace_id="tr1",
    )
    assert len(scan.edges) == 1
    e = scan.edges[0]
    assert e.artifact_id == ""
    assert e.external_ref == f"external:{Path(str(data)).expanduser().resolve()}"
    assert get_registry(app).get("ws1", "input.csv") is None


# --------------------------------------------------------------------------- #
# 3. The SCRIPT node resolves + recurses in lineage (artifact, not gap).
# --------------------------------------------------------------------------- #


def test_script_node_is_first_class_in_lineage(tmp_path):
    app, sess, _arc = _make_app(tmp_path, ["ws1"])
    _write_old(tmp_path / "plot.py", b"# plotting script\n")
    out = tmp_path / "chart.png"
    out.write_bytes(b"\x89PNG")
    minted = mint_tool_declared_outputs(
        app,
        sess["ws1"].id,
        tool_name="run_python",
        effective_args={"output_path": str(out)},
        call_id="gen_chart",
        workspace_id="ws1",
    )
    assert minted
    rec = record_transform(
        app,
        sess["ws1"].id,
        tool_name="run_python",
        args={"script_path": str(tmp_path / "plot.py"), "output_path": str(out)},
        call_id="gen_chart",
        ok=True,
        result=None,
        minted=minted,
        workspace_id="ws1",
    )
    assert rec is not None
    # The transform's instrument pins the script (both fields set).
    assert rec.instrument.script_artifact_id
    assert rec.instrument.script_hash
    script_edge = next(e for e in rec.used if (e.path or "").endswith("plot.py"))
    assert script_edge.artifact_id == rec.instrument.script_artifact_id
    # Lineage: the chart's upstream reaches the SCRIPT node as an ``artifact`` (a
    # TOOL_SCHEMA basis), never a ``gap`` (which would be Mechanism.NONE).
    graph = build_lineage(get_registry(app), minted[0].artifact_id, direction="upstream", depth=5)
    assert graph is not None
    by_id = {n["id"]: n for n in graph["nodes"]}
    script_node = by_id.get(script_edge.artifact_id)
    # Sabotage: mint with Mechanism.NONE -> node type flips to "gap" -> red.
    assert script_node is not None
    assert script_node["type"] == "artifact"
    assert script_node["kind"] == "script"


# --------------------------------------------------------------------------- #
# 4. A second unchanged use dedups (no v2 churn).
# --------------------------------------------------------------------------- #


def test_second_unchanged_use_dedups_no_v2(tmp_path):
    app, sess, _arc = _make_app(tmp_path, ["ws1"])
    script = tmp_path / "stable.py"
    _write_old(script, b"# unchanged\n")
    args = {"script_path": str(script)}
    first = detect_used_edges(
        app, sess["ws1"].id, args=args, workspace_id="ws1", turn_id="t1", trace_id="tr1"
    )
    second = detect_used_edges(
        app, sess["ws1"].id, args=args, workspace_id="ws1", turn_id="t2", trace_id="tr2"
    )
    reg = get_registry(app)
    rec = reg.get("ws1", "stable.py")
    # Same content on the second use -> same-sha dedup: one version, and the SECOND use
    # goes through find_version_by_path -> hash-pair on the SAME id (no fork).
    # Sabotage: mint a fresh v1 each time -> two versions / differing ids -> red.
    assert [v.version for v in rec.versions] == [1]
    assert first.edges[0].artifact_id == second.edges[0].artifact_id
    assert second.edges[0].evidence is EdgeEvidence.HASH_PAIR


# --------------------------------------------------------------------------- #
# 5. Composes with #1038 — a cross-job script reuses the FOREIGN id (no local fork).
# --------------------------------------------------------------------------- #


def test_cross_job_script_reuses_foreign_id_no_local_fork(tmp_path):
    # ws1 and ws2 are separate top-level jobs sharing tmp_path as root (the #1038
    # contributing set). ws1 mints the script first; ws2 consumes it.
    app, sess, _arc = _make_app(tmp_path, ["ws1", "ws2"])
    script = tmp_path / "shared.py"
    _write_old(script, b"# shared script\n")
    first = detect_used_edges(
        app,
        sess["ws1"].id,
        args={"script_path": str(script)},
        workspace_id="ws1",
        turn_id="t1",
        trace_id="tr1",
    )
    foreign_id = first.edges[0].artifact_id
    assert foreign_id
    # ws2 uses the SAME unchanged script, WITH ws1 in its contributing set -> the mint
    # does NOT run (match is not None); the edge reuses the FOREIGN id, cross-bind flagged.
    # Sabotage: mint under the local ws2 anyway -> new local id + a ws2 record -> red.
    second = detect_used_edges(
        app,
        sess["ws2"].id,
        args={"script_path": str(script)},
        workspace_id="ws2",
        turn_id="t2",
        trace_id="tr2",
        allowed_workspace_ids={"ws1", "ws2"},
    )
    assert len(second.edges) == 1
    e = second.edges[0]
    assert e.artifact_id == foreign_id
    assert e.cross_workspace_bind is True
    reg = get_registry(app)
    assert reg.get("ws2", "shared.py") is None
    ws1_rec = reg.get("ws1", "shared.py")
    assert ws1_rec is not None and [v.version for v in ws1_rec.versions] == [1]
