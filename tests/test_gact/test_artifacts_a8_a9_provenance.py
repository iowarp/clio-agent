"""Regression tests for A8 (empty producer) and A9 (dedup enrichment), #1176.

A8: the used-script designate-on-use mint seam (``transform_edges.detect_used_edges``,
P3.2 #1039) minted a consumed ``.py``/``.sh`` with NO ``producer`` kwarg at all —
``mint_artifact_outcome`` defaulted it to ``{}``. The session-scoped artifacts route
(``GET /v1/sessions/{sid}/artifacts``) joins strictly on ``producer.session_id``, so a
version with an empty producer NEVER surfaces there regardless of ``include_children``
— exactly the live-UI symptom (topbar count reads 0 for a workspace-referenced record
whose custody/kind matches a designate-on-use script mint). Fixed in
``proposal_effects._mint_producer`` (parity shape for the ordinary create_artifact
mint) and, for the actually-empty seam, ``transform_edges.detect_used_edges`` now
stamps the consuming call's session/tool/call_id.

A9: a ``create_artifact`` call that hits ``already_registered`` (W&B same-sha dedup)
returned the EXISTING immutable version untouched — the caller's own declared
``annotation`` (description) was silently dropped, never visible anywhere. Fixed via
the dedup-enrichment side index (``artifacts/dedup_enrichment.py`` +
``ArtifactRegistry._supplemental_annotations``), merged in at the route wire boundary
(``routes/artifacts.py::_version_wire``) wherever the version's own ``annotation`` is
blank. The declared ``used=[...]`` input-ref channel (#1191) was ALREADY unconditional
on ``created`` vs dedup (``transforms.record_transform`` always resolves it from the
call's own args) — this suite pins that it keeps working across a dedup too.

Each key lock carries a sabotage note: the referenced neutralization turns the named
assertion red, proving the test binds the invariant (not a tautology).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


def _client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


def _workspace_session(c: TestClient, root: Path) -> tuple[str, str]:
    wid = c.post("/v1/workspaces", json={"name": "w", "root_path": str(root)}).json()["id"]
    sid = c.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]
    return wid, sid


# --------------------------------------------------------------------------- #
# A8 — used-script designate-on-use producer parity + route visibility
# --------------------------------------------------------------------------- #


def test_used_script_designate_on_use_carries_full_producer_and_route_serves_it(
    tmp_path: Path,
):
    """A consumed ``.py`` referenced by a tool call (never separately registered via
    ``create_artifact``) is auto-designated ``kind=script`` (P3.2 #1039). Before the
    fix its producer was a bare ``{}`` — no session/tool/call_id/designation — so
    ``GET /v1/sessions/{sid}/artifacts`` never served it (the route's ONLY join key
    is ``producer.session_id``). The fix stamps the consuming call's identity.

    Sabotage: drop the ``producer={...}`` kwarg from the ``mint_artifact_outcome``
    call inside ``transform_edges.detect_used_edges`` (back to the bare call with no
    ``producer`` at all) -> the version's producer reverts to ``{}`` -> BOTH the
    producer-shape assertions AND the route-count assertion below go red.
    """
    c = _client(tmp_path)
    wid, sid = _workspace_session(c, tmp_path)

    # Pre-existing script (mtime predates the observed call -> a USED input, not a
    # freshly-written output).
    script = tmp_path / "helper.py"
    script.write_text("print('hi')\n", encoding="utf-8")

    from clio_agent.gact.artifacts.transforms import observe_tool_transform

    observe_tool_transform(
        c.app,
        sid,
        "execute_shell",
        {"command": "python helper.py", "script": str(script)},
        "call_shell_1",
        True,
        {"stdout": "hi\n", "exit_code": 0},
    )

    # -- storage-level sanity (not the acceptance bar by itself) --
    from clio_agent.gact.artifacts.registry import get_registry

    record = get_registry(c.app).get(wid, "helper.py")
    assert record is not None and record.head is not None
    producer = record.head.producer
    assert producer.get("session_id") == sid
    assert producer.get("tool") == "execute_shell"
    assert producer.get("call_id") == "call_shell_1"
    assert producer.get("designation") == "used-script"

    # -- the ACTUAL acceptance bar: what the route SERVES --
    slist = c.get(f"/v1/sessions/{sid}/artifacts").json()
    assert slist["count"] == 1, slist
    assert slist["artifacts"][0]["name"] == "helper.py"

    slist_children = c.get(
        f"/v1/sessions/{sid}/artifacts", params={"include_children": True}
    ).json()
    assert slist_children["count"] == 1, slist_children
    assert slist_children["artifacts"][0]["producing_session_ids"] == [sid]


def test_ordinary_create_artifact_mint_producer_is_unaffected(tmp_path: Path):
    """Regression pin: an ORDINARY create_artifact fresh mint (no used-script in
    play) already carried a full producer before this campaign and must still
    resolve at the route -- the A8 fix must not perturb the healthy path."""
    c = _client(tmp_path)
    wid, sid = _workspace_session(c, tmp_path)

    from clio_agent.gact.artifacts.proposals import parse_proposals, promote_proposals

    result = promote_proposals(
        c.app,
        sid,
        parse_proposals(
            name="report.md",
            kind="report",
            path="",
            content="# hello\n",
            annotation="",
            artifacts=None,
        ),
        workspace_id=wid,
        turn_id="t1",
        trace_id="tr1",
        agent_id="main",
    )
    assert result["created"] == 1, result

    slist = c.get(f"/v1/sessions/{sid}/artifacts").json()
    assert slist["count"] == 1
    version = slist["artifacts"][0]["versions"][0]
    assert version["producer"]["session_id"] == sid
    assert version["producer"]["tool"] == "create_artifact"
    assert version["producer"]["designation"] == "agent-proposed"


# --------------------------------------------------------------------------- #
# A9 — dedup enrichment (description + declared used refs) merges onto the
# existing record instead of being silently discarded.
# --------------------------------------------------------------------------- #


def test_create_artifact_dedup_merges_description_at_the_route(tmp_path: Path):
    """Seam-mint a file (tool-schema producer, no annotation), then call
    create_artifact on the SAME bytes with a description. The dedup ("already
    registered") must not discard the caller's declared description -- it merges
    onto the existing version's wire projection.

    Sabotage: remove the ``_dedup_enrich`` call from ``promote_proposal``'s dedup
    branches (or drop the ``registry`` merge in ``routes/artifacts._version_wire``)
    -> ``resolved["annotation"]`` stays ``""`` -> this assertion goes red.
    """
    c = _client(tmp_path)
    wid, sid = _workspace_session(c, tmp_path)

    data_path = tmp_path / "results.csv"
    data_path.write_bytes(b"a,b\n1,2\n")

    from clio_agent.gact.artifacts.minting import mint_tool_declared_outputs

    seam_minted = mint_tool_declared_outputs(
        c.app,
        sid,
        tool_name="run_analysis",
        effective_args={"output_path": str(data_path)},
        call_id="call_seam_1",
        workspace_id=wid,
    )
    assert len(seam_minted) == 1
    artifact_id = seam_minted[0].artifact_id
    assert seam_minted[0].annotation == ""

    # The route confirms the seam mint itself starts with no description.
    before = c.get(f"/v1/artifacts/{artifact_id}").json()
    assert before["resolved"]["annotation"] == ""

    from clio_agent.gact.artifacts.proposals import parse_proposals, promote_proposals

    result = promote_proposals(
        c.app,
        sid,
        parse_proposals(
            name="",
            kind="dataset",
            path=str(data_path),
            content="",
            annotation="the analysis results",
            artifacts=None,
        ),
        workspace_id=wid,
        turn_id="t2",
        trace_id="tr2",
        agent_id="main",
    )
    assert result["created"] == 0
    assert result["deduplicated"] == 1, result
    assert result["artifacts"][0]["reason"] == "already_registered"
    # Sabotage: drop the ``enrichment`` field from ProposalOutcome.to_wire ->
    # this key vanishes from the model-facing tool result too.
    assert result["artifacts"][0]["enrichment"] == "merged"

    # -- the ACTUAL acceptance bar: what the route SERVES for the SAME artifact_id --
    after = c.get(f"/v1/artifacts/{artifact_id}").json()
    assert after["resolved"]["artifact_id"] == artifact_id
    assert after["resolved"]["annotation"] == "the analysis results"

    # And via the session-scoped listing too (a different projection path).
    slist = c.get(f"/v1/sessions/{sid}/artifacts").json()
    assert slist["count"] == 1
    assert slist["artifacts"][0]["versions"][0]["annotation"] == "the analysis results"


def test_create_artifact_dedup_first_caller_wins_never_overwrites(tmp_path: Path):
    """The version's OWN annotation, once present (from either the original mint
    or an earlier supplemental merge), is never silently overwritten by a later
    dedup's differing description -- first-caller-wins, typed."""
    c = _client(tmp_path)
    wid, sid = _workspace_session(c, tmp_path)

    from clio_agent.gact.artifacts.proposals import parse_proposals, promote_proposals

    data_path = tmp_path / "notes.md"
    result1 = promote_proposals(
        c.app,
        sid,
        parse_proposals(
            name="notes.md",
            kind="report",
            path="",
            content="# notes\n",
            annotation="original description",
            artifacts=None,
        ),
        workspace_id=wid,
        turn_id="t1",
        trace_id="tr1",
        agent_id="main",
    )
    assert result1["created"] == 1
    artifact_id = result1["artifacts"][0]["artifact_id"]

    result2 = promote_proposals(
        c.app,
        sid,
        parse_proposals(
            name="",
            kind="report",
            path=str(data_path),
            content="",
            annotation="a DIFFERENT description",
            artifacts=None,
        ),
        workspace_id=wid,
        turn_id="t2",
        trace_id="tr2",
        agent_id="main",
    )
    assert result2["deduplicated"] == 1
    # Sabotage: drop the ``version.annotation or artifact_id in
    # registry._supplemental_annotations`` guard in ``decide_enrichment`` -> this
    # typed reason turns into a silent overwrite instead.
    assert result2["artifacts"][0]["enrichment"] == "annotation_already_present"

    after = c.get(f"/v1/artifacts/{artifact_id}").json()
    assert after["resolved"]["annotation"] == "original description"


def test_create_artifact_declared_used_refs_survive_a_dedup(tmp_path: Path):
    """The #1191 declared ``used=[...]`` channel is unconditional on created vs
    dedup -- pin that it keeps recording real PROV edges across a same-sha
    dedup, so a dedup call's own declared inputs are not silently discarded
    either (A9's second half, alongside the description merge above)."""
    c = _client(tmp_path)
    wid, sid = _workspace_session(c, tmp_path)

    from clio_agent.gact.artifacts.minting import mint_tool_declared_outputs
    from clio_agent.gact.artifacts.proposals import parse_proposals, promote_proposals
    from clio_agent.gact.artifacts.transforms import observe_tool_transform

    input_path = tmp_path / "input.csv"
    input_path.write_bytes(b"x,y\n1,2\n")
    input_minted = mint_tool_declared_outputs(
        c.app,
        sid,
        tool_name="producer",
        effective_args={"output_path": str(input_path)},
        call_id="call_in",
        workspace_id=wid,
    )
    assert len(input_minted) == 1
    input_id = input_minted[0].artifact_id

    report_path = tmp_path / "report.md"
    report_path.write_text("# report\n", encoding="utf-8")
    seam_minted = mint_tool_declared_outputs(
        c.app,
        sid,
        tool_name="run_report",
        effective_args={"output_path": str(report_path)},
        call_id="call_seam",
        workspace_id=wid,
    )
    assert len(seam_minted) == 1
    report_id = seam_minted[0].artifact_id

    # create_artifact deduping onto the report's existing bytes, declaring the
    # input.csv as a used ref.
    call_args = {
        "name": "",
        "kind": "report",
        "path": str(report_path),
        "content": "",
        "annotation": "",
        "artifacts": None,
        "used": [str(input_path)],
    }
    result = promote_proposals(
        c.app,
        sid,
        parse_proposals(
            name="", kind="report", path=str(report_path), content="", annotation="", artifacts=None
        ),
        workspace_id=wid,
        turn_id="t2",
        trace_id="tr2",
        agent_id="main",
    )
    assert result["deduplicated"] == 1
    observe_tool_transform(c.app, sid, "create_artifact", call_args, "call_dedup", True, result)

    from clio_agent.gact.artifacts.registry import get_registry

    rec = get_registry(c.app).get_transform("call_dedup")
    assert rec is not None
    # Sabotage: gate detect_declared_used_edges on ``created`` mints only (an
    # "optimization" that treats a dedup as a no-op) -> this list goes empty.
    declared_used = {e.artifact_id for e in rec.used if e.arg == "used"}
    assert declared_used == {input_id}

    # And at the route: the lineage graph rooted at the deduped-onto report
    # carries the declared input upstream.
    lineage = c.get(f"/v1/artifacts/{report_id}/lineage", params={"direction": "upstream"}).json()
    node_ids = {n["id"] for n in lineage["nodes"]}
    assert input_id in node_ids
