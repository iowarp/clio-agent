"""A1: uploaded resources are artifacts, and lineage can cite them.

Covers the fourth mint seam (:mod:`clio_agent.gact.artifacts.resource_sources`):
a ready, materialized resource is registered as a citable ``source`` artifact at
the SAME choke point (:func:`clio_agent.gact.resource_materialization.materialize_once`)
every caller already touches — never a second path. ``create_artifact``'s
declared ``used=[...]`` channel (:mod:`clio_agent.gact.artifacts.declared_used_edges`)
resolves a source by its relay ``artifact_<hex>`` id, its ``res_<hex>`` resource
id, or its ``.clio/inputs/<res_id>/<name>`` working-copy path — clio never
guesses which inputs a deliverable used; it only makes a model-declared
reference resolvable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.lineage import build_lineage
from clio_agent.gact.artifacts.proposals import parse_proposals, promote_proposals
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.artifacts.resource_sources import register_resource_source
from clio_agent.gact.artifacts.transforms import observe_tool_transform
from clio_agent.gact.resource_custody import ResourceStore
from clio_agent.gact.resource_materialization import materialize_once
from clio_agent.gact.sessions import SessionStore
from tests.test_gact.test_post_messages import FakeClioAgent
from tests.test_gact.test_resources import _upload, _workspace

pytestmark = pytest.mark.usefixtures("host_agent_executor")


# --------------------------------------------------------------------------- #
# Lightweight fake app (parity with test_artifacts_s5.py's harness) — used for
# tests that exercise the mint/lineage primitives directly, without a live turn.
# --------------------------------------------------------------------------- #


class _CapturingArc:
    def __init__(self) -> None:
        self.events: list[object] = []

    def record_semantic_event(self, event: object) -> object:
        self.events.append(event)
        return event


class _FakeWorkspaces:
    def __init__(self, roots: dict[str, str]) -> None:
        self._roots = roots

    def get(self, wid: str) -> object:
        root = self._roots.get(wid)
        return SimpleNamespace(id=wid, root_path=root) if root else None

    def list(self) -> list[object]:
        return [SimpleNamespace(id=wid, root_path=root) for wid, root in self._roots.items()]


def _make_app(tmp_path: Path):
    """A minimal fake app carrying BOTH the artifact-registry state
    (test_artifacts_s5.py's harness) AND a real resource store, so the res_<hex>
    declared-used-edges resolution channel has something to look up."""

    store = SessionStore(path=tmp_path / "sessions.json")
    sess = store.create(workspace_id="ws1", title="t")
    arc = _CapturingArc()
    ws_root = tmp_path / "workspace"
    ws_root.mkdir(parents=True, exist_ok=True)
    resource_store = ResourceStore(root=tmp_path / "resources", max_resource_bytes=10_000_000)
    state = SimpleNamespace(
        sessions=store,
        arc=arc,
        workspaces=_FakeWorkspaces({"ws1": str(ws_root)}),
        semantic_event_sink=object(),
        semantic_trace_detail_level="semantic",
        semantic_trace_backend=None,
        artifact_registry=None,
        in_flight_turns={},
        resource_store=resource_store,
    )
    app = SimpleNamespace(state=state)
    return app, sess, arc, ws_root


def _ready_resource(app, *, name: str = "DesignSpaceGeometry.pdf", content: bytes = b"%PDF-1.4\nfake"):
    """Upload+finalize one resource against the fake app's real resource store."""

    record, _replay = app.state.resource_store.create_or_resume(
        workspace_id="ws1", name=name, declared_size=len(content), claimed_mime="application/pdf"
    )
    return app.state.resource_store.append(record.id, offset=0, data=content), content


# --------------------------------------------------------------------------- #
# 1. Registration at the materialize_once choke point + idempotency.
# --------------------------------------------------------------------------- #


def test_ready_resource_is_registered_as_one_source_artifact(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent(answer="unused"))
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        content = b"%PDF-1.4\ndesign space geometry\n"
        resource = _upload(
            client,
            workspace_id,
            name="DesignSpaceGeometry.pdf",
            content=content,
            media_type="application/pdf",
        )

        assert resource["source_registration"]["state"] == "registered", resource
        artifact_id = resource["source_artifact_id"]
        assert artifact_id.startswith("artifact_")

        registry = get_registry(app)
        found = registry.get_by_artifact_id(artifact_id)
        assert found is not None
        record, version = found
        assert version.sha256 == hashlib.sha256(content).hexdigest()
        assert version.kind.value == "other"  # schema kind; "source" is a lineage-only label
        assert version.path == resource["workspace_path"]
        assert version.custody.value == "workspace-referenced"
        assert version.producer["designation"] == "resource-upload"
        assert version.producer["resource_id"] == resource["id"]

        # Re-touching a ready, already-registered resource (the SAME funnel every
        # other caller uses) must not mint a second version.
        stored = app.state.resource_store.get(workspace_id, resource["id"])
        again = materialize_once(app, stored)
        assert again.source_artifact_id == artifact_id
        assert len(record.versions) == 1


def test_registering_the_same_resource_twice_does_not_duplicate(tmp_path: Path) -> None:
    """Direct unit pin on :func:`register_resource_source`'s own idempotency,
    independent of the ``materialize_once`` funnel that gates it."""

    app, _sess, _arc, ws_root = _make_app(tmp_path)
    record, _content = _ready_resource(app)
    record = materialize_once(app, record)
    assert record.source_registration.state == "registered"
    first_id = record.source_artifact_id

    again = register_resource_source(app, record)
    assert again.source_artifact_id == first_id

    registry = get_registry(app)
    logical = registry.get_by_artifact_id(first_id)
    assert logical is not None
    assert len(logical[0].versions) == 1


def test_copied_resource_gets_its_own_source_artifact_not_the_original(tmp_path: Path) -> None:
    """Regression pin: the destination copy must NOT inherit the source's
    registration (it would then cite an artifact in the WRONG workspace,
    and materialize_once() would wrongly skip re-registering it there)."""

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent(answer="unused"))
    with TestClient(app) as client:
        source_ws = _workspace(client, tmp_path / "source", "source")
        dest_ws = _workspace(client, tmp_path / "destination", "destination")
        source = _upload(
            client, source_ws, name="paper.pdf", content=b"%PDF-1.4\nsource", media_type="application/pdf"
        )
        assert source["source_registration"]["state"] == "registered"

        response = client.post(
            f"/v1/workspaces/{source_ws}/resources/{source['id']}/copy",
            json={"destination_workspace_id": dest_ws},
        )
        assert response.status_code == 201, response.text
        copied = response.json()

        assert copied["source_registration"]["state"] == "registered"
        assert copied["source_artifact_id"] != source["source_artifact_id"]

        registry = get_registry(app)
        dest_record, _v = registry.get_by_artifact_id(copied["source_artifact_id"])
        assert dest_record.workspace_id == dest_ws


