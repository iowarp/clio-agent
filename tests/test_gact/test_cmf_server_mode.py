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
from clio_agent.gact.provenance.dispatcher import ProvenanceDispatcher
from clio_agent.gact.provenance.protocol import ProviderReceipt


class _Recorder:
    """What the fake server saw, and what it should answer next."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status_code = 200
        self.body: dict[str, Any] = {"status": "success"}
        #: Execution names the server actually HOLDS. ``None`` means "an honest
        #: server holds what it was given"; a list overrides it, so a test can
        #: model the upstream defect -- 200 success, entities silently dropped.
        self.held: list[str] | None = None
        self.pushed_names: list[str] = []
        #: Status the confirmation read answers with.
        self.confirm_status = 200
        #: Event types the server holds per execution uuid.
        self.stored_events: dict[str, list[int]] = {}
        #: When True the server keeps executions but drops every event -- the
        #: upstream handle_event swallow, which a uuid-only confirmation misses.
        self.drop_events = False

    def pull_document(self, exec_uuid: Any) -> dict[str, Any]:
        """Answer /mlmd_pull in the real shape, honouring exec_uuid scoping."""
        executions = [
            {
                "name": f"clio:{uuid}",
                "properties": {"Execution_uuid": uuid},
                "events": [] if self.drop_events else [{"type": t} for t in types],
            }
            for uuid, types in self.stored_events.items()
            if uuid in self.holds() and (not exec_uuid or uuid == exec_uuid)
        ]
        return {"Pipeline": [{"stages": [{"executions": executions}]}]}

    def holds(self) -> list[str]:
        return self.pushed_names if self.held is None else self.held

    def pushes(self) -> list[dict[str, Any]]:
        """Only the metadata pushes, excluding both kinds of confirmation read."""
        return [
            item for item in self.requests if str(item.get("path", "")).endswith("/api/mlmd_push")
        ]


def _handler(recorder: _Recorder) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _respond(self, status: int, body: Any) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
            recorder.requests.append(
                {
                    "path": self.path,
                    "content_type": self.headers.get("Content-Type"),
                    "body": body,
                }
            )
            if self.path.endswith("/mlmd_pull"):
                self._respond(200, recorder.pull_document(body.get("exec_uuid")))
                return
            if recorder.status_code == 200:
                document = json.loads(body.get("json_payload") or "{}")
                for pipeline in document.get("Pipeline") or []:
                    for stage in pipeline.get("stages") or []:
                        for execution in stage.get("executions") or []:
                            uuid = str(
                                (execution.get("properties") or {}).get("Execution_uuid") or ""
                            )
                            recorder.pushed_names.append(uuid)
                            recorder.stored_events.setdefault(uuid, []).extend(
                                int(event.get("type") or 0)
                                for event in execution.get("events") or []
                            )
            self._respond(recorder.status_code, recorder.body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            recorder.requests.append({"path": self.path, "method": "GET"})
            if recorder.confirm_status != 200:
                self._respond(recorder.confirm_status, {"detail": "confirmation unavailable"})
                return
            if self.path.startswith("/api/executions-by-stage/"):
                # The REAL row shape a live cmf-server returns: no "name" key,
                # identity lives in an execution_properties {name, value} list.
                self._respond(
                    200,
                    {
                        "items": [
                            {
                                "execution_id": index,
                                "execution_properties": [
                                    {"name": "Execution_uuid", "value": uuid},
                                    {"name": "Context_Type", "value": "clio-test/artifacts"},
                                ],
                            }
                            for index, uuid in enumerate(recorder.holds())
                        ]
                    },
                )
                return
            self._respond(404, {"detail": "no route"})

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
    assert len(cmf_server.pushes()) == 1
    request = cmf_server.pushes()[0]
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

    second = json.loads(cmf_server.pushes()[1]["body"]["json_payload"])
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

    documents = [json.loads(item["body"]["json_payload"]) for item in cmf_server.pushes()]
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


def test_a_source_or_environment_ontology_is_pushed_not_refused(cmf_server: Any) -> None:
    """Sources and environments are artifacts too -- different ontology, same push.

    They ride as Dataset (the storage class) with their own kind preserved, so
    user-submitted sources are tracked the moment they exist.
    """
    publisher = CMFServerPublisher(_config(cmf_server.url))
    publisher.record(_artifact_event("artifact_src", "upload.csv", kind="source"))
    publisher.record(_artifact_event("artifact_env", "env.txt", kind="environment"))
    publisher.publish()
    publisher.close()

    document = json.loads(cmf_server.pushes()[0]["body"]["json_payload"])
    artifacts = [
        event["artifact"]
        for execution in document["Pipeline"][0]["stages"][0]["executions"]
        for event in execution["events"]
    ]
    assert {artifact["type"] for artifact in artifacts} == {"Dataset"}
    assert {artifact["custom_properties"]["clio_kind"] for artifact in artifacts} == {
        "source",
        "environment",
    }


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
    # The transform names an artifact, so it waits for that artifact's event.
    assert provider.emit(_Event(_transform_event("call_1", ["artifact_1"]))) is (
        ProviderReceipt.FILTERED
    )
    assert cmf_server.pushes() == [], "an unresolved edge must not be pushed"

    receipt = provider.emit(_Event(_artifact_event("artifact_1", "a.csv")))

    assert receipt is ProviderReceipt.ACCEPTED
    assert len(cmf_server.pushes()) == 1
    provider.close()


def test_provider_reports_an_annotation_event_as_filtered_not_written(cmf_server: Any) -> None:
    """CMF's push document has no update verb for these -- saying ACCEPTED
    would count a write that never happened."""
    provider = CMFServerModeProvider(_config(cmf_server.url), store=object())
    receipt = provider.emit(
        _Event({"event_type": "artifact.enriched", "payload": {"artifact_id": "artifact_1"}})
    )
    assert receipt is ProviderReceipt.FILTERED
    assert cmf_server.pushes() == []
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


# --------------------------------------------------------------------------- #
# Read-back confirmation: a 200 is not evidence. The cmf-server swallows
# per-entity ingest failures (cmf_merger.handle_execution wraps the write in
# `except Exception: logger.error(...)`), so it can answer success having
# stored nothing -- which is how a live run recorded 13 executions, zero
# artifacts and zero events against a green counter.
# --------------------------------------------------------------------------- #


def test_a_success_the_server_does_not_hold_is_a_typed_failure(cmf_server: Any) -> None:
    cmf_server.held = []  # 200 success, nothing stored: the upstream defect
    publisher = CMFServerPublisher(_config(cmf_server.url))
    publisher.record(_transform_event("call_1", ["artifact_1"]))
    publisher.record(_artifact_event("artifact_1", "a.csv"))

    with pytest.raises(CMFRefusal) as excinfo:
        publisher.publish()

    assert excinfo.value.reason == "cmf_server_discarded_entities"
    assert excinfo.value.payload["details"]["missing"] == ["call_1"]
    assert excinfo.value.payload["details"]["missing_count"] == 1
    publisher.close()


def test_discarded_entities_are_not_counted_as_accepted(cmf_server: Any) -> None:
    """The whole point: the counter cannot outrun what CMF actually holds."""
    cmf_server.held = []
    provider = CMFServerModeProvider(_config(cmf_server.url), store=object())
    dispatcher = ProvenanceDispatcher([provider], queue_size=4)

    dispatcher.emit(_Event(_transform_event("call_1", ["artifact_1"])))
    dispatcher.emit(_Event(_artifact_event("artifact_1", "a.csv")))
    dispatcher.flush()
    health = dispatcher.health()[0]

    assert health["queued"] == 2
    assert health["accepted"] == 0, "a 200 that stored nothing is not a write"
    # emit fails, then flush retries the still-pending batch and fails again.
    assert health["failed"] >= 1
    assert "cmf_server_discarded_entities" in health["last_error"]
    assert health["status"] == "degraded"


def test_a_push_the_server_really_holds_confirms_and_counts(cmf_server: Any) -> None:
    provider = CMFServerModeProvider(_config(cmf_server.url), store=object())
    dispatcher = ProvenanceDispatcher([provider], queue_size=4)

    dispatcher.emit(_Event(_transform_event("call_1", ["artifact_1"])))
    dispatcher.emit(_Event(_artifact_event("artifact_1", "a.csv")))
    dispatcher.flush()
    health = dispatcher.health()[0]
    dispatcher.close()

    assert health["accepted"] == 1
    assert health["failed"] == 0
    confirmations = [item for item in cmf_server.requests if item.get("method") == "GET"]
    assert confirmations, "the publisher must read back what it pushed"
    assert "/api/executions-by-stage/" in confirmations[0]["path"]


def test_the_confirmation_read_is_bounded(cmf_server: Any) -> None:
    """One bounded page, not a full pipeline pull."""
    publisher = CMFServerPublisher(_config(cmf_server.url))
    publisher.record(_transform_event("call_1", ["artifact_1"]))
    publisher.record(_artifact_event("artifact_1", "a.csv"))
    publisher.publish()
    publisher.close()

    get = next(item for item in cmf_server.requests if item.get("method") == "GET")
    assert "record_per_page=" in get["path"]
    assert "active_page=1" in get["path"]
    page_size = int(get["path"].split("record_per_page=")[1].split("&")[0])
    assert 0 < page_size <= 200


def test_an_unreadable_confirmation_does_not_silently_pass(cmf_server: Any) -> None:
    """A confirmation that cannot be performed is not a confirmation.

    The push itself succeeded, but without a read-back there is no evidence the
    entities are held, so the batch must not be counted or cleared.
    """
    cmf_server.confirm_status = 500
    publisher = CMFServerPublisher(_config(cmf_server.url))
    publisher.record(_transform_event("call_1", ["artifact_1"]))
    publisher.record(_artifact_event("artifact_1", "a.csv"))

    with pytest.raises(CMFRefusal) as excinfo:
        publisher.publish()

    assert excinfo.value.reason == "cmf_server_unreachable"
    assert publisher.pending == 2, "an unconfirmed batch stays pending"
    publisher.close()


def test_a_raw_backslash_never_reaches_the_wire(cmf_server: Any) -> None:
    """The exact upstream trigger, asserted on the bytes that would be sent."""
    publisher = CMFServerPublisher(_config(cmf_server.url))
    event = _artifact_event("artifact_1", "a.csv")
    event["payload"]["path"] = r"D:\Libraries\Documents\a.csv"
    publisher.record(event)
    publisher.publish()
    publisher.close()

    document = json.loads(cmf_server.pushes()[0]["body"]["json_payload"])
    for pipeline in document["Pipeline"]:
        for stage in pipeline["stages"]:
            for execution in stage["executions"]:
                assert "\\" not in json.dumps(list(execution["properties"].values()))
                assert "\\" not in json.dumps(list(execution["custom_properties"].values()))


# --------------------------------------------------------------------------- #
# Cross-batch edges and event confirmation. Existence of an execution says
# nothing about its edges: handle_event has its own swallowing `except`, so an
# INPUT can vanish while the execution looks healthy.
# --------------------------------------------------------------------------- #


def test_an_input_from_an_earlier_batch_survives_the_round_trip(cmf_server: Any) -> None:
    """Create a5 in turn 1, use it in turn 2: the INPUT must reach the server."""
    publisher = CMFServerPublisher(_config(cmf_server.url))
    # Turn 1: mint the artifact and push it.
    publisher.record(_artifact_event("artifact_a5", "a5.csv"))
    publisher.publish()
    assert publisher.pending == 0

    # Turn 2: a transform consumes it. The batch no longer holds the artifact.
    publisher.record(_transform_event("call_turn2", []))
    publisher._executions[-1].used.append("artifact_a5")  # noqa: SLF001
    publisher.publish()
    publisher.close()

    second = json.loads(cmf_server.pushes()[1]["body"]["json_payload"])
    events = second["Pipeline"][0]["stages"][0]["executions"][0]["events"]
    assert [event["type"] for event in events] == [3], "the cross-batch INPUT was dropped"
    assert events[0]["artifact"]["uri"] == "clio://artifact/artifact_a5"
    # And the server confirms it holds that event.
    assert cmf_server.stored_events["call_turn2"] == [3]


def test_an_unknown_edge_reference_is_deferred_then_typed(cmf_server: Any) -> None:
    """Ordering is not load-bearing, but a dangling edge still goes loud.

    Artifact and transform events have no guaranteed order, so the first
    publishes hold the execution back rather than refusing it; a reference that
    never resolves becomes a typed refusal instead of deferring forever.
    """
    publisher = CMFServerPublisher(_config(cmf_server.url))
    publisher.record(_transform_event("call_1", []))
    publisher._executions[-1].used.append("artifact_never_seen")  # noqa: SLF001

    for _ in range(3):
        assert publisher.publish()["status"] == "deferred"
        assert publisher.pending == 1, "a deferred execution stays pending"

    with pytest.raises(CMFRefusal) as excinfo:
        publisher.publish()

    assert excinfo.value.reason == "cmf_artifact_reference_unresolved"
    assert excinfo.value.payload["details"]["artifact_ids"] == ["artifact_never_seen"]
    assert cmf_server.pushes() == [], "nothing may reach the wire"
    publisher.close()


def test_a_server_that_keeps_executions_but_drops_events_is_a_typed_failure(
    cmf_server: Any,
) -> None:
    """The handle_event swallow: uuid confirmation alone would call this green."""
    cmf_server.drop_events = True
    publisher = CMFServerPublisher(_config(cmf_server.url))
    publisher.record(_transform_event("call_1", ["artifact_1"]))
    publisher.record(_artifact_event("artifact_1", "a.csv"))

    with pytest.raises(CMFRefusal) as excinfo:
        publisher.publish()

    assert excinfo.value.reason == "cmf_server_discarded_entities"
    assert "missing events" in excinfo.value.payload["message"]
    assert publisher.pending == 2, "an unconfirmed batch stays pending"
    publisher.close()


def test_dropped_events_are_not_counted_as_accepted(cmf_server: Any) -> None:
    cmf_server.drop_events = True
    provider = CMFServerModeProvider(_config(cmf_server.url), store=object())
    dispatcher = ProvenanceDispatcher([provider], queue_size=4)

    dispatcher.emit(_Event(_transform_event("call_1", ["artifact_1"])))
    dispatcher.emit(_Event(_artifact_event("artifact_1", "a.csv")))
    dispatcher.flush()
    health = dispatcher.health()[0]

    assert health["accepted"] == 0, "an execution with no events is not a written batch"
    assert health["failed"] >= 1
    assert "cmf_server_discarded_entities" in health["last_error"]


def test_the_event_confirmation_scopes_its_pull_per_execution(cmf_server: Any) -> None:
    """Bounded: a small batch reads back one execution at a time."""
    publisher = CMFServerPublisher(_config(cmf_server.url))
    publisher.record(_transform_event("call_1", ["artifact_1"]))
    publisher.record(_artifact_event("artifact_1", "a.csv"))
    publisher.publish()
    publisher.close()

    pulls = [
        item for item in cmf_server.requests if str(item.get("path", "")).endswith("mlmd_pull")
    ]
    assert pulls, "events must be confirmed, not assumed"
    assert pulls[0]["body"]["exec_uuid"] == "call_1"


def test_an_execution_accumulating_more_edges_later_still_confirms(cmf_server: Any) -> None:
    """More held events than this batch expected is fine; fewer is not."""
    publisher = CMFServerPublisher(_config(cmf_server.url))
    publisher.record(_transform_event("call_1", ["artifact_1"]))
    publisher.record(_artifact_event("artifact_1", "a.csv"))
    publisher.publish()
    # The same call later attaches a second artifact.
    publisher.record(_transform_event("call_1", ["artifact_2"]))
    publisher.record(_artifact_event("artifact_2", "b.csv"))
    publisher.publish()
    publisher.close()

    assert sorted(cmf_server.stored_events["call_1"]) == [4, 4]


def test_known_artifact_memory_is_bounded() -> None:
    publisher = CMFServerPublisher(
        CMFServerConfig(
            server_url="http://127.0.0.1:1", publish_timeout_s=2.0, max_known_artifacts=3
        )
    )
    for index in range(10):
        publisher.record(_artifact_event(f"artifact_{index}", f"f{index}.csv"))
    assert len(publisher._known) == 3  # noqa: SLF001
    publisher.close()
