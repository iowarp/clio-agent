"""Real TCP qualification of organization → artifact storage → one scientific dataset."""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
import pytest

from clio_agent.gact.artifacts.registry import ArtifactRegistry
from clio_agent.gact.infrastructure.models import SshRoute
from clio_agent.gact.resource_custody import ResourceStore
from clio_agent.gact.skills import SkillCatalog, read_skill_body
from clio_agent.gact.storage import ndp_artifact_upload, ndp_endpoint_tools, ndp_endpoint_transport
from clio_agent.gact.storage.ndp_artifact_upload import (
    EndpointUploadReceipt,
    prepare_artifact_upload,
    upload_artifact,
)
from clio_agent.gact.storage.ndp_endpoint_models import EndpointConnectionInput, EndpointRequest
from clio_agent.gact.storage.ndp_endpoints import NDPEndpointService
from clio_agent.gact.storage.store import SourceStore


def _parameter(name: str, location: str, kind: str = "string") -> dict[str, Any]:
    return {
        "name": name,
        "in": location,
        "required": location == "path",
        "schema": {"type": kind},
    }


def _body(required: list[str], properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "required": True,
        "content": {
            "application/json": {
                "schema": {"type": "object", "required": required, "properties": properties}
            }
        },
    }


SCHEMA = {
    "openapi": "3.1.0",
    "info": {"title": "Isolated NDP authoring peer"},
    "paths": {
        "/organization": {
            "get": {"parameters": [_parameter("server", "query")]},
            "post": {
                "requestBody": _body(
                    ["name", "title"],
                    {"name": {"type": "string"}, "title": {"type": "string"}},
                )
            },
        },
        "/dataset": {
            "post": {
                "parameters": [_parameter("server", "query")],
                "requestBody": _body(
                    ["name", "title", "owner_org", "resources"],
                    {
                        "name": {"type": "string"},
                        "title": {"type": "string"},
                        "owner_org": {"type": "string"},
                        "resources": {"type": "array"},
                        "private": {"type": "boolean"},
                    },
                ),
            }
        },
        "/dataset/{dataset_id}": {"get": {"parameters": [_parameter("dataset_id", "path")]}},
        "/s3/objects/{bucket_name}": {
            "post": {
                "parameters": [_parameter("bucket_name", "path")],
                "requestBody": {
                    "required": True,
                    "content": {
                        "multipart/form-data": {
                            "schema": {
                                "type": "object",
                                "required": ["file"],
                                "properties": {
                                    "file": {"type": "string", "format": "binary"},
                                    "object_key": {"type": ["string", "null"]},
                                },
                            }
                        }
                    },
                },
            }
        },
        "/s3/objects/{bucket_name}/{object_key}": {
            "get": {
                "parameters": [_parameter("bucket_name", "path"), _parameter("object_key", "path")]
            }
        },
    },
}


