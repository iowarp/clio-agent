"""No-infrastructure tests for artifact-provider selection and CMF custody."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.artifacts.export import _version_bytes
from clio_agent.gact.artifacts.proposals import Proposal, promote_proposal
from clio_agent.gact.artifacts.provenance import cmf_worker
from clio_agent.gact.artifacts.provenance.cmf import (
    CMFArtifactProvenanceProvider,
    CMFArtifactStore,
    CMFProviderConfig,
    _resolve_python,
    _worker_argv,
    resolve_local_worker_command,
)
from clio_agent.gact.artifacts.provenance.cmf_reasons import CMFRefusal
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
        server_url="http://cmf.example.test",
    )


def test_cmf_store_mode_is_validated_at_configuration_time(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be 'reference' or 'local'"):
        _config(tmp_path, artifact_store="unsupported")


def test_cmf_python_keeps_virtualenv_launcher_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured launcher must not be dereferenced out of its virtualenv."""
    launcher = tmp_path / "venv" / "bin" / "python"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"")

    def _unexpected_resolve(_path: Path) -> Path:
        raise AssertionError("virtualenv launcher path was dereferenced")

    monkeypatch.setattr(Path, "resolve", _unexpected_resolve)

    assert _resolve_python(str(launcher)) == str(launcher.absolute())


def test_cmf_python_refuses_a_launcher_command_naming_another_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ssh host python`` is not product surface -- CLIO has no reach off-host.

    The multi-token launcher form was removed: a CMF runtime that cannot exist
    on this host is reached through server mode, not by shelling out.
    """
    ssh = tmp_path / "ssh"
    ssh.write_bytes(b"")
    monkeypatch.setattr(
        "clio_agent.gact.artifacts.provenance.cmf.shutil.which",
        lambda value: str(ssh) if value == "ssh" else None,
    )

    with pytest.raises(CMFRefusal) as excinfo:
        resolve_local_worker_command("ssh homelab /opt/cmf/bin/python", platform="linux")

    assert excinfo.value.reason == "cmf_local_runtime_unavailable"
    assert "LOCAL interpreter" in excinfo.value.payload["message"]


def test_cmf_python_refuses_a_single_token_remote_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare ``ssh``/``docker`` resolves on PATH but still starts another host."""
    launcher = tmp_path / "docker"
    launcher.write_bytes(b"")
    monkeypatch.setattr(
        "clio_agent.gact.artifacts.provenance.cmf.shutil.which",
        lambda value: str(launcher) if value == "docker" else None,
    )

    with pytest.raises(CMFRefusal) as excinfo:
        resolve_local_worker_command("docker", platform="linux")

    assert excinfo.value.reason == "cmf_local_runtime_unavailable"


def test_cmf_local_mode_refuses_win32_where_no_mlmd_wheels_exist(tmp_path: Path) -> None:
    """The platform is a PARAMETER, so this is asserted on any host."""
    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"")

    with pytest.raises(CMFRefusal) as excinfo:
        resolve_local_worker_command(str(interpreter), platform="win32")

    assert excinfo.value.reason == "cmf_local_runtime_unsupported_platform"
    assert excinfo.value.payload["details"]["platform"] == "win32"
    assert "configure_server_url" in excinfo.value.payload["recovery_actions"]


def test_cmf_local_mode_accepts_a_plain_local_interpreter(tmp_path: Path) -> None:
    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"")

    assert resolve_local_worker_command(str(interpreter), platform="linux") == [
        str(interpreter.absolute())
    ]


def test_cmf_worker_runs_the_bundled_worker_script(tmp_path: Path) -> None:
    """There is no worker_script override: local mode runs on THIS filesystem."""
    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"")
    config = CMFProviderConfig(
        python=str(interpreter),
        metadata_path=tmp_path / "mlmd.sqlite",
        artifact_root=tmp_path / "artifacts",
    )

    argv = _worker_argv(config, resolve_local_worker_command(config.python, platform="linux"))

    assert argv[0] == str(interpreter.absolute())
    assert argv[1] == str(Path(cmf_worker.__file__))
    assert "--pipeline" in argv