# --------------------------------------------------------------------------- #
# 2. The model can see the id to cite.
# --------------------------------------------------------------------------- #


def test_resource_enrichment_includes_the_source_artifact_id(tmp_path: Path) -> None:
    from .conftest import complete_turn

    agent = FakeClioAgent(answer="described")
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=agent)
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(
            client,
            workspace_id,
            name="DesignSpaceGeometry.pdf",
            content=b"%PDF-1.4\ndesign space geometry\n",
            media_type="application/pdf",
        )
        artifact_id = resource["source_artifact_id"]
        assert artifact_id

        sid = client.post(
            "/v1/sessions", json={"title": "cite source", "workspace_id": workspace_id}
        ).json()["id"]
        complete_turn(
            client,
            sid,
            "Describe the pdf",
            json_override={
                "client_message_id": "cite_source",
                "parts": [
                    {"type": "text", "text": "Describe the pdf"},
                    {
                        "type": "resource_ref",
                        "resource_id": resource["id"],
                        "resource_revision": str(resource["revision"]),
                    },
                ],
            },
        )

        prompt, _called_sid = agent.calls[0]
        assert artifact_id in prompt
        assert "create_artifact" in prompt
        assert "used=" in prompt


# --------------------------------------------------------------------------- #
# 3. ``used=[...]`` resolves a source by every declared-ref form.
# --------------------------------------------------------------------------- #


def _mint_report_via_create_artifact(app, sess, ws_root: Path, name: str) -> tuple[Path, dict]:
    report = ws_root / name
    report.write_text(f"# {name}\n", encoding="utf-8")
    result = promote_proposals(
        app,
        sess.id,
        parse_proposals(
            name="", kind="report", path=str(report), content="", annotation="", artifacts=None
        ),
        workspace_id="ws1",
    )
    assert result["created"] == 1, result
    return report, result


@pytest.mark.parametrize("ref_kind", ["artifact_id", "resource_id", "workspace_path"])
def test_create_artifact_used_resolves_source_by_declared_ref_form(
    tmp_path: Path, ref_kind: str
) -> None:
    app, sess, _arc, ws_root = _make_app(tmp_path)
    resource, _content = _ready_resource(app)
    resource = materialize_once(app, resource)
    assert resource.source_registration.state == "registered"

    used_ref = {
        "artifact_id": resource.source_artifact_id,
        "resource_id": resource.id,
        "workspace_path": f".clio/inputs/{resource.id}/{resource.name}",
    }[ref_kind]

    report, result = _mint_report_via_create_artifact(app, sess, ws_root, f"description-{ref_kind}.md")
    new_id = result["artifacts"][0]["artifact_id"]
    call_args = {
        "name": "",
        "kind": "report",
        "path": str(report),
        "content": "",
        "annotation": "",
        "artifacts": None,
        "used": [used_ref],
    }
    observe_tool_transform(app, sess.id, "create_artifact", call_args, f"call_{ref_kind}", True, result)

    registry = get_registry(app)
    graph = build_lineage(registry, new_id, direction="upstream", depth=5)
    assert graph is not None
    node_ids = {n["id"] for n in graph["nodes"]}
    assert resource.source_artifact_id in node_ids
    source_node = next(n for n in graph["nodes"] if n["id"] == resource.source_artifact_id)
    assert source_node["kind"] == "source"
    assert source_node["type"] == "artifact"

    rec = registry.get_transform(f"call_{ref_kind}")
    assert rec is not None
    declared_used = {e.artifact_id for e in rec.used if e.arg == "used"}
    assert declared_used == {resource.source_artifact_id}