class CatalogPeer(BaseHTTPRequestHandler):
    """Small test-only server retaining actual multipart bytes and catalog records."""

    objects: dict[str, bytes]
    datasets: list[dict[str, Any]]
    organizations: list[str]
    writes: int
    lose_response: bool
    corrupt: bool
    deny: bool
    requests: list[tuple[str, dict[str, str]]]

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the test receipt free of HTTP logging noise."""

    def respond(self, status: int, data: Any, *, raw: bool = False) -> None:
        """Send finite, uncompressed bytes with an explicit length."""
        body = data if raw else json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream" if raw else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        """Serve discovery, actual stored objects and authoring read-back."""
        self.requests.append(("GET", dict(self.headers)))
        path = unquote(urlsplit(self.path).path)
        if path == "/openapi.json":
            self.respond(200, SCHEMA)
        elif path == "/organization":
            self.respond(200, self.organizations)
        elif path.startswith("/dataset/"):
            self.respond(200, self.datasets[int(path.rsplit("/", 1)[-1])])
        elif self.deny:
            self.respond(403, {"detail": "Writer role required"})
        elif path in self.objects:
            self.respond(200, self.objects[path], raw=True)
        else:
            self.respond(404, {"detail": "Object not found"})

    def do_POST(self) -> None:
        """Retain a file or create the requested catalog record, without production fakes."""
        self.requests.append(("POST", dict(self.headers)))
        cls = type(self)
        cls.writes += 1
        body = self.rfile.read(int(self.headers["Content-Length"]))
        path = urlsplit(self.path).path
        if path == "/organization":
            organization = json.loads(body)
            self.organizations.append(organization["name"])
            self.respond(201, organization)
        elif path == "/dataset":
            dataset = json.loads(body)
            dataset["id"] = str(len(self.datasets))
            self.datasets.append(dataset)
            self.respond(201, {"id": dataset["id"], "name": dataset["name"]})
        else:
            message = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {self.headers['Content-Type']}\r\nMIME-Version: 1.0\r\n\r\n".encode()
                + body
            )
            parts: dict[str, bytes] = {}
            for item in message.iter_parts():
                payload = item.get_payload(decode=True)
                assert isinstance(payload, bytes)
                parts[str(item.get_param("name", header="content-disposition"))] = payload
            key = parts["object_key"].decode()
            self.objects[path + "/" + key] = b"wrong bytes" if self.corrupt else parts["file"]
            if self.lose_response:
                cls.lose_response = False
                self.close_connection = True
                return
            self.respond(200, {"bucket": path.rsplit("/", 1)[-1], "key": key})


@pytest.fixture
def peer() -> Iterator[str]:
    """Use a real local socket, with no production HTTP transport patched."""
    CatalogPeer.objects, CatalogPeer.datasets, CatalogPeer.organizations = {}, [], []
    CatalogPeer.writes = 0
    CatalogPeer.lose_response = CatalogPeer.corrupt = CatalogPeer.deny = False
    CatalogPeer.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), CatalogPeer)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def _artifact(app: Any, root: Path, name: str, content: bytes, *, workspace: str = "ws") -> str:
    """Register the actual byte identity through the real registry's event fold."""
    path = root / name
    path.write_bytes(content)
    artifact_id = "artifact_" + hashlib.sha256(name.encode()).hexdigest()[:20]
    result = app.state.artifact_registry.fold_payload(
        {
            "event_id": artifact_id,
            "artifact_id": artifact_id,
            "workspace_id": workspace,
            "name": name,
            "version": 1,
            "kind": "dataset",
            "custody": "workspace-referenced",
            "mechanism": "model-call",
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "path": str(path),
            "evidence": {"evidence_class": "hashed-at-use"},
        }
    )
    assert result.applied
    return artifact_id


def _app(root: Path) -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(
            artifact_registry=ArtifactRegistry(),
            workspaces={"ws": SimpleNamespace(root_path=str(root))},
        )
    )


