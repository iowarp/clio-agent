"""No-infrastructure tests for artifact-provider selection and CMF custody."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.artifacts.export import _version_bytes
from clio_agent.gact.artifacts.proposals import Proposal, promote_proposal
from clio_agent.gact.artifacts.provenance.cmf import (
    CMFArtifactProvenanceProvider,
    CMFArtifactStore,
    CMFProviderConfig,
)
from clio_agent.gact.artifacts.provenance.selector import ArtifactProvenanceDispatcher
from clio_agent.gact.artifacts.records import (
    ArtifactKind,
    ArtifactVersion,
    Custody,
    EvidenceClass,
    Mechanism,
)
from clio_agent.gact.artifacts.storage import ingest_artifact_identity
from clio_agent.gact.events import EventBus
from clio_agent.gact.routes.artifact_lineage import register_artifact_lineage_routes
from clio_agent.gact.semantic_events import SemanticEvent, SemanticEventSink
from tests._config_layer import set_config
from tests.test_gact.test_artifacts_s3 import _make_app


def _config(tmp_path: Path, *, artifact_store: str = "local") -> CMFProviderConfig:
    return CMFProviderConfig(
        python="missing-cmf-python",
        metadata_path=tmp_path / "cmf" / "mlmd.sqlite",
        artifact_root=tmp_path / "cmf" / "artifacts",
        artifact_store=artifact_store,
    )


def test_cmf_store_mode_is_validated_at_configuration_time(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be 'reference' or 'local'"):
        _config(tmp_path, artifact_store="unsupported")


def _version(identity: Any) -> ArtifactVersion:
    return ArtifactVersion(
        artifact_id="artifact_cmf_1",
        version=1,
        kind=ArtifactKind.DATASET,
        custody=identity.custody,
        mechanism=Mechanism.HARNESS,
        evidence=identity.evidence,
        producer={"storage_receipt": identity.storage_receipt},
        path="gone.csv",
    )


def test_cmf_local_store_is_primary_dvc_cas_with_cross_hash_receipt(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "dataset.csv"
    content = b"x,y\n1,2\n"
    source.write_bytes(content)
    store = CMFArtifactStore(_config(tmp_path))

    identity = store.ingest(source, workspace_root=workspace)

    assert identity.custody is Custody.EXTERNAL_REFERENCED
    assert identity.evidence.sha256 == hashlib.sha256(content).hexdigest()
    receipt = identity.storage_receipt
    assert receipt is not None
    assert receipt["provider"] == "cmf"
    assert receipt["backend"] == "dvc-local"
    assert receipt["digests"] == {
        "sha256": hashlib.sha256(content).hexdigest(),
        "md5": hashlib.md5(content, usedforsecurity=False).hexdigest(),
    }
    assert not (workspace / ".clio" / "agent" / "artifacts" / "cas").exists()

    version = _version(identity)
    source.unlink()
    owned = store.resolve_owned_path(version, workspace_root=workspace)
    assert owned is not None and owned.read_bytes() == content
    app = SimpleNamespace(
        state=SimpleNamespace(
            artifact_provenance_backend=SimpleNamespace(store=store),
        )
    )
    assert _version_bytes(app, workspace, version, max_bytes=1024) == content
    owned.write_bytes(b"corrupt")
    assert store.resolve_owned_path(version, workspace_root=workspace) is None


def test_cmf_reference_mode_hashes_without_copying_to_either_store(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "referenced.txt"
    source.write_text("reference only", encoding="utf-8")
    store = CMFArtifactStore(_config(tmp_path, artifact_store="reference"))

    identity = store.ingest(source, workspace_root=workspace)

    assert identity.custody is Custody.WORKSPACE_REFERENCED
    assert identity.storage_receipt is None
    assert identity.reason == "cmf_metadata_reference:cas_store_unavailable"
    assert not _config(tmp_path).artifact_root.exists()
    assert not (workspace / ".clio" / "agent" / "artifacts" / "cas").exists()


def test_cmf_reference_preserves_configured_stat_pinned_identity(tmp_path: Path) -> None:
    set_config("artifacts.hash_max_file_bytes", 1)
    source = tmp_path / "large-by-policy.bin"
    source.write_bytes(b"larger than one byte")

    identity = CMFArtifactStore(_config(tmp_path, artifact_store="reference")).ingest(
        source,
        workspace_root=tmp_path,
    )

    assert identity.evidence.evidence_class is EvidenceClass.STAT_PINNED
    assert identity.evidence.sha256 is None
    assert identity.not_ingested_size == source.stat().st_size
    assert identity.reason == "cmf_metadata_reference:over_hash_threshold"


def test_selected_store_drives_mint_ingestion_without_global_cas(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "selected.bin"
    source.write_bytes(b"selected CMF primary")
    selected = CMFArtifactStore(_config(tmp_path))
    app = SimpleNamespace(
        state=SimpleNamespace(
            artifact_provenance_backend=SimpleNamespace(store=selected),
        )
    )

    identity = ingest_artifact_identity(app, source, workspace_root=workspace)

    assert identity.storage_receipt is not None
    assert identity.storage_receipt["provider"] == "cmf"
    assert not (workspace / ".clio" / "agent" / "artifacts" / "cas").exists()


def test_inline_proposal_uses_selected_cmf_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    app, session, _arc = _make_app(tmp_path)
    selected = CMFArtifactStore(_config(tmp_path))
    app.state.artifact_provenance_backend = SimpleNamespace(store=selected)

    outcome = promote_proposal(
        app,
        session.id,
        Proposal(name="inline.md", kind="report", content="provider-owned\n"),
        workspace_id="ws1",
    )

    assert outcome.accepted and outcome.version is not None
    assert outcome.version.custody is Custody.EXTERNAL_REFERENCED
    assert outcome.version.producer["storage_receipt"]["provider"] == "cmf"
    assert selected.resolve_owned_path(outcome.version, workspace_root=tmp_path) is not None
    assert not (tmp_path / ".clio" / "agent" / "artifacts" / "cas").exists()


class _FakeBridge:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def request(self, operation: str, **payload: Any) -> dict[str, Any]:
        self.requests.append((operation, payload))
        if operation == "lineage":
            artifact_id = str(payload["artifact_id"])
            return {
                "ok": True,
                "graph": {
                    "root": artifact_id,
                    "direction": payload["direction"],
                    "depth": payload["depth"],
                    "nodes": [{"id": artifact_id, "type": "artifact"}],
                    "edges": [],
                    "truncated": None,
                    "provider": "cmf",
                },
            }
        return {"ok": True}

    def close(self) -> None:
        self.closed = True


def test_cmf_provider_submits_explicit_events_and_normalizes_queries(tmp_path: Path) -> None:
    bridge = _FakeBridge()
    provider = CMFArtifactProvenanceProvider(_config(tmp_path), bridge=bridge)  # type: ignore[arg-type]
    event = SemanticEvent(
        event_type="artifact.created",
        session_id="sess_1",
        workspace_id="ws_1",
        trace_id="trace_1",
        span_id="sem_1",
        payload={"artifact_id": "artifact_1", "name": "result.csv", "version": 1},
    )

    provider.emit(event)
    graph = provider.lineage("artifact_1", direction="upstream", depth=4)
    provider.close()

    assert bridge.requests[0][0] == "record"
    assert bridge.requests[0][1]["event"]["event_id"] == "sem_1"
    assert bridge.requests[1][0] == "lineage"
    assert graph is not None and graph["provider"] == "cmf"
    assert bridge.closed


class _CapturingBackend:
    name = "capture"

    def __init__(self) -> None:
        self.events: list[SemanticEvent] = []

    def emit(self, event: SemanticEvent) -> None:
        self.events.append(event)


class _ArtifactCapture:
    name = "artifact-capture"
    durable = True
    queryable = True
    store = SimpleNamespace(name="test")

    def __init__(self) -> None:
        self.events: list[SemanticEvent] = []

    def emit(self, event: SemanticEvent) -> None:
        self.events.append(event)

    def lineage(self, artifact_id: str, **kwargs: Any) -> None:
        del artifact_id, kwargs

    def close(self) -> None:
        return None


def test_artifact_substream_overlaps_parent_without_becoming_parallel_provider() -> None:
    parent = _CapturingBackend()
    provider = _ArtifactCapture()
    artifact = ArtifactProvenanceDispatcher(provider)  # type: ignore[arg-type]
    sink = SemanticEventSink(
        bus=EventBus(),
        trace_backend=parent,
        artifact_backend=artifact,
    )
    turn = SemanticEvent(event_type="turn.started", session_id="s", trace_id="t")
    created = SemanticEvent(event_type="artifact.created", session_id="s", trace_id="t")

    sink.emit(turn)
    sink.emit(created)
    artifact.flush()
    artifact.close()

    assert [event.event_type for event in parent.events] == ["turn.started", "artifact.created"]
    assert [event.event_type for event in provider.events] == ["artifact.created"]


class _LineageProvider:
    provider_name = "cmf"

    def lineage(self, artifact_id: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "root": artifact_id,
            "direction": kwargs["direction"],
            "depth": kwargs["depth"],
            "nodes": [{"id": artifact_id, "type": "artifact"}],
            "edges": [],
            "truncated": None,
            "provider": "cmf",
        }


def test_lineage_http_surface_is_artifact_provider_independent() -> None:
    app = FastAPI()
    app.state.artifact_provenance_backend = _LineageProvider()
    register_artifact_lineage_routes(app)

    response = TestClient(app).get(
        "/v1/artifacts/artifact_remote/lineage",
        params={"direction": "upstream", "depth": 5},
    )

    assert response.status_code == 200
    assert response.json() == {
        "root": "artifact_remote",
        "direction": "upstream",
        "depth": 5,
        "nodes": [{"id": "artifact_remote", "type": "artifact"}],
        "edges": [],
        "truncated": None,
        "provider": "cmf",
    }
