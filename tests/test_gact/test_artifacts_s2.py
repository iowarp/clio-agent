"""Tests for the S2 artifacts wire slice (#966/#968).

Covers the four wire surfaces: the ``artifact.*`` SSE event family + detail-level
redaction, the ``resource_link`` part projection (uri format, metadata
completeness, ui_payload variant, additive-only Part), the HTTP routes (list /
get-by-id / by-name+ref / bytes with the two typed 409s / user-pin), and the
``artifact.proposed`` byte-parity contract.

Each key lock carries a sabotage note: the referenced neutralization turns the
named assertion red, proving the test binds the invariant (not a tautology).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.records import (
    ArtifactKind,
    ArtifactVersion,
    Custody,
    IdentityEvidence,
    Mechanism,
)
from clio_agent.gact.artifacts.wire import (
    ARTIFACT_SERVER_ID,
    UI_PAYLOAD_MIME,
    resource_link_part,
)
from clio_agent.gact.semantic_events import (
    SSE_UI_EVENT_TYPES,
    SemanticEvent,
    event_reaches_ui,
    project_full,
    project_sse,
)
from clio_agent.gact.types import Part

# --------------------------------------------------------------------------- #
# 1. Event family: SSE allow-list + detail-level redaction
# --------------------------------------------------------------------------- #


def test_artifact_created_family_is_on_the_sse_allow_list():
    # Sabotage: drop the three names from SSE_UI_EVENT_TYPES -> these go red (the
    # served-family lock). The trace-only pair must stay OFF the wire.
    assert "artifact.created" in SSE_UI_EVENT_TYPES
    assert "artifact.version.added" in SSE_UI_EVENT_TYPES
    assert "artifact.alias.moved" in SSE_UI_EVENT_TYPES
    assert event_reaches_ui("artifact.created") is True
    assert event_reaches_ui("artifact.version.added") is True
    assert event_reaches_ui("artifact.alias.moved") is True


def test_artifact_used_and_transform_stay_trace_only():
    # Sabotage: add "artifact.used" to SSE_UI_EVENT_TYPES -> these go red (the
    # trace-only lock — provenance substrate must never reach the served wire).
    assert "artifact.used" not in SSE_UI_EVENT_TYPES
    assert "artifact.transform.recorded" not in SSE_UI_EVENT_TYPES
    assert event_reaches_ui("artifact.used") is False
    assert event_reaches_ui("artifact.transform.recorded") is False


def test_artifact_created_sse_projection_keeps_record_no_credentials():
    """project_sse at ``semantic`` keeps the full artifact record (no secrets in it)."""
    payload = {
        "artifact_id": "artifact_deadbeef",
        "sha256": "a" * 64,
        "kind": "image",
        "version": 1,
        "producer": {"call_id": "c1", "tool": "plot"},
        "path": "/ws/plot.png",
    }
    event = SemanticEvent(
        event_type="artifact.created",
        session_id="s1",
        trace_id="t1",
        payload=payload,
        detail_level="semantic",
    )
    sse = project_sse(event)
    full = project_full(event)
    # Sabotage: force detail_level="off" at emit -> project_sse empties payload ->
    # the sha/kind assertions go red (the redaction-path lock: content survives,
    # only genuine credentials are masked, of which a record has none).
    assert sse["payload"]["sha256"] == "a" * 64
    assert sse["payload"]["kind"] == "image"
    assert sse["payload"]["producer"]["call_id"] == "c1"
    # The trace keeps the identical full payload.
    assert full["payload"] == payload


class _CapturingArc:
    def __init__(self) -> None:
        self.events: list = []

    def record_semantic_event(self, event):
        self.events.append(event)
        return event


class _FakeWorkspaces:
    def __init__(self, roots: dict) -> None:
        self._roots = roots

    def get(self, wid):
        from types import SimpleNamespace

        root = self._roots.get(wid)
        return SimpleNamespace(root_path=root) if root else None


def _fake_mint_app(tmp_path: Path):
    from types import SimpleNamespace

    from clio_agent.gact.sessions import SessionStore

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


def test_mint_emits_artifact_created_at_semantic_detail(tmp_path: Path):
    """The mint funnel rewires off trace-only: the emitted event rides SSE detail."""
    from clio_agent.gact.artifacts.minting import mint_tool_declared_outputs

    app, sess, arc = _fake_mint_app(tmp_path)
    png = tmp_path / "chart.png"
    png.write_bytes(b"\x89PNG\r\n")
    mint_tool_declared_outputs(
        app,
        sess.id,
        tool_name="plot",
        effective_args={"output_path": str(png)},
        call_id="c1",
        workspace_id="ws1",
    )
    events = [e for e in arc.events if getattr(e, "event_type", "") == "artifact.created"]
    assert len(events) == 1
    # Sabotage: revert the emit to detail_level="off" in mint_artifact -> this goes
    # red (the rewire lock: S1 was trace-only, S2 must ride the SSE detail lane).
    assert events[0].detail_level == "semantic"
    assert event_reaches_ui(events[0].event_type) is True


# --------------------------------------------------------------------------- #
# 2. Parts: resource_link projection + additive-only Part
# --------------------------------------------------------------------------- #


def _version(kind: ArtifactKind = ArtifactKind.IMAGE, **kw) -> ArtifactVersion:
    ev = IdentityEvidence.hashed_at_use(sha256="b" * 64, size_bytes=6)
    return ArtifactVersion(
        version=kw.pop("version", 2),
        kind=kind,
        custody=kw.pop("custody", Custody.WORKSPACE_REFERENCED),
        mechanism=Mechanism.TOOL_SCHEMA,
        evidence=ev,
        producer=kw.pop("producer", {"call_id": "call_9"}),
        path=kw.pop("path", "/ws/plot.png"),
    )


def test_resource_link_uri_and_metadata_completeness():
    v = _version()
    part = resource_link_part("ws1", "plot.png", v, part_id="p1", agent_id="main")
    wire = part.to_wire()
    # Sabotage: change the uri format in wire.artifact_uri -> this goes red (the
    # uri-contract lock: artifact://<ws>/<name>@vN).
    assert wire["uri"] == "artifact://ws1/plot.png@v2"
    assert wire["server_id"] == ARTIFACT_SERVER_ID
    assert wire["name"] == "plot.png"
    md = wire["metadata"]
    # Sabotage: drop any key from resource_link_metadata -> this set-equality goes
    # red (the metadata-completeness lock — owner decision #966.9's exact keys).
    assert set(md) >= {
        "artifact_id",
        "sha256",
        "size_bytes",
        "kind",
        "version",
        "custody",
        "fetch_url",
        "producer_activity_id",
        "mechanism",
    }
    assert md["fetch_url"] == f"/v1/artifacts/{v.artifact_id}/bytes"
    assert md["producer_activity_id"] == "call_9"
    assert md["mechanism"] == "tool-schema"


def test_resource_link_ui_payload_variant():
    v = _version(kind=ArtifactKind.UI_PAYLOAD, path="/ws/app.html")
    part = resource_link_part("ws1", "app.html", v, part_id="p1")
    wire = part.to_wire()
    # Sabotage: route ui_payload through artifact:// in resource_link_part -> the
    # ui:// scheme + mcp-app mime assertions go red (the ui_payload lock).
    assert wire["uri"] == "ui://ws1/app.html@v2"
    assert wire["mime_type"] == UI_PAYLOAD_MIME


def test_part_new_fields_are_additive_omitempty():
    """Existing part types must serialize byte-identically — new fields drop by default."""
    text = Part(id="t1", type="text", text="hello").to_wire()
    diff = Part(
        id="d1", type="file_diff", path="a.py", unified_diff="@@", status="pending"
    ).to_wire()
    # Sabotage: give uri/name/server_id a non-empty default -> these go red (the
    # additive-only lock: no existing wire fixture may gain a key).
    for wire in (text, diff):
        assert "uri" not in wire
        assert "name" not in wire
        assert "server_id" not in wire
    # And the resource_link part DOES carry them (proves the fields exist + serialize).
    rl = resource_link_part("ws1", "x.png", _version(), part_id="r1").to_wire()
    assert {"uri", "name", "server_id"} <= set(rl)


# --------------------------------------------------------------------------- #
# 3. artifact.proposed byte-parity contract (deletion rides, payload unchanged)
# --------------------------------------------------------------------------- #


def test_artifact_proposed_is_not_on_the_sse_wire_and_payload_shape_is_pinned():
    # The proposal stage keeps its SPEC payload byte-identical: it is NOT widened
    # onto the SSE family wire, and its payload keys are exactly the file_diff set.
    # Sabotage: add "artifact.proposed" to SSE_UI_EVENT_TYPES -> the first assert
    # goes red (the byte-parity lock: proposal stage unchanged).
    assert "artifact.proposed" not in SSE_UI_EVENT_TYPES
    assert event_reaches_ui("artifact.proposed") is False
    # The finalize emit payload shape (§7.3a) — pinned so a widening never drifts it.
    expected_keys = {
        "path",
        "unified_diff",
        "new_content",
        "edit_mode",
        "lines_added",
        "lines_removed",
    }
    src = Path("src/clio_agent/gact/turn_finalize.py").read_text(encoding="utf-8")
    assert '"artifact.proposed"' in src
    for key in expected_keys:
        assert f'"{key}"' in src


# --------------------------------------------------------------------------- #
# 4. Routes (memory-route pattern): list / get / by-name / bytes / pin
# --------------------------------------------------------------------------- #


def _client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


def _workspace_session(c: TestClient, root: Path) -> tuple[str, str]:
    wid = c.post("/v1/workspaces", json={"name": "w", "root_path": str(root)}).json()["id"]
    sid = c.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]
    return wid, sid


def test_capability_flag_advertised(tmp_path: Path):
    c = _client(tmp_path)
    caps = c.get("/v1/capabilities").json()["capabilities"]
    # Sabotage: drop x_clio_artifacts=True in system.py -> this goes red.
    assert caps["x_clio_artifacts"] is True


def test_pin_then_list_get_and_by_name(tmp_path: Path):
    c = _client(tmp_path)
    wid, sid = _workspace_session(c, tmp_path)
    f = tmp_path / "report.md"
    f.write_text("# results\n", encoding="utf-8")

    pin = c.post(f"/v1/sessions/{sid}/artifacts/pin", json={"path": "report.md"})
    assert pin.status_code == 200, pin.text
    version = pin.json()["pinned"]
    aid = version["artifact_id"]
    # Sabotage: set mechanism to TOOL_SCHEMA in _pin_mint -> this goes red (the
    # user-pin channel lock: harness mechanism + user-pinned designation).
    assert version["mechanism"] == "harness"
    assert version["producer"]["designation"] == "user-pinned"
    assert version["uri"] == "artifact://%s/report.md@v1" % wid

    # session listing sees it
    slist = c.get(f"/v1/sessions/{sid}/artifacts").json()
    assert slist["count"] == 1
    assert slist["artifacts"][0]["name"] == "report.md"
    # workspace listing sees it
    wlist = c.get(f"/v1/workspaces/{wid}/artifacts").json()
    assert wlist["count"] == 1
    # get by artifact_id
    got = c.get(f"/v1/artifacts/{aid}").json()
    assert got["resolved"]["artifact_id"] == aid
    # by-name ref=latest and ref=v1
    latest = c.get(f"/v1/workspaces/{wid}/artifacts/report.md", params={"ref": "latest"}).json()
    assert latest["resolved"]["version"] == 1
    v1 = c.get(f"/v1/workspaces/{wid}/artifacts/report.md", params={"ref": "v1"}).json()
    assert v1["resolved"]["artifact_id"] == aid


def test_pin_outside_workspace_is_typed_403(tmp_path: Path):
    c = _client(tmp_path)
    root = tmp_path / "ws"
    root.mkdir()
    _wid, sid = _workspace_session(c, root)
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    r = c.post(f"/v1/sessions/{sid}/artifacts/pin", json={"path": str(outside)})
    # Sabotage: skip the _is_relative_to containment check in _pin_mint -> this
    # goes red (containment-before-hash lock, owner decision 10).
    assert r.status_code == 403
    assert r.json()["error"]["error"] == "path_outside_workspace"


def test_bytes_workspace_referenced_intact_is_custody_not_cas(tmp_path: Path):
    c = _client(tmp_path)
    wid, sid = _workspace_session(c, tmp_path)
    f = tmp_path / "data.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    aid = c.post(f"/v1/sessions/{sid}/artifacts/pin", json={"path": "data.csv"}).json()["pinned"][
        "artifact_id"
    ]
    r = c.get(f"/v1/artifacts/{aid}/bytes")
    # Sabotage: serve workspace-referenced bytes instead of the custody gate -> the
    # 409 goes red (the custody_not_cas lock: bytes ride the workspace file route).
    assert r.status_code == 409
    body = r.json()["error"]
    assert body["error"] == "custody_not_cas"
    assert body["details"]["fetch_via"].startswith(f"/v1/workspaces/{wid}/files/read")


def test_bytes_corrupted_file_is_integrity_violation(tmp_path: Path):
    c = _client(tmp_path)
    _wid, sid = _workspace_session(c, tmp_path)
    f = tmp_path / "series.csv"
    f.write_text("original\n", encoding="utf-8")
    aid = c.post(f"/v1/sessions/{sid}/artifacts/pin", json={"path": "series.csv"}).json()["pinned"][
        "artifact_id"
    ]
    # Corrupt the on-disk bytes AFTER mint (content no longer matches recorded sha).
    f.write_text("tampered!!\n", encoding="utf-8")
    r = c.get(f"/v1/artifacts/{aid}/bytes")
    # Sabotage: drop the re-hash comparison in _serve_bytes -> this goes red (the
    # integrity-detection lock: detection is the universal guarantee, §7).
    assert r.status_code == 409
    body = r.json()["error"]
    assert body["error"] == "integrity_violation"
    assert body["details"]["recorded_sha256"] != body["details"]["actual_sha256"]


def test_bytes_cas_custody_served_and_hash_verified(tmp_path: Path):
    """A CAS-custody version with a valid hash is served (constructed — S6 mints CAS)."""
    from clio_agent.gact.artifacts.minting import mint_artifact

    c = _client(tmp_path)
    wid, sid = _workspace_session(c, tmp_path)
    blob = tmp_path / "cas_blob.png"
    content = b"\x89PNG\r\nCASBYTES"
    blob.write_bytes(content)
    ev = IdentityEvidence.hashed_at_use(
        sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content)
    )
    # Mint a CAS-custody version directly into this app's registry.
    version = mint_artifact(
        c.app,
        sid,
        name="cas_blob.png",
        workspace_id=wid,
        evidence=ev,
        kind=ArtifactKind.IMAGE,
        mechanism=Mechanism.HARNESS,
        custody=Custody.CAS,
        path=str(blob),
    )
    r = c.get(f"/v1/artifacts/{version.artifact_id}/bytes")
    # Sabotage: gate CAS through custody_not_cas too -> the 200 goes red (the
    # CAS-serve lock: only CAS is app-served, hash-verified).
    assert r.status_code == 200
    assert r.content == content


def test_list_limit_clamp_and_before_cursor(tmp_path: Path):
    c = _client(tmp_path)
    wid, sid = _workspace_session(c, tmp_path)
    ids = []
    for i in range(3):
        f = tmp_path / f"f{i}.txt"
        f.write_text(f"c{i}\n", encoding="utf-8")
        ids.append(
            c.post(f"/v1/sessions/{sid}/artifacts/pin", json={"path": f"f{i}.txt"}).json()[
                "pinned"
            ]["artifact_id"]
        )
    # limit clamps to a page of 1 with a next_cursor.
    page = c.get(f"/v1/workspaces/{wid}/artifacts", params={"limit": 1}).json()
    # Sabotage: ignore the limit in _paginate_records -> len goes to 3, red (clamp lock).
    assert len(page["artifacts"]) == 1
    assert page["count"] == 3
    assert page["next_cursor"]
    # before-cursor with an unknown id is a typed 404.
    bad = c.get(f"/v1/workspaces/{wid}/artifacts", params={"before": "artifact_nope"})
    assert bad.status_code == 404


def test_get_unknown_artifact_is_typed_404(tmp_path: Path):
    c = _client(tmp_path)
    r = c.get("/v1/artifacts/artifact_missing")
    assert r.status_code == 404
    assert r.json()["error"]["error"] == "not_found"


# --------------------------------------------------------------------------- #
# 2b. Turn buffer + finalize resource_link append seam
# --------------------------------------------------------------------------- #


def _buffer_app():
    from types import SimpleNamespace

    return SimpleNamespace(state=SimpleNamespace(turn_artifacts=None))


def test_turn_buffer_drain_filters_by_turn_and_clears():
    from clio_agent.gact.artifacts.minting import (
        _record_turn_artifact,
        drain_turn_artifacts,
    )

    app = _buffer_app()
    v1, v2 = _version(version=1), _version(version=2)
    _record_turn_artifact(app, "s1", workspace_id="ws1", name="a.png", version=v1, turn_id="T1")
    _record_turn_artifact(app, "s1", workspace_id="ws1", name="b.png", version=v2, turn_id="T2")
    # Drain for T1 returns only the T1 entry and CLEARS the session list.
    drained = drain_turn_artifacts(app, "s1", "T1")
    # Sabotage: drop the turn_id filter in drain_turn_artifacts -> len becomes 2, red
    # (the leak-guard lock: a prior turn's mint never rides this turn's message).
    assert [e["name"] for e in drained] == ["a.png"]
    # The list is popped whole, so a second drain is empty (no cross-turn leak).
    assert drain_turn_artifacts(app, "s1", "T2") == []


def test_dedup_no_op_contributes_no_part(tmp_path: Path):
    """A same-sha dedup mints no new version, so it buffers nothing (one part per GENERATED)."""
    from clio_agent.gact.artifacts.minting import drain_turn_artifacts, mint_tool_declared_outputs

    app, sess, _arc = _fake_mint_app(tmp_path)
    png = tmp_path / "same.png"
    png.write_bytes(b"PNGDATA")
    for _ in range(2):  # mint the identical content twice -> v1 then a dedup no-op
        mint_tool_declared_outputs(
            app,
            sess.id,
            tool_name="plot",
            effective_args={"output_path": str(png)},
            call_id="c",
            workspace_id="ws1",
        )
    drained = drain_turn_artifacts(app, sess.id)
    # Sabotage: buffer on the dedup no-op path too -> len becomes 2, red (the
    # generated-not-designated lock).
    assert len(drained) == 1


def test_finalize_append_helper_builds_resource_link_parts():
    from types import SimpleNamespace

    from clio_agent.gact.artifacts.minting import _record_turn_artifact
    from clio_agent.gact.artifacts.wire import append_turn_resource_links

    appended: list[Part] = []
    transcript = SimpleNamespace(append_part=lambda part, stream_source="": appended.append(part))
    app = _buffer_app()
    _record_turn_artifact(
        app, "s1", workspace_id="ws1", name="plot.png", version=_version(), turn_id="T1"
    )
    append_turn_resource_links(app, "s1", "T1", transcript, agent_id="geospatial")
    # Sabotage: skip the append call in append_turn_resource_links -> red (the
    # finalize-wire lock: a generated artifact gets a resource_link part).
    assert len(appended) == 1
    assert appended[0].type == "resource_link"
    assert appended[0].uri == "artifact://ws1/plot.png@v2"
    assert appended[0].agent_id == "geospatial"
