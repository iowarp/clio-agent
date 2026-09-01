"""Server-mode CMF writes against a real HTTP server speaking the push contract.

The fake server is a real socket, not a mock: the POST contract
(``/api/mlmd_push``, JSON body, ``json_payload`` as a STRING) is asserted on the
bytes that actually crossed the wire, because that contract is the whole product
surface of deployment shape (a).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from clio_agent.gact.artifacts.provenance.cmf_document import (
    artifact_entry,
    build_push_document,
)
from clio_agent.gact.artifacts.provenance.cmf_reasons import CMFRefusal
from clio_agent.gact.artifacts.provenance.cmf_server_mode import (
    CMFServerConfig,
    CMFServerModeProvider,
    CMFServerPublisher,
    verify_push_document,
)
from clio_agent.gact.provenance.protocol import ProviderReceipt


class _Recorder:
    """What the fake server saw, and what it should answer next."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status_code = 200
        self.body: dict[str, Any] = {"status": "success"}


def _handler(recorder: _Recorder) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            recorder.requests.append(
                {
                    "path": self.path,
                    "content_type": self.headers.get("Content-Type"),
                    "body": json.loads(raw.decode("utf-8")),
                }
            )
            payload = json.dumps(recorder.body).encode("utf-8")
            self.send_response(recorder.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:
            return

    return Handler


@pytest.fixture
def cmf_server() -> Any:
    recorder = _Recorder()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(recorder))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    recorder.url = f"http://{host}:{port}"  # type: ignore[attr-defined]
    try:
        yield recorder
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _artifact_event(artifact_id: str, name: str, *, kind: str = "dataset") -> dict[str, Any]:
    return {
        "event_type": "artifact.created",
        "event_id": f"sem_{artifact_id}",
        "payload": {
            "artifact_id": artifact_id,
            "name": name,
            "version": 1,
            "kind": kind,
            "sha256": "a" * 64,
            "size_bytes": 12,
            "workspace_id": "ws_1",
            "producer": {"call_id": "call_1", "tool": "fs_apply_edit_write"},
        },
    }


def _transform_event(call_id: str, generated: list[str]) -> dict[str, Any]:
    return {
        "event_type": "artifact.transform.recorded",
        "event_id": f"sem_{call_id}",
        "payload": {
            "call_id": call_id,
            "session_id": "sess_1",
            "status": "success",
            "instrument": {"tool": "fs_apply_edit_write"},
            "generated": [{"artifact_id": item, "name": item} for item in generated],
        },
    }


def _config(url: str) -> CMFServerConfig:
    return CMFServerConfig(server_url=url, pipeline_name="clio-test", publish_timeout_s=10.0)


def test_push_hits_the_documented_endpoint_with_the_documented_body(cmf_server: Any) -> None:
    publisher = CMFServerPublisher(_config(cmf_server.url))
    publisher.record(_transform_event("call_1", ["artifact_1"]))
    publisher.record(_artifact_event("artifact_1", "a.csv"))
    result = publisher.publish()
    publisher.close()

    assert result["status"] == "success"
    assert len(cmf_server.requests) == 1
    request = cmf_server.requests[0]
    assert request["path"] == "/api/mlmd_push"
    assert "application/json" in str(request["content_type"])
    # Exactly the three documented keys, no more.
    assert set(request["body"]) == {"exec_uuid", "pipeline_name", "json_payload"}
    assert request["body"]["exec_uuid"] is None
    assert request["body"]["pipeline_name"] == "clio-test"
    # json_payload is a STRING containing the JSON, not a nested object.
    raw_payload = request["body"]["json_payload"]
    assert isinstance(raw_payload, str)
    document = json.loads(raw_payload)
    assert list(document) == ["Pipeline"]
    execution = document["Pipeline"][0]["stages"][0]["executions"][0]
    assert execution["name"] == "clio:call_1"
    assert execution["properties"]["Execution_uuid"] == "call_1"
    assert execution["events"][0]["artifact"]["uri"] == "clio://artifact/artifact_1"


def test_a_confirmed_push_clears_the_batch_so_pushes_are_incremental(cmf_server: Any) -> None:
    publisher = CMFServerPublisher(_config(cmf_server.url))
    publisher.record(_transform_event("call_1", ["artifact_1"]))
    publisher.record(_artifact_event("artifact_1", "a.csv"))
    publisher.publish()
    assert publisher.pending == 0

    publisher.record(_transform_event("call_2", ["artifact_2"]))
    publisher.record(_artifact_event("artifact_2", "b.csv"))
    publisher.publish()
    publisher.close()

    second = json.loads(cmf_server.requests[1]["body"]["json_payload"])
    names = [execution["name"] for execution in second["Pipeline"][0]["stages"][0]["executions"]]
    assert names == ["clio:call_2"], "the second push must not resend the first batch"


