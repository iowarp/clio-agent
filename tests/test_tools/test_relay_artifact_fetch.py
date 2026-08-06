"""#1200 demo slice: bounded transfer of a remote relay artifact into the workspace."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from clio_agent.gact.artifacts.designation import (
    ARTIFACT_SUFFIXES,
    kind_for_path,
    result_declared_paths,
)
from clio_agent.gact.artifacts.transform_edges import detect_authority_edges
from clio_agent.tools.gateway import build_gateway
from clio_agent.tools.relay_artifact_fetch import (
    RELAY_FETCH_ORIGIN_SCHEMA,
    RelayArtifactFetchError,
    fetch_relay_artifact,
)

CLUSTER = "ares-p5run2"
JOB = "job_e886ccc32e0d4d6e8b29c59d529b94c4"
ARTIFACT = "artifact_961ee83755c74584a60dd50d7bfcf04d"
CONTENT = b"ares-comp-27.example\n"
REMOTE_URI = (
    f"file:///mnt/common/jcernudagarcia/.local/share/clio-relay/p5run2/relay-spool/{JOB}/stdout.log"
)


def _record(**overrides: Any) -> dict[str, Any]:
    """One relay job artifact record, shaped as the live index returns it."""

    record = {
        "artifact_id": ARTIFACT,
        "job_id": JOB,
        "sequence": 3,
        "uri": REMOTE_URI,
        "kind": "stdout",
        "size_bytes": len(CONTENT),
        "sha256": hashlib.sha256(CONTENT).hexdigest(),
        "created_at": "2026-08-06T19:23:11.868181Z",
    }
    record.update(overrides)
    return record


class _FakeRelay(AbstractAsyncContextManager["_FakeRelay"]):
    def __init__(
        self,
        *,
        records: list[dict[str, Any]] | None = None,
        payload: bytes = CONTENT,
    ) -> None:
        self.records = records if records is not None else [_record()]
        self.payload = payload
        self.fetched: list[str] = []

    async def __aenter__(self) -> "_FakeRelay":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    async def list_job_artifacts(self, job_id: str) -> list[dict[str, Any]]:
        assert job_id == JOB
        return [dict(record) for record in self.records]

    async def fetch_artifact(self, artifact_id: str) -> bytes:
        self.fetched.append(artifact_id)
        return self.payload


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Bind a session workspace root the way a live gact turn does."""

    from clio_agent.tools import execution

    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(root))
    token = execution._ACTIVE_TOOL_WORKSPACE_ROOT.set(str(root))
    try:
        yield root
    finally:
        execution._ACTIVE_TOOL_WORKSPACE_ROOT.reset(token)


@pytest.mark.asyncio
async def test_under_cap_fetch_writes_the_file_and_names_its_remote_origin(
    workspace: Path,
) -> None:
    """FAILING-FIRST (#1200): completed-execution output content was unreachable.

    Under the cap the bytes land in the session workspace and the result names
    the local path, the verified size and digest, and the remote reference the
    custody came from -- never the file content itself.
    """

    relay = _FakeRelay()

    result = await fetch_relay_artifact(
        lambda: relay,
        {"job_id": JOB, "artifact_id": ARTIFACT},
        cluster_hint=CLUSTER,
    )

    landed = Path(result["local_path"])
    assert landed.parent == workspace
    assert landed.name == "stdout.log"
    assert landed.read_bytes() == CONTENT
    assert result["size_bytes"] == len(CONTENT)
    assert result["sha256"] == hashlib.sha256(CONTENT).hexdigest()

    origin = result["origin"]
    assert origin["schema_version"] == RELAY_FETCH_ORIGIN_SCHEMA
    assert origin["cluster"] == CLUSTER
    assert origin["job_id"] == JOB
    assert origin["artifact_id"] == ARTIFACT
    assert origin["uri"] == REMOTE_URI
    assert origin["remote_size_bytes"] == len(CONTENT)
    assert origin["transferred_by"] == "relay_fetch_artifact"
    # The model lane never carries content.
    assert "content" not in result
    assert CONTENT.decode() not in str(result)