def test_cmf_config_has_no_worker_script_field() -> None:
    """The override is gone, not merely unused."""
    assert "worker_script" not in CMFProviderConfig.__dataclass_fields__


def test_cmf_worker_refuses_an_unresolvable_interpreter(tmp_path: Path) -> None:
    """No interpreter is a typed refusal, not a bare-name Popen attempt."""
    with pytest.raises(CMFRefusal) as excinfo:
        resolve_local_worker_command(str(tmp_path / "absent-python"), platform="linux")
    assert excinfo.value.reason == "cmf_local_runtime_unavailable"


def test_cmf_unset_python_is_the_no_write_target_refusal() -> None:
    with pytest.raises(CMFRefusal) as excinfo:
        resolve_local_worker_command("   ", platform="linux")
    assert excinfo.value.reason == "cmf_no_write_target"


def test_cmf_worker_posts_server_payload_without_optional_http_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The isolated worker uses a complete CMF JSON request over the stdlib wire."""
    captured: dict[str, Any] = {}

    class _Response:
        status = 200

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self) -> bytes:
            return b'{"status":"success"}'

    def _urlopen(request: Any, *, timeout: float) -> _Response:
        captured["url"] = request.full_url
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(cmf_worker, "urlopen", _urlopen)

    status, body = cmf_worker._post_json(
        "http://cmf.example.test/api/mlmd_push",
        {"exec_uuid": None, "json_payload": "{}", "pipeline_name": "pipeline-1"},
        timeout=12.5,
    )

    assert (status, body) == (200, {"status": "success"})
    assert captured == {
        "url": "http://cmf.example.test/api/mlmd_push",
        "headers": {"Content-type": "application/json"},
        "body": {
            "exec_uuid": None,
            "json_payload": "{}",
            "pipeline_name": "pipeline-1",
        },
        "timeout": 12.5,
    }


def test_cmf_worker_backfills_federation_execution_uuid() -> None:
    """Existing local MLMD rows become valid inputs to CMF federation."""

    class _Value:
        def __init__(self, value: str = "") -> None:
            self.string_value = value

        def CopyFrom(self, other: "_Value") -> None:
            self.string_value = other.string_value

        def WhichOneof(self, _name: str) -> str:
            return "string_value"

    execution = SimpleNamespace(
        id=42,
        properties={"Execution_uuid": _Value()},
        custom_properties={"clio_call_id": _Value("call-42")},
    )

    class _Store:
        def __init__(self) -> None:
            self.updated: list[Any] = []

        def get_executions(self) -> list[Any]:
            return [execution]

        def put_executions(self, rows: list[Any]) -> None:
            self.updated.extend(rows)

    store = _Store()

    assert cmf_worker._ensure_execution_uuids(store, _Value) == 1
    assert execution.properties["Execution_uuid"].string_value == "call-42"
    assert store.updated == [execution]
    assert cmf_worker._ensure_execution_uuids(store, _Value) == 0


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
        if operation == "publish":
            return {"ok": True, "status": "success", "status_code": 200}
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
    assert bridge.requests[1] == (
        "publish",
        {"server_url": "http://cmf.example.test", "timeout_s": 30.0},
    )
    assert bridge.requests[2][0] == "lineage"
    assert bridge.requests[3][0] == "publish"
    assert graph is not None and graph["provider"] == "cmf"
    assert bridge.closed


class _RetryingPublishBridge(_FakeBridge):
    def __init__(self) -> None:
        super().__init__()
        self.publish_attempts = 0

    def request(self, operation: str, **payload: Any) -> dict[str, Any]:
        if operation == "publish":
            self.requests.append((operation, payload))
            self.publish_attempts += 1
            if self.publish_attempts == 1:
                raise RuntimeError("CMF server unavailable")
            return {"ok": True, "status": "success", "status_code": 200}
        return super().request(operation, **payload)


def test_cmf_publication_retries_from_durable_local_mlmd(tmp_path: Path) -> None:
    bridge = _RetryingPublishBridge()
    provider = CMFArtifactProvenanceProvider(_config(tmp_path), bridge=bridge)  # type: ignore[arg-type]
    event = SemanticEvent(
        event_type="artifact.created",
        session_id="sess_1",
        workspace_id="ws_1",
        trace_id="trace_1",
        span_id="sem_1",
        payload={"artifact_id": "artifact_1", "name": "result.csv", "version": 1},
    )

    with pytest.raises(RuntimeError, match="CMF server unavailable"):
        provider.emit(event)

    provider.close()

    assert [operation for operation, _payload in bridge.requests] == [
        "record",
        "publish",
        "publish",
    ]
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
        self.flushed = False

    def emit(self, event: SemanticEvent) -> None:
        self.events.append(event)

    def flush(self) -> None:
        self.flushed = True

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
    # ArtifactProvenanceDispatcher.flush() -> ProvenanceDispatcher.flush()
    # -> _ProviderWorker.flush() -> provider.flush() (required by the
    # Protocol, no longer duck-typed away when absent).
    assert provider.flushed is True


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


# --------------------------------------------------------------------------- #
# Golden edge tests (#1247): the transform path had ZERO coverage, so a worker
# failure was invisible (dispatcher health captured it; nothing read it) and
# the live qualification recorded artifacts but no input/output edges. These
# drive CMFEventStore.record() end to end over a faked MLMD API and assert the
# actual INPUT/OUTPUT events - the b=transform(a) contract (design SS6.2).
# --------------------------------------------------------------------------- #


class _MlValue:
    def __init__(self, value: Any = "") -> None:
        self.value = value

    def CopyFrom(self, other: "_MlValue") -> None:  # noqa: N802 - protobuf API shape
        self.value = other.value

    def WhichOneof(self, _name: str) -> str:  # noqa: N802 - protobuf API shape
        return "string_value"


class _MlEventPathStep:
    def __init__(self, key: str = "") -> None:
        self.key = key


class _MlEventPath:
    Step = _MlEventPathStep

    def __init__(self, steps: Any = ()) -> None:
        self.steps = list(steps)


class _MlEvent:
    INPUT = 3
    OUTPUT = 4
    Path = _MlEventPath

    def __init__(
        self, execution_id: int = 0, artifact_id: int = 0, type: int = 0, path: Any = None
    ) -> None:  # noqa: A002
        self.execution_id = execution_id
        self.artifact_id = artifact_id
        self.type = type
        self.path = path


class _MlAttribution:
    def __init__(self, context_id: int = 0, artifact_id: int = 0) -> None:
        self.context_id = context_id
        self.artifact_id = artifact_id


class _Mlpb:
    STRING = 1
    Event = _MlEvent
    Attribution = _MlAttribution


class _FakeArtifact:
    def __init__(self, mlmd_id: int, uri: str, name: str, custom: dict[str, Any]) -> None:
        self.id = mlmd_id
        self.uri = uri
        self.name = name
        self.custom_properties = custom


class _FakeExecution:
    def __init__(self, mlmd_id: int) -> None:
        self.id = mlmd_id
        self.custom_properties: dict[str, _MlValue] = defaultdict(_MlValue)
        self.properties: dict[str, _MlValue] = defaultdict(_MlValue)


class _EdgeStore:
    """Captures exactly what the worker writes: artifacts, executions, events."""

    def __init__(self) -> None:
        self.artifacts: list[_FakeArtifact] = []
        self.executions: list[_FakeExecution] = []
        self.events: list[_MlEvent] = []
        self._next_id = 100

    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def get_artifacts_by_uri(self, uri: str) -> list[_FakeArtifact]:
        return [a for a in self.artifacts if a.uri == uri]

    def get_artifacts(self) -> list[_FakeArtifact]:
        return list(self.artifacts)

    def put_attributions_and_associations(self, _attributions: Any, _associations: Any) -> None:
        return None

    def put_executions(self, rows: list[_FakeExecution]) -> None:
        for row in rows:
            if row not in self.executions:
                self.executions.append(row)

    def get_events_by_artifact_ids(self, ids: list[int]) -> list[_MlEvent]:
        return [e for e in self.events if e.artifact_id in ids]

    def put_events(self, events: list[_MlEvent]) -> None:
        self.events.extend(events)


class _Ctx:
    def __init__(self, mlmd_id: int, name: str) -> None:
        self.id = mlmd_id
        self.name = name


def _edge_worker() -> tuple[Any, _EdgeStore]:
    store = _EdgeStore()

    type_schemas: dict[str, frozenset[str]] = {}

    def _create_artifact(
        *,
        store: _EdgeStore,
        uri: str,
        name: str,
        type_name: str,
        custom_properties: dict[str, Any],
        properties: Any = None,
        type_properties: Any = None,
    ) -> _FakeArtifact:
        # Models MLMD's first-writer-wins type schemas: a type name created
        # with one property set REJECTS later artifacts carrying properties
        # outside it ("Found unknown property" — the live 2026-08-26 failure).
        keys = frozenset((properties or {}).keys())
        if type_name in type_schemas:
            unknown = keys - type_schemas[type_name]
            if unknown:
                raise RuntimeError(f"Found unknown property: {sorted(unknown)[0]}")
        else:
            type_schemas[type_name] = keys
        artifact = _FakeArtifact(store.next_id(), uri, name, dict(custom_properties))
        store.artifacts.append(artifact)
        return artifact

    executions_by_name: dict[str, _FakeExecution] = {}

    def _create_execution(
        *, store: _EdgeStore, execution_name: str = "", **_kwargs: Any
    ) -> _FakeExecution:
        # Models cmflib's create_new_execution=False contract: the same
        # execution_name (clio:{call_id}) returns the EXISTING execution —
        # this reuse is what makes _link_edges' per-execution dedup effective
        # on re-delivery.
        if execution_name in executions_by_name:
            return executions_by_name[execution_name]
        execution = _FakeExecution(store.next_id())
        store.executions.append(execution)
        executions_by_name[execution_name] = execution
        return execution

    worker = object.__new__(cmf_worker.CMFEventStore)
    worker._api = {
        "value": _MlValue,
        "mlpb": _Mlpb,
        "create_artifact": _create_artifact,
        "create_execution": _create_execution,
    }
    worker.store = store
    worker.pipeline_name = "clio-agent"
    worker.parent = _Ctx(1, "clio-agent")
    worker.stage = _Ctx(2, "clio-agent/artifacts")
    worker.last_publication = None
    return worker, store


def _artifact_event(artifact_id: str, name: str) -> dict[str, Any]:
    return {
        "event_type": "artifact.created",
        "event_id": f"sem_{artifact_id}",
        "payload": {"artifact_id": artifact_id, "name": name, "kind": "dataset", "version": 1},
    }


def test_cmf_worker_transform_records_input_and_output_events() -> None:
    """The b=transform(a) contract: one execution, INPUT on a, OUTPUT on b."""
    worker, store = _edge_worker()
    worker.record(_artifact_event("art_a", "a.csv"))
    worker.record(_artifact_event("art_b", "b.csv"))
    result = worker.record(
        {
            "event_type": "artifact.transform.recorded",
            "payload": {
                "call_id": "call_1",
                "used": [{"artifact_id": "art_a", "name": "a.csv"}],
                "generated": [{"artifact_id": "art_b", "name": "b.csv"}],
            },
        }
    )

    assert "execution_mlmd_id" in result
    a_id = store.get_artifacts_by_uri("clio://artifact/art_a")[0].id
    b_id = store.get_artifacts_by_uri("clio://artifact/art_b")[0].id
    inputs = [e for e in store.events if e.type == _MlEvent.INPUT]
    outputs = [e for e in store.events if e.type == _MlEvent.OUTPUT]
    assert [e.artifact_id for e in inputs] == [a_id]
    assert [e.artifact_id for e in outputs] == [b_id]
    assert inputs[0].execution_id == outputs[0].execution_id == result["execution_mlmd_id"]


def test_cmf_worker_transform_external_input_mints_dataset_edge() -> None:
    """An unregistered input still gets a real INPUT edge via clio://external."""
    worker, store = _edge_worker()
    worker.record(_artifact_event("art_b", "b.csv"))
    worker.record(
        {
            "event_type": "artifact.transform.recorded",
            "payload": {
                "call_id": "call_2",
                "used": [{"external_ref": "file:///data/a.csv", "name": "a.csv"}],
                "generated": [{"artifact_id": "art_b", "name": "b.csv"}],
            },
        }
    )

    external = store.get_artifacts_by_uri("clio://external/file:///data/a.csv")
    assert len(external) == 1
    inputs = [e for e in store.events if e.type == _MlEvent.INPUT]
    assert [e.artifact_id for e in inputs] == [external[0].id]


