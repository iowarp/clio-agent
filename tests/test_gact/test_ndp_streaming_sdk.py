"""SDK checkpoints retain real capture custody and survive restarts and races."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.resource_custody import ResourceStore
from clio_agent.gact.storage.ndp_endpoint_models import EndpointConnection, EndpointRequest
from clio_agent.gact.storage.ndp_endpoint_operations import connection_operations, resolve_request
from clio_agent.gact.storage.ndp_endpoints import NDPEndpointService
from clio_agent.gact.storage.ndp_presentations import endpoint_presentation
from clio_agent.gact.storage.ndp_streaming_models import PREFIX
from clio_agent.gact.storage.store import SourceStore


@pytest.fixture
def bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    """Use the real durable service and custody with one test-only Kafka peer."""
    owner = NDPEndpointService(SourceStore(tmp_path / "ledger"), "owner")
    resources = ResourceStore(root=tmp_path / "resources", max_resource_bytes=4 * 1024 * 1024)
    connection = EndpointConnection(
        id="endpoint",
        workspace_id="workspace",
        principal="owner",
        name="NDP",
        url="https://ndp.example",
        authentication="ndp",
        observed_at="now",
        schema_document={
            "paths": {
                "/status/kafka-details": {"get": {}},
                "/streams": {"get": {}},
                "/streams/{id}": {"delete": {}},
            }
        },
        streaming_runtime={"python": "/sdk/bin/python", "profile_path": "/private/broker.json"},
    )
    owner.store.put("ndp_endpoint", connection.id, connection)
    calls: list[dict[str, Any]] = []

    def peer(record: Any, request: dict[str, Any], token: Any, seconds: float) -> dict[str, Any]:
        calls.append(request)
        if request["action"] == "open":
            return {
                "topic": "observations",
                "positions": {"0": 0},
                "earliest_positions": {"0": 0},
                "latest_positions": {"0": 575},
                "topic_identity": "topic-generation",
                "history": "Retained broker records only",
            }
        start = request["positions"]["0"]
        count = min(request["max_records"], 575 - start)
        rows = [
            {
                "topic": "observations",
                "partition": 0,
                "offset": offset,
                "value": {"measured": offset * 2},
            }
            for offset in range(start, start + count)
        ]
        return {
            "records": rows,
            "count": count,
            "next_positions": {"0": start + count},
            "earliest_positions": {"0": 0},
            "latest_positions": {"0": 575},
            "topic_identity": "topic-generation",
            "complete": True,
        }

    monkeypatch.setattr("clio_agent.gact.storage.ndp_streaming_service.run_sdk", peer)
    return owner, resources, connection, calls, peer


def invoke(bound: tuple[Any, ...], action: str, **parameters: Any) -> dict[str, Any]:
    """Exercise the same operation entry point used by the agent and HTTP UI."""
    owner, resources, connection, *_ = bound
    return owner.invoke(
        connection.workspace_id,
        connection.id,
        EndpointRequest(operation_id=PREFIX + action, parameters=parameters, max_events=100),
        resources,
        "private-account-token",
    )


def test_sdk_capture_has_an_honest_inspectable_output(bound: tuple[Any, ...]) -> None:
    """SDK results use the normal input/body/custody presentation without claiming HTTP."""
    result = invoke(bound, "open", resource_id="resource", start="earliest")
    view = endpoint_presentation({}, result, None)
    assert view is not None
    assert "SDK result" in view["summary"]
    receipt = next(block for block in view["blocks"] if block["id"] == "receipt")
    assert "streaming_v2 SDK completed" in receipt["text"]
    assert "HTTP 200" not in receipt["text"]
    assert {"parameters", "response-preview", "saved-response", "receipt"} <= {
        block["id"] for block in view["blocks"]
    }


def test_sdk_executes_the_approved_runtime_snapshot(
    bound: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An endpoint edit after approval cannot substitute a different executable or profile."""
    owner, resources, connection, _, peer = bound
    replacement = connection.model_copy(update={"streaming_runtime": None})
    owner.store.put("ndp_endpoint", connection.id, replacement)

    def checked(record: Any, request: Any, token: Any, seconds: Any) -> Any:
        assert record.streaming_runtime == connection.streaming_runtime
        return peer(record, request, token, seconds)

    monkeypatch.setattr("clio_agent.gact.storage.ndp_streaming_service.run_sdk", checked)
    result = owner.invoke(
        connection.workspace_id,
        connection.id,
        EndpointRequest(operation_id=PREFIX + "open", parameters={"resource_id": "resource"}),
        resources,
        "private-account-token",
        approved_connection=connection,
    )
    assert result["transport"] == "streaming_v2"


def test_sdk_operations_are_explicit_and_do_not_claim_http_support(bound: tuple[Any, ...]) -> None:
    """Configured readers use SDK identities; unrelated observed API operations remain."""
    _, _, connection, *_ = bound
    rows = connection_operations(connection)
    assert [row["id"] for row in rows if row["method"] == "SDK"] == [
        PREFIX + action for action in ("open", "read", "list", "stop", "resume")
    ]
    assert not any(row["path"].startswith("/streams") for row in rows)
    assert any(row["id"] == "GET /status/kafka-details" for row in rows)
    with pytest.raises(ValueError, match="Refresh Endpoint"):
        resolve_request(connection, EndpointRequest(operation_id="GET /streams"))
    with pytest.raises(ValueError, match="does not match"):
        resolve_request(
            connection,
            EndpointRequest(operation_id=PREFIX + "open", parameters={"resource_id": "bad/id"}),
        )