def test_used_resource_id_is_workspace_scoped(tmp_path: Path) -> None:
    """A ``res_<hex>`` id only ever names a resource IN the citing workspace —
    a bare id, unlike an ``artifact_<hex>`` id, carries no other identity."""

    app, _sess, _arc, _ws_root = _make_app(tmp_path)
    resource, _content = _ready_resource(app)
    resource = materialize_once(app, resource)
    assert resource.source_artifact_id

    from clio_agent.gact.artifacts.declared_used_edges import _resolve_resource_source_ref

    registry = get_registry(app)
    # Same workspace ("ws1", the fixture's only registered workspace) resolves.
    assert _resolve_resource_source_ref(app, registry, "ws1", resource.id) is not None
    # A different workspace must NOT resolve the same bare resource id.
    assert _resolve_resource_source_ref(app, registry, "some-other-workspace", resource.id) is None


# --------------------------------------------------------------------------- #
# 4. A registration failure is typed, never silent.
# --------------------------------------------------------------------------- #


def test_source_registration_failure_is_typed_and_never_blocks_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _sess, _arc, _ws_root = _make_app(tmp_path)
    record, _content = _ready_resource(app)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated mint failure")

    monkeypatch.setattr(
        "clio_agent.gact.artifacts.minting.mint_artifact_outcome", _boom
    )

    updated = materialize_once(app, record)
    assert updated.materialization.state == "ready"
    assert updated.source_registration.state == "failed"
    assert "simulated mint failure" in updated.source_registration.reason
    assert updated.source_artifact_id == ""


# --------------------------------------------------------------------------- #
# 5. Migration: a pre-existing ready resource is registered once, lazily.
# --------------------------------------------------------------------------- #


def test_preexisting_ready_resource_is_migrated_on_next_reference(tmp_path: Path) -> None:
    """A resource ready BEFORE this feature shipped has no ``source_artifact_id``
    in its persisted index row (the field defaults for a legacy record) — its
    NEXT ready-touch (``materialize_once``, called from every real call site)
    registers it, exactly once, with no separate migration code path."""

    app, _sess, _arc, ws_root = _make_app(tmp_path)
    record, content = _ready_resource(app, name="legacy.pdf")
    record = materialize_once(app, record)
    assert record.source_registration.state == "registered"
    first_artifact_id = record.source_artifact_id

    # Simulate the pre-migration on-disk shape: an index row saved before
    # ``source_artifact_id``/``source_registration`` existed. A real legacy
    # row simply lacks these keys; pydantic fills their declared defaults on
    # load — reproduced here by writing that exact legacy-shaped JSON and
    # reloading a FRESH ResourceStore from it (never mutating the live one
    # in memory, which is what a real restart would do).
    index_path = app.state.resource_store._index_path
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    for row in payload["resources"]:
        row.pop("source_artifact_id", None)
        row.pop("source_registration", None)
    index_path.write_text(json.dumps(payload), encoding="utf-8")

    reloaded_store = ResourceStore(root=app.state.resource_store.root, max_resource_bytes=10_000_000)
    reloaded_app = SimpleNamespace(state=SimpleNamespace(**{**vars(app.state), "resource_store": reloaded_store}))
    legacy_record = reloaded_store.get("ws1", record.id)
    assert legacy_record is not None
    assert legacy_record.source_registration.state == "pending"
    assert legacy_record.source_artifact_id == ""

    migrated = materialize_once(reloaded_app, legacy_record)
    assert migrated.source_registration.state == "registered"
    # Same content, same logical name -> the registry's own dedup identifies
    # it as the SAME artifact chain, not a duplicate registration.
    assert migrated.source_artifact_id == first_artifact_id

    registry = get_registry(reloaded_app)
    logical = registry.get_by_artifact_id(first_artifact_id)
    assert logical is not None
    assert len(logical[0].versions) == 1

    # A second lazy touch stays a no-op (already ``registered``).
    again = materialize_once(reloaded_app, migrated)
    assert again.source_artifact_id == first_artifact_id
    assert len(registry.get_by_artifact_id(first_artifact_id)[0].versions) == 1
