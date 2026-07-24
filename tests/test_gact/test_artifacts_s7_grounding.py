"""S7 answer-grounding, re-sourced from the artifact registry (#973, deletion item 4).

The pre-S7 ``evidence.py`` heuristics (``_ground_fabricated_local_artifact_paths``
+ ``_verified_local_artifact_paths_by_ext`` + ``_is_remote_artifact_ref``) disk-
scanned ``workflow_state.artifact_paths``. Those are DELETED; grounding now
validates/rewrites against the session's REGISTERED artifacts (ids + content
hashes) via :func:`ground_answer_artifacts`.

This module is the **grounding-parity suite**: every recorded corpus the deleted
tests exercised (the six inline EarthScope scenarios + the widget de-domaining
case) is re-expressed with a registry populated to mirror it, and asserts the
registry-sourced grounding delivers the SAME corrective outcome the old heuristic
guaranteed — i.e. registry-sourced ≥ old on the recorded corpora. Plus the new
precision the registry buys (a staged authority-asserted input is never a
substitution candidate) and error-path coverage (remote untouched, on-disk
untouched, ambiguity left alone, child-workspace reach).

Each lock has a sabotage twin in the assertion comments: neutralizing the named
behaviour reddens the assertion, proving it binds the invariant.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from clio_agent.gact.artifacts.grounding import ground_answer_artifacts
from clio_agent.gact.artifacts.minting import mint_artifact, mint_tool_declared_outputs
from clio_agent.gact.artifacts.records import (
    ArtifactKind,
    Custody,
    EvidenceClass,
    IdentityEvidence,
    Mechanism,
)
from clio_agent.gact.artifacts.registry import ArtifactRegistry
from clio_agent.gact.sessions import SessionStore
from clio_agent.gact.workflow_state.schema import WorkflowStateSchema
from tests.test_gact.earthscope_schema import EARTHSCOPE_WORKFLOW_STATE_SCHEMA

# A synthetic non-EarthScope schema (the widget-factory de-domaining corpus): only
# ``svg`` is a declared deliverable extension, so csv/png citations are untouched.
WIDGET_SCHEMA = WorkflowStateSchema(artifact_extensions=("svg",))


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


def _grounding_app(tmp_path: Path, *, roots: dict[str, str] | None = None):
    """A lightweight app with a real registry + session store, for grounding."""
    store = SessionStore(path=tmp_path / "sessions.json")
    state = SimpleNamespace(
        sessions=store,
        arc=_CapturingArc(),
        workspaces=_FakeWorkspaces(roots or {"ws1": str(tmp_path)}),
        semantic_event_sink=object(),
        semantic_trace_detail_level="semantic",
        semantic_trace_backend=None,
        artifact_registry=ArtifactRegistry(),
    )
    return SimpleNamespace(state=state), store


def _register(
    app,
    sid: str,
    *,
    name: str,
    path: str,
    kind: ArtifactKind,
    workspace_id: str = "ws1",
    deliverable: bool = True,
) -> None:
    """Register one PRODUCTION-SHAPED artifact version — a deliverable or a staged input.

    The discriminator is the version's PRODUCER, not a hand-set evidence class (S7
    review). Both classes carry the SAME real ``hashed-at-use`` evidence a small
    on-disk file gets in production (a staged NDP catalog is hashed in-workspace too —
    it is NOT ``authority-asserted``); what differs is the producing tool:

    * ``deliverable`` → a PROCESSING tool's declared output (``pandas_filter_data``,
      designation ``tool-arg``) — a candidate.
    * otherwise → a STAGING tool's result (``ndp_stage_resource`` → ``local_path``,
      designation ``tool-result``) — an input the run consumed, excluded by
      :func:`_is_staging_tool`, regardless of its (identical) evidence class.
    """
    data = Path(path).read_bytes() if Path(path).is_file() else b""
    evidence = IdentityEvidence.hashed_at_use(
        sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data)
    )
    if deliverable:
        producer = {
            "tool": "pandas_filter_data",
            "designation": "tool-arg",
            "arg": "output_file",
            "session_id": sid,
            "call_id": f"call_{name}",
        }
    else:
        producer = {
            "tool": "ndp_stage_resource",
            "designation": "tool-result",
            "result_key": "local_path",
            "session_id": sid,
            "call_id": f"stage_{name}",
        }
    mint_artifact(
        app,
        sid,
        name=name,
        workspace_id=workspace_id,
        evidence=evidence,
        kind=kind,
        mechanism=Mechanism.TOOL_SCHEMA,
        custody=Custody.WORKSPACE_REFERENCED,
        path=path,
        producer=producer,
    )


def _new_session(store: SessionStore, workspace_id: str = "ws1") -> str:
    return store.create(workspace_id=workspace_id, title="t").id


def _seam_deliverable(app, sid: str, path: str, *, tool: str = "pandas_filter_data") -> None:
    """Mint a produced deliverable through the REAL seam (tool-schema, arg channel)."""
    mint_tool_declared_outputs(
        app,
        sid,
        tool_name=tool,
        effective_args={"output_file": path},
        call_id=f"call_{Path(path).name}",
        workspace_id="ws1",
    )


def _seam_staged(app, sid: str, path: str) -> None:
    """Mint a STAGED input through the REAL staging seam (ndp_stage_resource result)."""
    mint_tool_declared_outputs(
        app,
        sid,
        tool_name="ndp_stage_resource",
        effective_args={},
        call_id=f"stage_{Path(path).name}",
        workspace_id="ws1",
        result={"structuredContent": {"local_path": path}},
    )


def _registry_version(app, sid: str, name: str):
    """The head version of the ``ws1`` record named ``name`` (test introspection)."""
    record = app.state.artifact_registry.get("ws1", name)
    return record.head if record is not None else None


# --------------------------------------------------------------------------- #
# Parity: the six recorded EarthScope corpora, registry-sourced.
# --------------------------------------------------------------------------- #


def test_parity_rewrites_fabricated_csv_and_png_to_verified(tmp_path: Path) -> None:
    app, store = _grounding_app(tmp_path)
    sid = _new_session(store)
    real_csv = tmp_path / "ndp-staging" / "P475.CI.LY_.20.csv"
    real_png = tmp_path / "ndp-staging" / "P475.CI.LY_.20_plot.png"
    real_csv.parent.mkdir(parents=True)
    real_csv.write_text("time,east,north,up\n0,0,0,0\n")
    real_png.write_bytes(b"\x89PNG" + b"0" * 64)
    _register(app, sid, name="station.csv", path=str(real_csv), kind=ArtifactKind.DATASET)
    _register(app, sid, name="plot.png", path=str(real_png), kind=ArtifactKind.IMAGE)

    answer = (
        "Staged CSV: /home/x/.clio/artifacts/ndp-staging/P475.CI.LY_.20.csv\n"
        "Plot (PNG): /home/x/.clio/artifacts/plots/P475_CI_LY_timeseries.png\n"
        "Source URL: https://ds2.datacollaboratory.org/raw_csv/P475.CI.LY_.20.csv"
    )
    grounded = ground_answer_artifacts(app, sid, answer, schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA)

    # Fabricated PNG (not on disk) → the single verified PNG. Sabotage: return the
    # answer unchanged and this reddens.
    assert str(real_png) in grounded
    assert "plots/P475_CI_LY_timeseries.png" not in grounded
    # Fabricated CSV → the single verified deliverable CSV.
    assert str(real_csv) in grounded
    # The remote source URL is never a local artifact — untouched.
    assert "https://ds2.datacollaboratory.org/raw_csv/P475.CI.LY_.20.csv" in grounded


def test_parity_staged_authority_input_is_not_a_substitution_candidate(tmp_path: Path) -> None:
    app, store = _grounding_app(tmp_path)
    sid = _new_session(store)
    staging = tmp_path / "ndp-staging"
    staging.mkdir()
    real_csv = staging / "P475.CI.LY_.20.csv"
    real_csv.write_text("time,east,north,up\n0,0,0,0\n")
    catalog = staging / "earthscope_converted_data.csv"
    catalog.write_text("Site,Latitude,Longitude\nP475,32,-117\n")
    # BOTH are hashed-at-use CSVs (the real production shape); they differ ONLY by
    # producer — the deliverable is a pandas output, the catalog is a staging-tool
    # result. The discriminator excludes the catalog by its producer, not its class.
    _register(app, sid, name="station.csv", path=str(real_csv), kind=ArtifactKind.DATASET)
    _register(
        app,
        sid,
        name="catalog.csv",
        path=str(catalog),
        kind=ArtifactKind.DATASET,
        deliverable=False,
    )

    answer = "Staged station CSV: /tmp/SAN_timeseries.csv"
    grounded = ground_answer_artifacts(app, sid, answer, schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA)

    # Exactly ONE deliverable CSV candidate (the catalog excluded by its STAGING-TOOL
    # producer, though it is byte-hashed just like the deliverable), so the fabricated
    # citation grounds to it. Sabotage: count the catalog as a candidate and the set
    # becomes ambiguous → no rewrite → this reddens.
    assert str(real_csv) in grounded
    assert "/tmp/SAN_timeseries.csv" not in grounded
    assert str(catalog) not in grounded


def test_parity_respects_missing_framing(tmp_path: Path) -> None:
    app, store = _grounding_app(tmp_path)
    sid = _new_session(store)
    real_png = tmp_path / "ndp-staging" / "P475.CI.LY_.20_plot.png"
    real_png.parent.mkdir(parents=True)
    real_png.write_bytes(b"\x89PNG" + b"0" * 64)
    _register(app, sid, name="plot.png", path=str(real_png), kind=ArtifactKind.IMAGE)

    answer = "No figure was produced; a PNG has not been staged at /tmp/expected/P475_plot.png yet."
    grounded = ground_answer_artifacts(app, sid, answer, schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA)
    # Honestly-framed missing/expected path: untouched even though a verified PNG
    # exists. Sabotage: drop the framing guard and the path is rewritten → reddens.
    assert grounded == answer


def test_parity_no_verified_neutralizes(tmp_path: Path) -> None:
    app, store = _grounding_app(tmp_path)
    sid = _new_session(store)  # data-blocked run: nothing registered.

    answer = "Plot (PNG): /home/x/.clio/artifacts/plots/SAN_timeseries.png"
    grounded = ground_answer_artifacts(app, sid, answer, schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA)
    # No registered PNG deliverable → neutralize rather than present as real. The
    # schema's declared png extension vocabulary is what still flags the fabricated
    # type on an empty registry (the hybrid design). Sabotage: skip neutralize and
    # ".png" survives → reddens.
    assert "SAN_timeseries.png" not in grounded
    assert ".png" not in grounded
    assert "no local png artifact was produced" in grounded


def test_parity_collapses_doubled_prefix_even_with_multiple_verified(
    tmp_path: Path, monkeypatch
) -> None:
    app, store = _grounding_app(tmp_path)
    sid = _new_session(store)
    # The artifact-token grammar is slash-based and colon-free (a Windows drive
    # colon would split a token), so exercise the embedded-prefix collapse with
    # colon-free relative paths anchored at the cwd — the same code path a POSIX
    # duplicated-prefix path hits.
    monkeypatch.chdir(tmp_path)
    staging = tmp_path / "ndp-staging"
    staging.mkdir()
    (staging / "P473.PW.LY_.00.csv").write_text("time,east,north,up\n0,0,0,0\n")
    (staging / "P999.PW.LY_.00.csv").write_text("time,east,north,up\n1,1,1,1\n")
    real_rel = "ndp-staging/P473.PW.LY_.00.csv"
    # TWO verified CSV deliverables → normally ambiguous, but the doubled token
    # embeds exactly one, so the embedded-collapse wins before the ambiguity check.
    _register(app, sid, name="a.csv", path=real_rel, kind=ArtifactKind.DATASET)
    _register(
        app, sid, name="b.csv", path="ndp-staging/P999.PW.LY_.00.csv", kind=ArtifactKind.DATASET
    )
    doubled = f"artifacts/ndp-{real_rel}"  # a real path with a duplicated prefix

    grounded = ground_answer_artifacts(
        app, sid, f"Staged CSV: {doubled}.", schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA
    )
    assert real_rel in grounded
    assert doubled not in grounded


def test_parity_keeps_honest_blocked_prose(tmp_path: Path) -> None:
    app, store = _grounding_app(tmp_path)
    sid = _new_session(store)
    answer = (
        "No PNG was produced because staging was blocked; a figure would be "
        "written to /tmp/expected/figure.png once a station CSV is staged."
    )
    grounded = ground_answer_artifacts(app, sid, answer, schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA)
    assert grounded == answer


# --------------------------------------------------------------------------- #
# De-domaining parity + precision / error paths.
# --------------------------------------------------------------------------- #


def test_parity_widget_schema_grounds_svg_and_leaves_csv_png_untouched(tmp_path: Path) -> None:
    app, store = _grounding_app(tmp_path)
    sid = _new_session(store)
    real_svg = tmp_path / "widget.svg"
    real_svg.write_text("<svg/>")
    _register(app, sid, name="widget.svg", path=str(real_svg), kind=ArtifactKind.IMAGE)

    answer = "Rendered: /out/fabricated_widget.svg\nData: /out/table.csv\nChart: /out/chart.png"
    grounded = ground_answer_artifacts(app, sid, answer, schema=WIDGET_SCHEMA)
    # Only the declared svg type is grounded; csv/png are outside this pack's
    # deliverable vocabulary and left verbatim (domain-agnostic).
    assert str(real_svg) in grounded
    assert "/out/fabricated_widget.svg" not in grounded
    assert "/out/table.csv" in grounded
    assert "/out/chart.png" in grounded


def test_generic_schema_grounds_nothing(tmp_path: Path) -> None:
    app, store = _grounding_app(tmp_path)
    sid = _new_session(store)
    real = tmp_path / "x.png"
    real.write_bytes(b"\x89PNG")
    _register(app, sid, name="x.png", path=str(real), kind=ArtifactKind.IMAGE)
    answer = "Plot: /tmp/fabricated.png"
    # A schema declaring no deliverable extensions is a no-op (parity with the old
    # GENERIC_WORKFLOW_STATE_SCHEMA default).
    assert ground_answer_artifacts(app, sid, answer, schema=WorkflowStateSchema()) == answer


def test_on_disk_citation_is_left_unchanged(tmp_path: Path) -> None:
    app, store = _grounding_app(tmp_path)
    sid = _new_session(store)
    real = tmp_path / "real.png"
    real.write_bytes(b"\x89PNG")
    real_s = real.as_posix()
    _register(app, sid, name="real.png", path=real_s, kind=ArtifactKind.IMAGE)
    answer = f"Plot: {real_s}"
    # The cited path exists on disk → never rewritten (it is a real artifact).
    assert (
        ground_answer_artifacts(app, sid, answer, schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA) == answer
    )


def test_ambiguous_multiple_deliverables_left_unchanged(tmp_path: Path) -> None:
    app, store = _grounding_app(tmp_path)
    sid = _new_session(store)
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(b"\x89PNGa")
    b.write_bytes(b"\x89PNGb")
    _register(app, sid, name="a.png", path=str(a), kind=ArtifactKind.IMAGE)
    _register(app, sid, name="b.png", path=str(b), kind=ArtifactKind.IMAGE)
    answer = "Plot: /tmp/fabricated.png"
    # Two verified PNGs, no embedding → ambiguous which was meant → unchanged
    # (precision over recall; false attribution is worse than none).
    assert (
        ground_answer_artifacts(app, sid, answer, schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA) == answer
    )


def test_unknown_session_grounds_nothing(tmp_path: Path) -> None:
    app, _store = _grounding_app(tmp_path)
    answer = "Plot: /tmp/fabricated.png"
    # No such session → no workspace → no candidates → answer returned unchanged
    # (never a crash on an unbound session).
    assert (
        ground_answer_artifacts(
            app, "sess_missing", answer, schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA
        )
        == answer
    )


class _FakeTask:
    def __init__(self, child_session_id: str) -> None:
        self.child_session_id = child_session_id


class _FakeTaskRegistry:
    def __init__(self, by_parent: dict[str, list[str]]) -> None:
        self._by_parent = by_parent

    def for_parent(self, parent: str) -> list[_FakeTask]:
        return [_FakeTask(c) for c in self._by_parent.get(parent, [])]


def test_include_children_grounds_against_delegate_output(tmp_path: Path) -> None:
    parent_root = tmp_path / "parent"
    child_root = tmp_path / "child"
    parent_root.mkdir()
    child_root.mkdir()
    app, store = _grounding_app(
        tmp_path, roots={"ws_parent": str(parent_root), "ws_child": str(child_root)}
    )
    parent_sid = _new_session(store, workspace_id="ws_parent")
    child_sid = _new_session(store, workspace_id="ws_child")
    app.state.agent_task_registry = _FakeTaskRegistry({parent_sid: [child_sid]})

    child_png = child_root / "delegate_plot.png"
    child_png.write_bytes(b"\x89PNG" + b"0" * 32)
    _register(
        app,
        child_sid,
        name="delegate_plot.png",
        path=str(child_png),
        kind=ArtifactKind.IMAGE,
        workspace_id="ws_child",
    )

    answer = "The delegate produced /tmp/fabricated_delegate.png"
    grounded = ground_answer_artifacts(
        app, parent_sid, answer, schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA
    )
    # The parent grounds against the CHILD workspace's registered deliverable via the
    # include_children union. Sabotage: pass include_children=False and this reddens.
    assert str(child_png) in grounded
    assert "/tmp/fabricated_delegate.png" not in grounded

    # With the reach disabled the parent sees no candidate → neutralized.
    off = ground_answer_artifacts(
        app, parent_sid, answer, schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA, include_children=False
    )
    assert "no local png artifact was produced" in off


# --------------------------------------------------------------------------- #
# The producer discriminator on PRODUCTION-SHAPED records (S7 review).
# --------------------------------------------------------------------------- #


def test_stat_pinned_over_ceiling_deliverable_is_a_candidate(tmp_path: Path, monkeypatch) -> None:
    """A produced deliverable over the hash ceiling is stat-pinned — and STILL a candidate.

    Finding [1]: keying the candidate set on ``evidence_class == hashed-at-use``
    EXCLUDED a genuine >64MB produced deliverable (production mints it ``stat-pinned``),
    so the server then falsely authored "no artifact was produced" over a real
    deliverable. Minted here through the REAL tool-schema seam with the hash ceiling
    lowered so a small file stat-pins exactly as a >64MB one does in production.
    """
    monkeypatch.setenv("CLIO_ARTIFACTS_HASH_MAX_FILE_BYTES", "4")
    app, store = _grounding_app(tmp_path)
    sid = _new_session(store)
    big = tmp_path / "huge_series.csv"
    big.write_text("time,east\n0,1\n1,2\n")  # > 4 bytes → stat-pinned via the seam
    _seam_deliverable(app, sid, str(big))

    version = _registry_version(app, sid, "huge_series.csv")
    # Production shape, not hand-set: the seam really stat-pinned it (no sha256).
    assert version is not None
    assert version.evidence.evidence_class is EvidenceClass.STAT_PINNED
    assert version.sha256 is None

    answer = "Filtered CSV: /tmp/SAN_over_ceiling.csv"
    grounded = ground_answer_artifacts(app, sid, answer, schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA)
    # The stat-pinned deliverable IS a substitution candidate. Sabotage: gate the
    # candidate set on hashed-at-use again and this reddens (fabricated survives, or
    # is neutralized as "no artifact produced").
    assert str(big) in grounded
    assert "/tmp/SAN_over_ceiling.csv" not in grounded
    assert "no local csv artifact was produced" not in grounded


def test_seam_minted_staged_catalog_is_excluded(tmp_path: Path) -> None:
    """Discriminator on REAL seam shapes: a staged catalog + a deliverable, same ext.

    Both minted through the real seams — the deliverable via the pandas tool-schema
    arg channel, the catalog via ``ndp_stage_resource``'s ``local_path`` result. Both
    are hashed-at-use; only the producer distinguishes them, so the fabricated CSV
    grounds to the single deliverable, never the staged catalog.
    """
    app, store = _grounding_app(tmp_path)
    sid = _new_session(store)
    deliverable = tmp_path / "clean.csv"
    deliverable.write_text("time,east\n0,1\n")
    catalog = tmp_path / "earthscope_converted_data.csv"
    catalog.write_text("Site,Latitude,Longitude\nP475,32,-117\n")
    _seam_deliverable(app, sid, str(deliverable))
    _seam_staged(app, sid, str(catalog))

    # Both were minted hashed-at-use through the real seams (no hand-set class).
    assert _registry_version(app, sid, "clean.csv").sha256 is not None
    assert _registry_version(app, sid, "earthscope_converted_data.csv").sha256 is not None

    answer = "Filtered station CSV: /tmp/SAN_timeseries.csv"
    grounded = ground_answer_artifacts(app, sid, answer, schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA)
    assert str(deliverable) in grounded
    assert str(catalog) not in grounded
    assert "/tmp/SAN_timeseries.csv" not in grounded


def test_produced_deliverable_off_disk_is_not_falsely_neutralized(tmp_path: Path) -> None:
    """A produced deliverable whose bytes are gone is NEVER declared "not produced".

    Finding [1] guard: when a deliverable of the cited ext WAS produced (registered)
    but its file no longer resolves on disk (a scratch file cleaned up), the citation
    is left unchanged — the server must not author the affirmatively-false "no local
    csv artifact was produced" note over a deliverable that genuinely existed.
    """
    app, store = _grounding_app(tmp_path)
    sid = _new_session(store)
    gone = tmp_path / "produced_then_deleted.csv"
    gone.write_text("time,east\n0,1\n")
    _seam_deliverable(app, sid, str(gone))
    gone.unlink()  # the deliverable was produced, but its bytes are now gone

    answer = "Filtered CSV: /tmp/SAN_missing.csv"
    grounded = ground_answer_artifacts(app, sid, answer, schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA)
    # Ambiguity (produced but off-disk) → unchanged, NEVER the false not-produced note.
    # Sabotage: drop the produced_deliverable_extensions guard and this reddens.
    assert "no local csv artifact was produced" not in grounded
    assert "/tmp/SAN_missing.csv" in grounded


def test_token_re_keeps_windows_drive_spec() -> None:
    """A drive-lettered citation tokenizes WHOLE — the C:C:/ doubling root cause.

    Without the optional ``[A-Za-z]:`` head the tokenizer strips the drive, a real
    on-disk path fails ``is_file()`` and the substitution doubles the prefix.
    """
    from clio_agent.gact.artifacts.grounding import _artifact_path_token_re

    token_re = _artifact_path_token_re(("png",))
    match = token_re.search("Plot: C:/Users/jaime/real.png done")
    assert match is not None
    assert match.group(0) == "C:/Users/jaime/real.png"
    # POSIX citations are unchanged by the drive head.
    match = token_re.search("Plot: /home/user/real.png done")
    assert match is not None
    assert match.group(0) == "/home/user/real.png"


def test_tail_of_verified_token_is_never_substituted(tmp_path: Path) -> None:
    """A token that is the TAIL of a verified path stays untouched (inverse-mangle guard).

    Substituting the full verified path into a tail token that sits inside prose
    would double the prefix — the same C:C:/ defect shape for every prefix-trim
    (e.g. a model citing the path without its mount prefix).
    """
    app, store = _grounding_app(tmp_path)
    sid = _new_session(store)
    real = tmp_path / "real.png"
    real.write_bytes(b"PNGBYTES")
    real_s = real.as_posix()
    _register(app, sid, name="real.png", path=real_s, kind=ArtifactKind.IMAGE)
    # A backslash-cited real path: the tokenizer's char class breaks on ``\\``, so
    # the token collapses to the bare tail ``real.png``. Substituting the verified
    # full path into that tail would mangle the citation (the prefix survives in
    # the prose) — the tail-of-verified guard must leave it untouched.
    answer = "Saved at " + real_s.replace("/", "\\\\") + " in the workspace."
    grounded = ground_answer_artifacts(app, sid, answer, schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA)
    assert grounded == answer
    assert real_s + real_s not in grounded