def test_restart_retains_positions_and_same_cursor_returns_same_capture(
    bound: tuple[Any, ...],
) -> None:
    """Retries return the exact retained receipt; restart continues without replay."""
    owner, resources, connection, calls, _ = bound
    opened = invoke(bound, "open", resource_id="source")["stream"]
    first = invoke(bound, "read", stream_id=opened["id"], cursor=opened["cursor"])
    assert first["continuation"]["next_positions"] == {"0": 100}
    assert first["method"] == "SDK" and first["events"] == 100
    retained = resources.get(connection.workspace_id, first["resource_id"])
    assert retained is not None and retained.sha256 == first["sha256"]
    restarted = NDPEndpointService(SourceStore(owner.store.root), "owner")
    repeated = restarted.invoke(
        connection.workspace_id,
        connection.id,
        EndpointRequest(
            operation_id=PREFIX + "read",
            parameters={"stream_id": opened["id"], "cursor": opened["cursor"]},
        ),
        resources,
        "private-account-token",
    )
    assert repeated == first and len(calls) == 2
    second = restarted.invoke(
        connection.workspace_id,
        connection.id,
        EndpointRequest(operation_id=PREFIX + "read", parameters={"stream_id": opened["id"]}),
        resources,
        "private-account-token",
    )
    assert second["continuation"]["next_positions"] == {"0": 200}
    assert calls[-1]["positions"] == {"0": 100}


def test_stop_resume_preserves_position_instead_of_sdk_history_replay(
    bound: tuple[Any, ...],
) -> None:
    """Controls change CLIO's logical reader, independently of the source producer."""
    opened = invoke(bound, "open", resource_id="source")["stream"]
    invoke(bound, "read", stream_id=opened["id"])
    stopped = invoke(bound, "stop", stream_id=opened["id"])
    assert stopped["stream"]["state"] == "stopped"
    with pytest.raises(ValueError, match="stopped"):
        invoke(bound, "read", stream_id=opened["id"])
    invoke(bound, "resume", stream_id=opened["id"])
    captured = invoke(bound, "read", stream_id=opened["id"])
    assert captured["continuation"]["next_positions"] == {"0": 200}


def test_full_history_and_empty_poll_rotate_retriable_checkpoints(bound: tuple[Any, ...]) -> None:
    """An empty poll is repeatable without permanently preventing future reads."""
    opened = invoke(bound, "open", resource_id="source")["stream"]
    counts = [invoke(bound, "read", stream_id=opened["id"])["events"] for _ in range(6)]
    assert counts == [100, 100, 100, 100, 100, 75]
    empty = invoke(bound, "read", stream_id=opened["id"])
    assert empty["events"] == 0 and empty["continuation"]["next_positions"] == {"0": 575}
    later = invoke(bound, "read", stream_id=opened["id"])
    assert empty["continuation"]["next_cursor"] != later["continuation"]["next_cursor"]


def test_reader_is_bound_to_workspace_owner_and_runtime(bound: tuple[Any, ...]) -> None:
    """A changed destination or copied ID cannot repurpose a prior checkpoint."""
    owner, resources, connection, *_ = bound
    opened = invoke(bound, "open", resource_id="source")["stream"]
    with pytest.raises(KeyError):
        owner.invoke(
            "other-workspace",
            connection.id,
            EndpointRequest(operation_id=PREFIX + "read", parameters={"stream_id": opened["id"]}),
            resources,
            "token",
        )
    altered = connection.model_copy(update={"url": "https://other.example"})
    owner.store.put("ndp_endpoint", connection.id, altered)
    with pytest.raises(ValueError, match="runtime changed"):
        invoke(bound, "read", stream_id=opened["id"])


def test_stop_during_capture_does_not_wait_for_poll_or_advance_checkpoint(
    bound: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending read holds no ledger lock and cannot overwrite a later stop."""
    owner, _, connection, _, peer = bound
    opened = invoke(bound, "open", resource_id="source")["stream"]
    polling, release = threading.Event(), threading.Event()

    def blocked(*args: Any) -> dict[str, Any]:
        polling.set()
        assert release.wait(5)
        return peer(*args)

    monkeypatch.setattr("clio_agent.gact.storage.ndp_streaming_service.run_sdk", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(invoke, bound, "read", stream_id=opened["id"])
        try:
            assert polling.wait(3)
            assert invoke(bound, "stop", stream_id=opened["id"])["stream"]["state"] == "stopped"
        finally:
            release.set()
        with pytest.raises(ValueError, match="stopped, resumed or advanced"):
            future.result(timeout=5)
    reader = owner.streaming.readers(connection)[0]
    assert reader.positions == {"0": 0} and reader.state == "stopped"


def test_invalid_worker_offsets_are_rejected_without_advancing(
    bound: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not hide incomplete history or a failed client initialization."""
    owner, _, connection, _, peer = bound
    opened = invoke(bound, "open", resource_id="source")["stream"]

    def gap(*args: Any) -> dict[str, Any]:
        result = peer(*args)
        result["records"][0]["offset"] = 500
        return result

    monkeypatch.setattr("clio_agent.gact.storage.ndp_streaming_service.run_sdk", gap)
    with pytest.raises(ValueError, match="gap or duplicate"):
        invoke(bound, "read", stream_id=opened["id"])
    assert owner.streaming.readers(connection)[0].positions == {"0": 0}