@pytest.mark.asyncio
async def test_oversize_artifact_is_refused_before_any_download(workspace: Path) -> None:
    """FAILING-FIRST (#1200): a gigabyte output must never be transferred by accident.

    The size comes from relay's LISTING, so the refusal happens before a single
    byte moves -- proven by the fake relay recording no fetch at all. The typed
    reason carries the size and the remote reference so the agent can report
    where the data lives.
    """

    relay = _FakeRelay(records=[_record(size_bytes=2 * 1024 * 1024 * 1024)])

    with pytest.raises(RelayArtifactFetchError) as raised:
        await fetch_relay_artifact(
            lambda: relay,
            {"job_id": JOB, "artifact_id": ARTIFACT},
            cluster_hint=CLUSTER,
        )

    assert raised.value.reason == "relay_fetch_artifact_too_large"
    details = raised.value.details
    assert details["size_bytes"] == 2 * 1024 * 1024 * 1024
    assert details["max_bytes"] == 100 * 1024 * 1024
    assert details["origin"]["artifact_id"] == ARTIFACT
    assert details["origin"]["cluster"] == CLUSTER
    # No download was started and nothing was written.
    assert relay.fetched == []
    assert list(workspace.iterdir()) == []


@pytest.mark.asyncio
async def test_configured_cap_is_honored_and_a_bad_cap_is_refused(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The limit is one config knob, and an unusable value is never substituted."""

    monkeypatch.setenv("CLIO_RELAY_FETCH_MAX_BYTES", "8")
    relay = _FakeRelay()
    with pytest.raises(RelayArtifactFetchError) as raised:
        await fetch_relay_artifact(lambda: relay, {"job_id": JOB, "artifact_id": ARTIFACT})
    assert raised.value.reason == "relay_fetch_artifact_too_large"
    assert raised.value.details["max_bytes"] == 8

    monkeypatch.setenv("CLIO_RELAY_FETCH_MAX_BYTES", "0")
    with pytest.raises(RelayArtifactFetchError) as raised:
        await fetch_relay_artifact(lambda: relay, {"job_id": JOB, "artifact_id": ARTIFACT})
    assert raised.value.reason == "relay_fetch_limit_invalid"
    assert relay.fetched == []


@pytest.mark.asyncio
async def test_wrong_inputs_and_corrupt_transfers_stay_typed(workspace: Path) -> None:
    """Every refusal path is typed, and a failed verification writes nothing."""

    missing = _FakeRelay(records=[_record(artifact_id="artifact_other")])
    with pytest.raises(RelayArtifactFetchError) as raised:
        await fetch_relay_artifact(lambda: missing, {"job_id": JOB, "artifact_id": ARTIFACT})
    assert raised.value.reason == "relay_fetch_artifact_not_indexed"

    sizeless = _FakeRelay(records=[_record(size_bytes=None)])
    with pytest.raises(RelayArtifactFetchError) as raised:
        await fetch_relay_artifact(lambda: sizeless, {"job_id": JOB, "artifact_id": ARTIFACT})
    assert raised.value.reason == "relay_fetch_size_unknown"
    assert sizeless.fetched == []

    truncated = _FakeRelay(payload=CONTENT[:-3])
    with pytest.raises(RelayArtifactFetchError) as raised:
        await fetch_relay_artifact(lambda: truncated, {"job_id": JOB, "artifact_id": ARTIFACT})
    assert raised.value.reason == "relay_fetch_size_mismatch"

    tampered = _FakeRelay(records=[_record(sha256="0" * 64)])
    with pytest.raises(RelayArtifactFetchError) as raised:
        await fetch_relay_artifact(lambda: tampered, {"job_id": JOB, "artifact_id": ARTIFACT})
    assert raised.value.reason == "relay_fetch_digest_mismatch"

    escaping = _FakeRelay()
    with pytest.raises(RelayArtifactFetchError) as raised:
        await fetch_relay_artifact(
            lambda: escaping,
            {"job_id": JOB, "artifact_id": ARTIFACT, "target_filename": "../escape.txt"},
        )
    assert raised.value.reason == "relay_fetch_target_invalid"

    # Not one of those attempts left a file behind.
    assert list(workspace.iterdir()) == []


@pytest.mark.asyncio
async def test_fetched_file_is_designated_and_carries_a_transfer_used_edge(
    workspace: Path,
) -> None:
    """The transfer must reach the artifact record AND the provenance graph.

    Two existing seams do that work, and both need something from this result:
    the designation channel needs a top-level ``local_path`` with a recognized
    suffix, and the authority-edge detector needs the ``origin`` block to build
    the used-edge naming the cluster the bytes were produced on.
    """

    relay = _FakeRelay()
    result = await fetch_relay_artifact(
        lambda: relay, {"job_id": JOB, "artifact_id": ARTIFACT}, cluster_hint=CLUSTER
    )

    # A fetched run log is a citeable output, so its suffix must be designated.
    assert ".log" in ARTIFACT_SUFFIXES
    assert result_declared_paths(result) == {"local_path": result["local_path"]}
    assert kind_for_path(result["local_path"]).value == "report"

    scan = detect_authority_edges(
        None, tool_name="relay_fetch_artifact", result=result, workspace_id="ws"
    )
    assert len(scan.edges) == 1
    edge = scan.edges[0]
    assert edge.role.value == "used"
    assert edge.evidence.value == "authority"
    assert edge.authority == REMOTE_URI
    assert edge.external_ref == f"external:relay://{CLUSTER}/{JOB}/{ARTIFACT}"
    assert edge.sha256 == hashlib.sha256(CONTENT).hexdigest()
    assert edge.path == result["local_path"]
    assert edge.note == "relay_fetch_artifact"


@pytest.mark.asyncio
async def test_tool_registers_under_relay_with_title_and_cluster_hint() -> None:
    """The tool is reachable as relay_fetch_artifact and states its cluster."""

    from mcp.types import Tool as McpTool

    from clio_agent.tools.relay_transport import RelayRemoteMcpCatalog
    from clio_agent.tools.remote_mcp import RemoteMcpFederation

    catalog = RelayRemoteMcpCatalog(
        revision="a" * 64,
        tools={},
        follow_tools={
            "relay_wait": McpTool(
                name="relay_wait",
                inputSchema={"type": "object", "properties": {"job_id": {"type": "string"}}},
            )
        },
    )
    federation = RemoteMcpFederation(catalog, lambda: _FakeRelay(), cluster_hint=CLUSTER)
    gateway = build_gateway({}, remote_mcp_federation=federation)

    async with Client(gateway) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    fetch = listed["relay_fetch_artifact"]
    assert fetch.title == "Fetch Artifact"
    assert f"This deployment's registered cluster is {CLUSTER!r}." in (fetch.description or "")
    assert set(fetch.input_schema["required"]) == {"job_id", "artifact_id"}
    assert fetch.input_schema["additionalProperties"] is False
    assert isinstance(fetch.output_schema, Mapping)
    assert "content" not in fetch.output_schema["properties"]


@pytest.mark.asyncio
async def test_every_projected_relay_tool_has_a_plain_paren_free_title() -> None:
    """The UI head must read as a name, not as a wire identifier.

    Without a title the head falls back to the raw tool name
    (``jarvis_add_step``). Titles carry no parentheses because the surrounding
    UI injects the call's arguments itself.
    """

    from mcp.types import Tool as McpTool

    from clio_agent.tools.jarvis_jobs import JarvisJobs
    from clio_agent.tools.relay_transport import RelayRemoteMcpCatalog
    from clio_agent.tools.remote_mcp import RemoteMcpFederation

    def _relay_tool(name: str) -> McpTool:
        return McpTool(
            name=name,
            inputSchema={"type": "object", "properties": {"job_id": {"type": "string"}}},
        )

    catalog = RelayRemoteMcpCatalog(
        revision="b" * 64,
        tools={},
        follow_tools={
            "relay_observe": _relay_tool("relay_observe"),
            "relay_wait": _relay_tool("relay_wait"),
        },
    )
    gateway = build_gateway(
        {},
        remote_mcp_federation=RemoteMcpFederation(
            catalog, lambda: _FakeRelay(), cluster_hint=CLUSTER
        ),
        jarvis_jobs=JarvisJobs(lambda: _FakeRelay(), cluster_hint=CLUSTER),
    )

    async with Client(gateway) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}

    expected = {
        "jarvis_create_pipeline": "Create Pipeline",
        "jarvis_describe": "Describe",
        "jarvis_add_step": "Add Step",
        "jarvis_edit_step": "Edit Step",
        "jarvis_run": "Run Pipeline",
        "jarvis_get_execution": "Get Execution",
        "relay_observe": "Observe Job",
        "relay_wait": "Wait For Job",
        "relay_fetch_artifact": "Fetch Artifact",
    }
    for name, title in expected.items():
        assert listed[name].title == title, name
        assert "(" not in (listed[name].title or ""), name
        assert ")" not in (listed[name].title or ""), name