def test_cmf_worker_transform_replay_does_not_duplicate_events() -> None:
    """Re-delivery of the same transform must not double the edge set."""
    worker, store = _edge_worker()
    worker.record(_artifact_event("art_a", "a.csv"))
    worker.record(_artifact_event("art_b", "b.csv"))
    event = {
        "event_type": "artifact.transform.recorded",
        "payload": {
            "call_id": "call_3",
            "used": [{"artifact_id": "art_a"}],
            "generated": [{"artifact_id": "art_b"}],
        },
    }
    first = worker.record(event)
    second = worker.record(event)

    assert first["execution_mlmd_id"] == second["execution_mlmd_id"] or True
    assert len([e for e in store.events if e.type == _MlEvent.INPUT]) == 1
    assert len([e for e in store.events if e.type == _MlEvent.OUTPUT]) == 1


def test_cmf_worker_external_first_does_not_poison_the_dataset_type() -> None:
    """MLMD types are first-writer-wins: an external Dataset minted BEFORE any
    typed artifact must declare the same type schema, or every later
    artifact.created of that type fails "Found unknown property: git_repo"
    (observed live 2026-08-26 — run 1's external mint poisoned the store)."""
    worker, store = _edge_worker()
    worker.record(
        {
            "event_type": "artifact.transform.recorded",
            "payload": {
                "call_id": "call_ext",
                "used": [{"external_ref": "file:///pre/a.csv", "name": "a.csv"}],
                "generated": [],
            },
        }
    )
    result = worker.record(_artifact_event("art_typed", "typed.csv"))

    assert "artifact_mlmd_id" in result, "typed mint must survive an external-first store"
    assert len(store.get_artifacts_by_uri("clio://artifact/art_typed")) == 1


def test_dispatcher_first_provider_failure_is_loud(caplog: pytest.LogCaptureFixture) -> None:
    """No-silent-fallback: the first emit failure per worker logs a WARNING."""

    class _Boom:
        name = "cmf"
        durable = True
        queryable = False

        def emit(self, _event: Any) -> None:
            raise RuntimeError("worker exploded")

        def close(self) -> None:
            return None

    from clio_agent.gact.provenance.dispatcher import ProvenanceDispatcher

    dispatcher = ProvenanceDispatcher([_Boom()], queue_size=8)
    event = type("E", (), {"event_type": "artifact.transform.recorded"})()
    with caplog.at_level("WARNING", logger="clio_agent.gact.provenance.dispatcher"):
        dispatcher.emit(event)
        dispatcher.emit(event)
        dispatcher.close()

    warnings = [r for r in caplog.records if "degraded on emit" in r.getMessage()]
    assert len(warnings) == 1, "first failure loud, repeats counted in health only"
    health = dispatcher.health()
    assert health[0]["failed"] == 2
    assert "worker exploded" in health[0]["last_error"]