@pytest.mark.parametrize(
    "file_schema",
    [
        {"type": "string", "format": "binary"},
        {"type": "string", "contentMediaType": "application/octet-stream"},
    ],
)
def test_three_artifacts_form_one_dataset_with_verified_bytes(
    tmp_path: Path, peer: str, monkeypatch: pytest.MonkeyPatch, file_schema: dict[str, Any]
) -> None:
    """Exercise creation and read-back of an org, three objects, and one dataset."""
    app = _app(tmp_path)
    monkeypatch.setitem(
        SCHEMA["paths"]["/s3/objects/{bucket_name}"]["post"]["requestBody"]["content"][
            "multipart/form-data"
        ]["schema"]["properties"],
        "file",
        file_schema,
    )
    store = SourceStore(tmp_path / "ledger")
    owner = NDPEndpointService(store, "tester")
    connection = owner.connect("ws", EndpointConnectionInput(name="Test lab", url=peer))
    resources = ResourceStore(root=tmp_path / "responses", max_resource_bytes=2 * 1024 * 1024)
    organization = {"name": "gnosis", "title": "Gnosis"}
    owner.invoke(
        "ws",
        connection.id,
        EndpointRequest(operation_id="POST /organization", body=organization),
        resources,
    )
    listing = owner.invoke(
        "ws",
        connection.id,
        EndpointRequest(operation_id="GET /organization", parameters={"server": "local"}),
        resources,
    )
    assert json.loads(listing["preview"]) == ["gnosis"]
    descriptors, receipt_paths = [], []
    for name, data in [
        ("curves.csv", b"n,loss\n1,0.1\n"),
        ("report.md", b"# Results\n"),
        ("run.json", b'{"steps":3}'),
    ]:
        identifier = _artifact(app, tmp_path, name, data)
        with prepare_artifact_upload(
            app, "ws", connection, identifier, "gnosis", "optimization"
        ) as upload:
            result = upload_artifact(store, connection, upload, None)
        assert result["state"] == "verified" and not result["reused"]
        assert result["sha256"] == hashlib.sha256(data).hexdigest()
        assert CatalogPeer.objects[f"/s3/objects/gnosis/{result['key']}"] == data
        descriptors.append(result["resource"])
        receipt_path = tmp_path / f"{result['id']}.json"
        receipt_path.write_text(json.dumps(result), encoding="utf-8")
        receipt_paths.extend(["--receipt", str(receipt_path)])
    catalog = SkillCatalog(home=tmp_path / "skill-home", cwd=tmp_path)
    skill = catalog.resolve("work-with-ndp").skill
    assert skill is not None
    prepared = subprocess.run(
        [
            sys.executable,
            str(Path(skill.dir) / "scripts" / "prepare_dataset.py"),
            "--name",
            "to-optimization-dataset-1",
            "--title",
            "TO Optimization dataset 1",
            "--organization",
            "gnosis",
            *receipt_paths,
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    prepared_body = json.loads(prepared.stdout)
    assert len(prepared_body["evidence"]["uploads"]) == 3
    created = owner.invoke(
        "ws",
        connection.id,
        EndpointRequest(
            operation_id="POST /dataset",
            parameters={"server": "local"},
            body=prepared_body["dataset"],
        ),
        resources,
    )
    dataset_id = json.loads(created["preview"])["id"]
    readback = owner.invoke(
        "ws",
        connection.id,
        EndpointRequest(
            operation_id="GET /dataset/{dataset_id}", parameters={"dataset_id": dataset_id}
        ),
        resources,
    )
    dataset = json.loads(readback["preview"])
    assert len(CatalogPeer.datasets) == 1
    assert dataset["title"] == "TO Optimization dataset 1" and dataset["owner_org"] == "gnosis"
    assert dataset["private"] and dataset["resources"] == descriptors
    assert len(store.list("ndp_upload", EndpointUploadReceipt)) == 3


def test_lost_response_retry_reads_before_writing(tmp_path: Path, peer: str) -> None:
    app, store = _app(tmp_path), SourceStore(tmp_path / "ledger")
    owner = NDPEndpointService(store, "tester")
    connection = owner.connect("ws", EndpointConnectionInput(name="Lab", url=peer))
    artifact_id = _artifact(app, tmp_path, "result.txt", b"verified result")
    CatalogPeer.lose_response = True
    with prepare_artifact_upload(app, "ws", connection, artifact_id, "gnosis", "retry") as upload:
        with pytest.raises(ValueError, match="unverified"):
            upload_artifact(store, connection, upload, None)
    assert store.list("ndp_upload", EndpointUploadReceipt)[0].state == "unverified"
    with prepare_artifact_upload(app, "ws", connection, artifact_id, "gnosis", "retry") as upload:
        retry = upload_artifact(store, connection, upload, None)
    assert retry["state"] == "verified" and retry["reused"]
    assert CatalogPeer.writes == 1


def test_corrupt_readback_and_storage_denial_do_not_claim_success(
    tmp_path: Path, peer: str
) -> None:
    app, store = _app(tmp_path), SourceStore(tmp_path / "ledger")
    connection = NDPEndpointService(store, "tester").connect(
        "ws", EndpointConnectionInput(name="Lab", url=peer)
    )
    artifact_id = _artifact(app, tmp_path, "result.txt", b"result")
    CatalogPeer.deny = True
    with prepare_artifact_upload(app, "ws", connection, artifact_id, "gnosis", "denied") as upload:
        with pytest.raises(PermissionError, match="denied"):
            upload_artifact(store, connection, upload, None)
    assert CatalogPeer.writes == 0
    CatalogPeer.deny, CatalogPeer.corrupt = False, True
    with prepare_artifact_upload(app, "ws", connection, artifact_id, "gnosis", "corrupt") as upload:
        with pytest.raises(ValueError, match="unverified"):
            upload_artifact(store, connection, upload, None)
    assert all(row.state == "unverified" for row in store.list("ndp_upload", EndpointUploadReceipt))


def test_native_approval_pins_content_and_denial_sends_no_request(
    tmp_path: Path, peer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, store = _app(tmp_path), SourceStore(tmp_path / "ledger")
    owner = NDPEndpointService(store, "tester")
    connection = owner.connect("ws", EndpointConnectionInput(name="Lab", url=peer))
    identifier = _artifact(app, tmp_path, "result.txt", b"original")
    session = SimpleNamespace(id="session", workspace_id="ws")
    service = SimpleNamespace(store=store, ndp_endpoints=owner)
    monkeypatch.setattr(ndp_endpoint_tools, "_owner", lambda: (app, session, service))
    monkeypatch.setattr(ndp_endpoint_tools, "require_admission", lambda *_: None)
    app.state.pending_permission_gate = lambda *_: "deny"
    with pytest.raises(PermissionError, match="not approved"):
        ndp_endpoint_tools.connected_data_ndp_upload_artifact(connection.id, identifier, "gnosis")
    assert not CatalogPeer.objects and CatalogPeer.writes == 0
    decisions = []

    def approve(_name: str, args: dict[str, Any], _context: Any) -> str:
        decisions.append(args)
        (tmp_path / "result.txt").write_bytes(b"changed during approval")
        return "allow"

    app.state.pending_permission_gate = approve
    result = ndp_endpoint_tools.connected_data_ndp_upload_artifact(
        connection.id, identifier, "gnosis"
    )
    assert decisions[0]["sha256"] == hashlib.sha256(b"original").hexdigest()
    assert decisions[0]["artifact_id"] == identifier
    assert CatalogPeer.objects[f"/s3/objects/gnosis/{result['key']}"] == b"original"


@pytest.mark.parametrize("problem", ["other-workspace", "changed", "unsupported", "traversal"])
def test_invalid_selection_refuses_before_upload(tmp_path: Path, peer: str, problem: str) -> None:
    app, store = _app(tmp_path), SourceStore(tmp_path / "ledger")
    connection = NDPEndpointService(store, "tester").connect(
        "ws", EndpointConnectionInput(name="Lab", url=peer)
    )
    identifier = _artifact(
        app,
        tmp_path,
        "result.txt",
        b"result",
        workspace="other" if problem == "other-workspace" else "ws",
    )
    if problem == "changed":
        (tmp_path / "result.txt").write_bytes(b"different")
    if problem == "unsupported":
        connection.schema_document["paths"].pop("/s3/objects/{bucket_name}")
    with pytest.raises((ValueError, PermissionError)):
        with prepare_artifact_upload(
            app,
            "ws",
            connection,
            identifier,
            "gnosis",
            "../escape" if problem == "traversal" else "clio",
        ):
            pytest.fail("An invalid artifact/storage destination was accepted")
    assert CatalogPeer.writes == 0


@pytest.mark.parametrize("shared_route", [False, True])
def test_ssh_transport_streams_multipart_over_one_closed_channel(
    tmp_path: Path, peer: str, monkeypatch: pytest.MonkeyPatch, shared_route: bool
) -> None:
    """Exercise real HTTP framing over a test socket standing in for an SSH channel."""
    clients = []

    class SSHPeer:
        def __init__(self) -> None:
            self.closed = False
            clients.append(self)

        def get_transport(self) -> SSHPeer:
            return self

        def open_channel(
            self, kind: str, destination: tuple[str, int], origin: Any, timeout: float
        ) -> socket.socket:
            assert kind == "direct-tcpip" and destination[0] == "127.0.0.1"
            return socket.create_connection(destination, timeout)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(ndp_endpoint_transport, "connect_profile", lambda _: SSHPeer())
    route = SshRoute(
        host="compute.example",
        user="scientist",
        port=2222,
        jump_hosts=["login.example"],
        identity_file="D:/keys/science key",
    )
    seen: list[SshRoute] = []

    def connect_route(choice: SshRoute) -> SSHPeer:
        seen.append(choice)
        return SSHPeer()

    monkeypatch.setattr(ndp_endpoint_transport, "connect_route", connect_route)
    app, store = _app(tmp_path), SourceStore(tmp_path / "ledger")
    connection = NDPEndpointService(store, "tester").connect(
        "ws",
        EndpointConnectionInput(
            name="Lab",
            url=peer,
            ssh_profile="" if shared_route else "test-profile",
            ssh_route=route if shared_route else None,
        ),
    )
    identifier = _artifact(app, tmp_path, "result.csv", b"a,b\n1,2\n" * 200000)
    with prepare_artifact_upload(app, "ws", connection, identifier, "gnosis", "ssh") as upload:
        result = upload_artifact(store, connection, upload, None)
    assert result["state"] == "verified" and CatalogPeer.writes == 1
    assert clients and all(client.closed for client in clients)
    assert (seen and all(choice == route for choice in seen)) if shared_route else not seen
    assert result["ssh_route"] == (route.model_dump() if shared_route else None)
    reads = [headers for method, headers in CatalogPeer.requests if method == "GET"]
    assert reads and all(
        "transfer-encoding" not in {key.lower() for key in headers} for headers in reads
    )


def test_private_account_token_never_enters_publication_receipts(
    tmp_path: Path, peer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, store = _app(tmp_path), SourceStore(tmp_path / "ledger")
    connection = (
        NDPEndpointService(store, "tester")
        .connect("ws", EndpointConnectionInput(name="Lab", url=peer))
        .model_copy(update={"url": "https://ndp.test", "authentication": "ndp"})
    )
    data, stored, seen = b"private data", False, []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal stored
        assert request.url.host == "ndp.test"
        assert request.headers["Authorization"] == "Bearer private-test-token"
        seen.append(request.method)
        if request.method == "POST":
            assert data in request.read()
            stored = True
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, content=data) if stored else httpx.Response(404)

    monkeypatch.setattr(
        ndp_artifact_upload,
        "endpoint_client",
        lambda *_: httpx.Client(transport=httpx.MockTransport(respond)),
    )
    identifier = _artifact(app, tmp_path, "result.txt", data)
    with prepare_artifact_upload(app, "ws", connection, identifier, "gnosis", "private") as upload:
        result = upload_artifact(store, connection, upload, "private-test-token")
    assert seen == ["GET", "POST", "GET"] and result["state"] == "verified"
    assert "private-test-token" not in json.dumps(result)
    assert all(
        "private-test-token" not in row.model_dump_json()
        for row in store.list("ndp_upload", EndpointUploadReceipt)
    )


def test_ndp_authoring_reference_is_loadable_as_a_skill_resource(tmp_path: Path) -> None:
    """The agent discovers the procedure through the actual built-in skill catalog."""
    from clio_agent.gact.agents.skill_runtime import (
        SkillRuntime,
        build_load_skill_tool,
        effective_declared_skills,
    )
    from clio_agent.gact.catalog import _builtin_main_agent

    agent = _builtin_main_agent()
    catalog = SkillCatalog(home=tmp_path / "home", cwd=tmp_path)
    resolution = catalog.resolve("work-with-ndp")
    assert resolution.status == "resolved" and resolution.skill is not None
    body = read_skill_body(resolution.skill)
    assert "references/catalog-authoring.md" in body
    runtime = SkillRuntime(
        resolutions=catalog.resolve_declared(effective_declared_skills(agent, catalog))
    )
    tool = build_load_skill_tool(agent, runtime)
    root = Path(resolution.skill.dir)
    for reference in sorted(root.rglob("*")):
        if reference.suffix not in {".md", ".py", ".json", ".sse"} or reference.name == "SKILL.md":
            continue
        relative = reference.relative_to(root).as_posix()
        loaded = tool.func(skill_id="work-with-ndp", file=relative)
        assert reference.read_text(encoding="utf-8").strip() in loaded
    tools = {tool.name: tool for tool in ndp_endpoint_tools.build_ndp_endpoint_tools()}
    assert "artifact_id" in tools["connected_data_ndp_upload_artifact"].args