def test_re_pushing_the_same_call_id_is_idempotent_by_named_execution(cmf_server: Any) -> None:
    """A named execution is merged by (Context_Type, name), never duplicated.

    The server skips its uuid-intersection filter for named executions, so the
    re-push is not discarded as "exists" either -- new artifacts attach to the
    same execution row. That is what makes per-batch pushes safe to repeat.
    """
    publisher = CMFServerPublisher(_config(cmf_server.url))
    publisher.record(_transform_event("call_1", ["artifact_1"]))
    publisher.record(_artifact_event("artifact_1", "a.csv"))
    publisher.publish()
    # The same call reported again with a second artifact.
    publisher.record(_transform_event("call_1", ["artifact_2"]))
    publisher.record(_artifact_event("artifact_2", "b.csv"))
    publisher.publish()
    publisher.close()

    documents = [json.loads(item["body"]["json_payload"]) for item in cmf_server.requests]
    for document in documents:
        executions = document["Pipeline"][0]["stages"][0]["executions"]
        assert [execution["name"] for execution in executions] == ["clio:call_1"]
        assert executions[0]["properties"]["Execution_uuid"] == "call_1"
    assert (
        documents[1]["Pipeline"][0]["stages"][0]["executions"][0]["events"][0]["artifact"]["uri"]
        == "clio://artifact/artifact_2"
    )


def test_server_reporting_exists_is_accepted_as_an_idempotent_repush(cmf_server: Any) -> None:
    cmf_server.body = {"status": "exists"}
    publisher = CMFServerPublisher(_config(cmf_server.url))
    publisher.record(_transform_event("call_1", ["artifact_1"]))
    publisher.record(_artifact_event("artifact_1", "a.csv"))
    assert publisher.publish()["status"] == "exists"
    publisher.close()


def test_unreachable_server_refuses_with_the_typed_reason() -> None:
    # Port 1 on loopback: nothing listens, connection is refused immediately.
    publisher = CMFServerPublisher(
        CMFServerConfig(server_url="http://127.0.0.1:1", publish_timeout_s=2.0)
    )
    publisher.record(_artifact_event("artifact_1", "a.csv"))
    with pytest.raises(CMFRefusal) as excinfo:
        publisher.publish()
    assert excinfo.value.reason == "cmf_server_unreachable"
    assert excinfo.value.payload["details"]["server_url"] == "http://127.0.0.1:1"
    publisher.close()


def test_a_refused_push_keeps_the_batch_for_the_next_attempt() -> None:
    publisher = CMFServerPublisher(
        CMFServerConfig(server_url="http://127.0.0.1:1", publish_timeout_s=2.0)
    )
    publisher.record(_artifact_event("artifact_1", "a.csv"))
    with pytest.raises(CMFRefusal):
        publisher.publish()
    assert publisher.pending == 1, "a failed push must not silently drop the records"
    publisher.close()


def test_version_update_maps_to_the_version_incompatible_reason(cmf_server: Any) -> None:
    cmf_server.status_code = 422
    cmf_server.body = {"detail": "version_update"}
    publisher = CMFServerPublisher(_config(cmf_server.url))
    publisher.record(_artifact_event("artifact_1", "a.csv"))
    with pytest.raises(CMFRefusal) as excinfo:
        publisher.publish()
    assert excinfo.value.reason == "cmf_server_version_incompatible"
    publisher.close()


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (400, {"detail": "Invalid JSON payload. The pipeline name is missing."}),
        (500, {"detail": "Internal Server Error"}),
        (200, {"status": "pipeline_not_exist"}),
    ],
)
def test_every_other_refusal_maps_to_rejected_payload(
    cmf_server: Any, status_code: int, body: dict[str, Any]
) -> None:
    cmf_server.status_code = status_code
    cmf_server.body = body
    publisher = CMFServerPublisher(_config(cmf_server.url))
    publisher.record(_artifact_event("artifact_1", "a.csv"))
    with pytest.raises(CMFRefusal) as excinfo:
        publisher.publish()
    assert excinfo.value.reason == "cmf_server_rejected_payload"
    assert excinfo.value.payload["details"]["status_code"] == status_code
    publisher.close()


def test_an_unrepresentable_kind_never_reaches_the_wire(cmf_server: Any) -> None:
    """The server would drop it silently and still answer success."""
    publisher = CMFServerPublisher(_config(cmf_server.url))
    with pytest.raises(CMFRefusal) as excinfo:
        publisher.record(_artifact_event("artifact_1", "m.json", kind="metrics"))
    assert excinfo.value.reason == "cmf_artifact_kind_not_representable"
    assert cmf_server.requests == []
    publisher.close()


