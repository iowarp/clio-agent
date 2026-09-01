"""The synthesized ``mlmd_push`` document matches what CMF's merger reads.

CLIO builds this document itself (no cmflib on the server-mode path), so these
tests stand in for the library that would otherwise guarantee the shape. The
required-key sets below are taken from the code that consumes them in cmflib
0.1.0 -- ``cmf_merger.create_original_time_since_epoch`` (unguarded ``[...]``
indexing, so a missing key is a server 500), ``cmf_merger.handle_execution`` /
``handle_event`` (the keys actually read), and
``cmf_federation.update_mlmd`` (indexes ``Execution_uuid`` unconditionally) --
and cross-checked against a document pulled from a live cmf-server.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from clio_agent.gact.artifacts.provenance.cmf_document import (
    EVENT_INPUT,
    EVENT_OUTPUT,
    ArtifactEntry,
    artifact_entry,
    build_push_document,
    execution_entry,
    narrow_artifact_type,
)
from clio_agent.gact.artifacts.provenance.cmf_reasons import CMFRefusal

# Every key an unguarded consumer indexes. A missing one is a server-side crash,
# not a validation error, so they are asserted structurally.
_PIPELINE_KEYS = {
    "id",
    "name",
    "type",
    "type_id",
    "create_time_since_epoch",
    "last_update_time_since_epoch",
    "properties",
    "custom_properties",
    "stages",
}
_STAGE_KEYS = {
    "id",
    "name",
    "type",
    "type_id",
    "create_time_since_epoch",
    "last_update_time_since_epoch",
    "properties",
    "custom_properties",
    "executions",
}
_EXECUTION_KEYS = {
    "id",
    "name",
    "type",
    "type_id",
    "create_time_since_epoch",
    "last_update_time_since_epoch",
    "properties",
    "custom_properties",
    "events",
}
_ARTIFACT_KEYS = {
    "id",
    "name",
    "type",
    "type_id",
    "uri",
    "create_time_since_epoch",
    "last_update_time_since_epoch",
    "properties",
    "custom_properties",
}


def _artifact_event(
    artifact_id: str,
    name: str,
    *,
    kind: str = "dataset",
    call_id: str = "call_write",
    version: int = 1,
) -> tuple[dict[str, Any], dict[str, Any]]:
    event = {"event_id": f"sem_{artifact_id}", "occurred_at": "2026-09-01T00:00:00+00:00"}
    body = {
        "artifact_id": artifact_id,
        "name": name,
        "version": version,
        "kind": kind,
        "sha256": "a" * 64,
        "size_bytes": 12,
        "workspace_id": "ws_1",
        "custody": "cas",
        "mechanism": "tool_schema",
        "path": f"/w/{name}",
        "producer": {
            "call_id": call_id,
            "tool": "fs_apply_edit_write",
            "storage_receipt": {
                "object_uri": f"cmf+dvc://local/files/md5/ab/{artifact_id}",
                "digests": {"md5": "b" * 32, "sha256": "a" * 64},
            },
        },
    }
    return event, body


def _transform_event(
    call_id: str, *, used: list[str] | None = None, generated: list[str] | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    event = {"event_id": f"sem_{call_id}"}
    body = {
        "call_id": call_id,
        "session_id": "sess_1",
        "turn_id": "msg_1",
        "status": "success",
        "instrument": {"tool": "fs_apply_edit_write"},
        "used": [{"artifact_id": item, "name": item} for item in used or []],
        "generated": [{"artifact_id": item, "name": item} for item in generated or []],
    }
    return event, body


def _document(**kwargs: Any) -> dict[str, Any]:
    return build_push_document(pipeline_name="clio-agent", created_ms=1_700_000_000_000, **kwargs)


def test_golden_document_matches_the_shape_the_merger_reads() -> None:
    in_event, in_body = _artifact_event("artifact_in", "a.csv")
    out_event, out_body = _artifact_event("artifact_out", "b.csv")
    t_event, t_body = _transform_event("call_1", used=["artifact_in"], generated=["artifact_out"])
    artifacts = {
        "artifact_in": artifact_entry(in_event, in_body),
        "artifact_out": artifact_entry(out_event, out_body),
    }
    document = _document(artifacts=artifacts, executions=[execution_entry(t_event, t_body)])

    assert set(document) == {"Pipeline"}
    pipeline = document["Pipeline"][0]
    assert _PIPELINE_KEYS <= set(pipeline)
    assert pipeline["name"] == "clio-agent"
    assert pipeline["type"] == "Parent_Context"
    assert pipeline["properties"] == {"Pipeline": "clio-agent"}

    stage = pipeline["stages"][0]
    assert _STAGE_KEYS <= set(stage)
    assert stage["name"] == "clio-agent/artifacts"
    assert stage["type"] == "Pipeline_Stage"
    assert stage["properties"] == {"Pipeline_Stage": "clio-agent/artifacts"}

    assert len(stage["executions"]) == 1
    execution = stage["executions"][0]
    assert _EXECUTION_KEYS <= set(execution)
    # Named execution: the server skips the uuid-intersection filter for it and
    # merges by (Context_Type, name), so a re-push attaches new artifacts here.
    assert execution["name"] == "clio:call_1"
    assert execution["type"] == "clio-agent/artifacts"
    assert execution["properties"]["Context_Type"] == "clio-agent/artifacts"
    assert execution["properties"]["Execution_uuid"] == "call_1"
    assert execution["properties"]["Execution"] == "fs_apply_edit_write"
    assert execution["custom_properties"]["clio_call_id"] == "call_1"

    events = execution["events"]
    assert [event["type"] for event in events] == [EVENT_INPUT, EVENT_OUTPUT]
    for event in events:
        assert set(event) == {"type", "artifact"}
        assert _ARTIFACT_KEYS <= set(event["artifact"])
    used, generated = events[0]["artifact"], events[1]["artifact"]
    assert used["uri"] == "clio://artifact/artifact_in"
    assert used["type"] == "Dataset"
    assert used["name"] == "a.csv:v1"
    assert generated["uri"] == "clio://artifact/artifact_out"
    assert generated["custom_properties"]["clio_artifact_id"] == "artifact_out"
    assert generated["custom_properties"]["clio_mapping_version"] == "clio.cmf.v1"
    # Dataset needs git_repo/url; the merger reads props straight through.
    assert set(generated["properties"]) == {"git_repo", "Commit", "url"}


def test_every_execution_carries_execution_uuid_the_server_indexes_unconditionally() -> None:
    event, body = _artifact_event("artifact_1", "a.csv")
    document = _document(artifacts={"artifact_1": artifact_entry(event, body)}, executions=[])
    for execution in document["Pipeline"][0]["stages"][0]["executions"]:
        assert execution["properties"]["Execution_uuid"], "a missing uuid is answered 422"


def test_the_document_is_json_serializable_as_the_push_body_string() -> None:
    event, body = _artifact_event("artifact_1", "a.csv")
    document = _document(artifacts={"artifact_1": artifact_entry(event, body)}, executions=[])
    # json_payload is a STRING containing the document (MLMDPushRequest).
    assert json.loads(json.dumps(document)) == document


def test_model_kind_narrows_to_model_and_always_carries_the_uri_property() -> None:
    event, body = _artifact_event("artifact_m", "model.pkl", kind="model")
    entry = artifact_entry(event, body)
    assert entry.cmf_type == "Model"
    # log_model_with_version raises "Model uri empty" into a bare except that
    # only logs -- a silently dropped Model inside a "success" push.
    assert entry.properties["uri"] == "clio://artifact/artifact_m"
    assert entry.custom_properties["clio_kind"] == "model"


@pytest.mark.parametrize(
    "kind", ["dataset", "image", "report", "plan", "script", "config", "ui_payload", "other"]
)
def test_representable_kinds_narrow_to_dataset_with_the_real_kind_preserved(kind: str) -> None:
    event, body = _artifact_event("artifact_1", "f.bin", kind=kind)
    entry = artifact_entry(event, body)
    assert entry.cmf_type == "Dataset"
    # Narrowing must not LOSE the kind -- that is the whole fidelity bargain.
    assert entry.custom_properties["clio_kind"] == kind


@pytest.mark.parametrize(
    "kind", ["metrics", "step_metrics", "dataslice", "environment", "label", "table"]
)
def test_unrepresentable_kinds_refuse_instead_of_narrowing_silently(kind: str) -> None:
    with pytest.raises(CMFRefusal) as excinfo:
        narrow_artifact_type(kind)
    assert excinfo.value.reason == "cmf_artifact_kind_not_representable"
    assert excinfo.value.payload["details"]["kind"] == kind


def test_unattached_artifact_gets_a_synthesized_creation_execution() -> None:
    """An artifact no transform claims is INVISIBLE in the pushed document.

    CMF has no free-standing artifact list -- ``execution.events[].artifact`` is
    the only place one can exist. This is how a live run recorded 13 executions,
    zero artifacts and zero events while health showed no failures.
    """
    event, body = _artifact_event("artifact_orphan", "a3.csv", call_id="call_mint")
    document = _document(artifacts={"artifact_orphan": artifact_entry(event, body)}, executions=[])
    executions = document["Pipeline"][0]["stages"][0]["executions"]
    assert len(executions) == 1
    synthesized = executions[0]
    assert synthesized["name"] == "clio:call_mint"
    assert synthesized["properties"]["Execution_uuid"] == "call_mint"
    assert synthesized["custom_properties"]["clio_synthesized"] == "creation_execution"
    assert [event["type"] for event in synthesized["events"]] == [EVENT_OUTPUT]
    assert synthesized["events"][0]["artifact"]["uri"] == "clio://artifact/artifact_orphan"


def test_an_artifact_already_claimed_by_a_transform_is_not_duplicated() -> None:
    event, body = _artifact_event("artifact_out", "b.csv", call_id="call_1")
    t_event, t_body = _transform_event("call_1", generated=["artifact_out"])
    document = _document(
        artifacts={"artifact_out": artifact_entry(event, body)},
        executions=[execution_entry(t_event, t_body)],
    )
    executions = document["Pipeline"][0]["stages"][0]["executions"]
    assert len(executions) == 1, "the claimed artifact must not also mint a creation execution"
    assert executions[0]["events"][0]["artifact"]["uri"] == "clio://artifact/artifact_out"


def test_artifact_with_no_producer_call_is_refused_not_dropped() -> None:
    event, body = _artifact_event("artifact_x", "x.csv")
    body["producer"] = {}
    entry = artifact_entry(event, body)
    with pytest.raises(CMFRefusal) as excinfo:
        _document(artifacts={"artifact_x": entry}, executions=[])
    assert excinfo.value.reason == "cmf_artifact_not_attached_to_execution"
    assert excinfo.value.payload["details"]["artifact_id"] == "artifact_x"


def test_artifact_event_without_an_id_is_refused() -> None:
    event, body = _artifact_event("", "x.csv")
    body["artifact_id"] = ""
    with pytest.raises(CMFRefusal) as excinfo:
        artifact_entry(event, body)
    assert excinfo.value.reason == "cmf_artifact_not_attached_to_execution"


def test_transform_without_a_call_id_is_refused() -> None:
    event, body = _transform_event("")
    body["call_id"] = ""
    with pytest.raises(CMFRefusal) as excinfo:
        execution_entry(event, body)
    assert excinfo.value.reason == "cmf_server_rejected_payload"


def test_edges_naming_an_unknown_artifact_do_not_emit_a_null_event() -> None:
    t_event, t_body = _transform_event("call_1", used=["artifact_missing"])
    document = _document(artifacts={}, executions=[execution_entry(t_event, t_body)])
    assert document["Pipeline"][0]["stages"][0]["executions"][0]["events"] == []


def test_entry_documents_are_copies_so_one_push_cannot_mutate_the_next() -> None:
    event, body = _artifact_event("artifact_1", "a.csv")
    entry: ArtifactEntry = artifact_entry(event, body)
    first = entry.to_document()
    first["custom_properties"]["clio_kind"] = "tampered"
    assert entry.to_document()["custom_properties"]["clio_kind"] == "dataset"