def test_verification_refuses_a_type_the_server_would_drop_in_its_else_branch() -> None:
    event = _artifact_event("artifact_1", "a.csv")
    entry = artifact_entry(event, event["payload"])
    document = build_push_document(
        pipeline_name="clio-test",
        artifacts={"artifact_1": entry},
        executions=[],
    )
    # Sabotage: the exact shape handle_event's `else: pass` swallows.
    artifact = document["Pipeline"][0]["stages"][0]["executions"][0]["events"][0]["artifact"]
    artifact["type"] = "Dataslice"
    with pytest.raises(CMFRefusal) as excinfo:
        verify_push_document(document)
    assert excinfo.value.reason == "cmf_artifact_kind_not_representable"


def test_verification_refuses_a_model_whose_uri_property_was_lost() -> None:
    event = _artifact_event("artifact_1", "m.pkl", kind="model")
    entry = artifact_entry(event, event["payload"])
    document = build_push_document(
        pipeline_name="clio-test", artifacts={"artifact_1": entry}, executions=[]
    )
    artifact = document["Pipeline"][0]["stages"][0]["executions"][0]["events"][0]["artifact"]
    del artifact["properties"]["uri"]
    with pytest.raises(CMFRefusal) as excinfo:
        verify_push_document(document)
    assert excinfo.value.reason == "cmf_server_rejected_payload"


def test_verification_refuses_an_execution_without_execution_uuid() -> None:
    event = _artifact_event("artifact_1", "a.csv")
    entry = artifact_entry(event, event["payload"])
    document = build_push_document(
        pipeline_name="clio-test", artifacts={"artifact_1": entry}, executions=[]
    )
    execution = document["Pipeline"][0]["stages"][0]["executions"][0]
    execution["properties"]["Execution_uuid"] = ""
    with pytest.raises(CMFRefusal) as excinfo:
        verify_push_document(document)
    assert excinfo.value.reason == "cmf_server_version_incompatible"


class _Event:
    """Minimal SemanticEvent stand-in exposing the dispatcher's contract."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.event_type = payload["event_type"]

    def to_dict(self, _mode: str) -> dict[str, Any]:
        return self._payload


def test_provider_accepts_only_after_the_server_confirms(cmf_server: Any) -> None:
    provider = CMFServerModeProvider(_config(cmf_server.url), store=object())
    receipt = provider.emit(_Event(_transform_event("call_1", ["artifact_1"])))
    assert receipt is ProviderReceipt.ACCEPTED
    assert len(cmf_server.requests) == 1
    provider.close()


def test_provider_reports_an_annotation_event_as_filtered_not_written(cmf_server: Any) -> None:
    """CMF's push document has no update verb for these -- saying ACCEPTED
    would count a write that never happened."""
    provider = CMFServerModeProvider(_config(cmf_server.url), store=object())
    receipt = provider.emit(
        _Event({"event_type": "artifact.enriched", "payload": {"artifact_id": "artifact_1"}})
    )
    assert receipt is ProviderReceipt.FILTERED
    assert cmf_server.requests == []
    provider.close()


def test_provider_lineage_without_a_reader_is_the_typed_capability_gap(cmf_server: Any) -> None:
    provider = CMFServerModeProvider(_config(cmf_server.url), store=object())
    with pytest.raises(CMFRefusal) as excinfo:
        provider.lineage("artifact_1", direction="both", depth=3)
    assert excinfo.value.reason == "cmf_lineage_query_unavailable"
    provider.close()


def test_pending_records_are_bounded_and_eviction_is_reported() -> None:
    publisher = CMFServerPublisher(
        CMFServerConfig(
            server_url="http://127.0.0.1:1", publish_timeout_s=2.0, max_pending_records=4
        )
    )
    for index in range(10):
        publisher.record(_transform_event(f"call_{index}", []))
    assert publisher.pending <= 4
    with pytest.raises(CMFRefusal) as excinfo:
        publisher.publish()
    # The loss is carried in the refusal, never dropped silently.
    assert excinfo.value.payload["details"]["evicted_records"] == 6
    publisher.close()


def test_server_config_rejects_an_empty_url_at_construction() -> None:
    with pytest.raises(ValueError, match="server_url must not be empty"):
        CMFServerConfig(server_url="  ")


def test_push_url_is_built_from_the_declared_server_url() -> None:
    assert CMFServerConfig(server_url="http://host:8080/").push_url == (
        "http://host:8080/api/mlmd_push"
    )
