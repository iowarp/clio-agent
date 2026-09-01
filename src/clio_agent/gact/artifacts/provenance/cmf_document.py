"""Synthesize CMF's ``mlmd_push`` document from the records CLIO already holds.

Server mode writes to a CMF server WITHOUT cmflib: CLIO builds the same JSON
document ``CmfQuery.dumptojson`` produces and POSTs it. This module owns that
synthesis; :mod:`clio_agent.gact.artifacts.provenance.cmf_server_mode` owns the
transport.

The document shape is fixed by what the server's merger reads
(``cmflib/cmf_merger.py`` 0.1.0), verified against a live pull from a running
cmf-server::

    {"Pipeline": [{                       # name, id, type, properties,
       "create_time_since_epoch": ms,     # custom_properties, create_time_*
       "stages": [{                       # Pipeline_Stage context
          "executions": [{                # properties.Execution_uuid REQUIRED
             "events": [{                 # 3 = INPUT, 4 = OUTPUT
                "artifact": {...}}]}]}]}]}

Three consequences drive the design and are not negotiable:

* **An artifact exists only inside an execution's event.** There is no
  free-standing artifact list anywhere in the document — ``handle_event`` is the
  only artifact constructor the merger reaches. An artifact CLIO cannot attach
  to an execution is therefore INVISIBLE in a push that still answers
  ``{"status": "success"}``; this is exactly how a live qualification recorded
  13 executions, zero artifacts and zero events while provider health showed no
  failures. Unattached artifacts get a synthesized creation execution from their
  own ``producer.call_id``; one with no producer at all is refused
  (``cmf_artifact_not_attached_to_execution``) rather than silently dropped.
* **Only Dataset and Model survive the push faithfully**, so those are the only
  two STORAGE CLASSES CLIO writes. ``handle_event`` branches on seven type
  strings and ends in ``else: pass``; of those, Metrics needs a
  ``label:uri:execution_id`` name and carries no bytes, Environment and Label
  need a second multipart upload, and Dataslice/Step_Metrics need constructors
  this document does not reach. This is a narrowing of storage class, NOT of
  what CLIO can track: ``kind`` is an ontology (dataset, source, environment,
  report, plan, ...) preserved verbatim in ``clio_kind``, so every artifact
  stays trackable and nothing is refused for being the "wrong kind".
  ``cmf_artifact_kind_not_representable`` guards the WIRE instead, refusing a
  type the server would drop in its ``else`` branch.
* **``properties.Execution_uuid`` is mandatory on every execution.** The
  server's federation layer indexes it unconditionally
  (``cmf_federation.update_mlmd``), so omitting it is a 500, and a missing key
  on an unnamed execution is answered 422 ``version_update``.

Idempotency: every execution is NAMED ``clio:{call_id}``. A named execution is
skipped by the server's uuid-intersection filter and merged by
``(Context_Type, name)``, so re-pushing one attaches its new artifacts to the
same execution row instead of creating a duplicate or being discarded as
``exists``. That is what makes incremental per-batch pushes correct.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from clio_agent.gact.artifacts.provenance.cmf_encoding import encode_properties, posix_path
from clio_agent.gact.artifacts.provenance.cmf_reasons import CMFRefusal

#: Stamped on every entity so a CMF store can tell which CLIO mapping wrote it.
CLIO_MAPPING_VERSION = "clio.cmf.v1"

#: ``mlpb.Event.Type`` values, as the merger reads them (cmf_merger.py:92-93).
EVENT_INPUT = 3
EVENT_OUTPUT = 4

#: The only two CMF artifact types CLIO writes. See the module docstring.
CMF_TYPE_DATASET = "Dataset"
CMF_TYPE_MODEL = "Model"

#: Synthetic MLMD ids. The server resolves identity by name/uri and assigns its
#: own ids; these are only read on its concurrent-push AlreadyExists branches.
_PIPELINE_ID = 1
_STAGE_ID = 2
_FIRST_ENTITY_ID = 100


def narrow_artifact_type(kind: str) -> str:
    """Map a CLIO artifact kind onto the CMF artifact type CLIO will write.

    CLIO's ``kind`` is an **ontology**, not a storage class: a dataset, a
    user-submitted source, a captured environment, a report and a plan are all
    byte-backed artifacts that differ in what they MEAN, not in how CMF must
    hold them. CMF's artifact type is the storage class, and it has exactly two
    that the metadata push can carry end to end -- Dataset and Model. So the
    ontology is preserved verbatim in ``clio_kind`` and the storage class is
    narrowed, which keeps every kind trackable, including ones CLIO has not
    minted yet.

    The deliberate consequence: nothing is refused for being the "wrong kind".
    CMF's other types (Metrics, Environment, Label, Dataslice, Step_Metrics)
    are storage classes needing constructors or second multipart uploads the
    push document never reaches -- CLIO simply does not use them, rather than
    refusing the artifacts that would have mapped onto them.
    ``cmf_artifact_kind_not_representable`` guards the wire instead
    (:func:`~clio_agent.gact.artifacts.provenance.cmf_server_mode.verify_push_document`),
    where a type outside these two would be dropped silently by the server.

    Args:
        kind: The CLIO artifact kind (ontology), free-form.

    Returns:
        ``"Model"`` for a model, ``"Dataset"`` for everything else.
    """
    return CMF_TYPE_MODEL if kind.strip().lower() == "model" else CMF_TYPE_DATASET


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class ArtifactEntry:
    """One CLIO artifact version, ready to nest under an execution event."""

    artifact_id: str
    name: str
    uri: str
    cmf_type: str
    properties: dict[str, Any]
    custom_properties: dict[str, Any]
    created_ms: int
    #: The transform that produced it, from ``producer.call_id`` -- the anchor a
    #: synthesized creation execution is built from when nothing else claims it.
    producer_call_id: str = ""
    producer_tool: str = ""

    def to_document(self) -> dict[str, Any]:
        """Return the artifact object as the merger reads it.

        Values are encoded on the way out (see :mod:`cmf_encoding`): a literal
        backslash anywhere in an entity's properties makes the cmf-server
        discard it silently.
        """
        return {
            "id": 0,
            "name": self.name,
            "type": self.cmf_type,
            "type_id": 0,
            "uri": self.uri,
            "create_time_since_epoch": self.created_ms,
            "last_update_time_since_epoch": self.created_ms,
            "properties": encode_properties(self.properties),
            "custom_properties": encode_properties(self.custom_properties),
        }


@dataclass
class ExecutionEntry:
    """One CLIO transform, carrying the artifact edges it claims."""

    call_id: str
    execution: str
    custom_properties: dict[str, Any]
    created_ms: int
    used: list[str] = field(default_factory=list)
    generated: list[str] = field(default_factory=list)
    #: True when CLIO minted this execution to carry an otherwise-unattachable
    #: artifact rather than reading it from a transform event.
    synthesized: bool = False
    #: How many publishes this execution has been held back for because an edge
    #: named an artifact not yet seen. Artifact and transform events have no
    #: guaranteed order, so a transform can legitimately arrive first; the
    #: counter bounds how long that excuse lasts.
    deferrals: int = 0

    def unresolved_edges(self, available: dict[str, Any]) -> list[str]:
        """Edge artifact ids that ``available`` cannot resolve."""
        return [
            artifact_id
            for artifact_id in (*self.used, *self.generated)
            if artifact_id not in available
        ]

    @property
    def name(self) -> str:
        """The stable, named-execution key the server merges on."""
        return f"clio:{self.call_id}"


def artifact_entry(event: dict[str, Any], body: dict[str, Any]) -> ArtifactEntry:
    """Build an :class:`ArtifactEntry` from one artifact semantic event.

    The custom-property vocabulary is identical to the local worker's
    (``cmf_worker._record_artifact``) so both deployment shapes populate the same
    ``clio_*`` keys and a reader cannot tell which lane wrote a row.

    Args:
        event: The semantic event envelope.
        body: Its payload.

    Returns:
        The artifact entry, typed Dataset or Model.

    Raises:
        CMFRefusal: The artifact carries no id, or its kind is not representable.
    """
    artifact_id = str(body.get("artifact_id") or "")
    if not artifact_id:
        raise CMFRefusal(
            "cmf_artifact_not_attached_to_execution",
            "artifact event carries no artifact_id to key a CMF artifact on",
            event_id=str(event.get("event_id") or ""),
        )
    kind = str(body.get("kind") or "other")
    cmf_type = narrow_artifact_type(kind)
    producer = body.get("producer")
    producer = producer if isinstance(producer, dict) else {}
    receipt = producer.get("storage_receipt")
    receipt = receipt if isinstance(receipt, dict) else {}
    digests = receipt.get("digests")
    digests = digests if isinstance(digests, dict) else {}
    name = str(body.get("name") or artifact_id)
    version = int(body.get("version") or 1)
    uri = f"clio://artifact/{artifact_id}"
    sha256 = str(body.get("sha256") or digests.get("sha256") or "")
    # ``url`` and ``clio_path`` are declared paths, so they carry the faithful
    # POSIX representation rather than a percent-escaped Windows one.
    url = posix_path(str(receipt.get("object_uri") or body.get("path") or ""))
    properties: dict[str, Any] = {
        "git_repo": "",
        "Commit": str(digests.get("md5") or ""),
        "url": url,
    }
    if cmf_type == CMF_TYPE_MODEL:
        # log_model_with_version raises "Model uri empty" into handle_event's
        # bare `except Exception` -- a silently dropped Model in an otherwise
        # successful push. Always carry the uri the merger copies over.
        properties["uri"] = uri
    custom_properties: dict[str, Any] = {
        "clio_artifact_id": artifact_id,
        "clio_arc_event_id": str(event.get("event_id") or body.get("event_id") or ""),
        "clio_workspace_id": str(body.get("workspace_id") or event.get("workspace_id") or ""),
        "clio_name": name,
        "clio_version": version,
        "clio_kind": kind,
        "clio_sha256": sha256,
        "clio_size_bytes": int(body.get("size_bytes") or 0),
        "clio_custody": str(body.get("custody") or ""),
        "clio_mechanism": str(body.get("mechanism") or ""),
        "clio_evidence_class": str(body.get("evidence_class") or ""),
        "clio_prior_version": int(body.get("prior_version") or 0),
        "clio_prior_sha256": str(body.get("prior_sha256") or ""),
        "clio_created_at": str(body.get("created_at") or event.get("occurred_at") or ""),
        "clio_path": posix_path(str(body.get("path") or "")),
        "clio_annotation": str(body.get("annotation") or ""),
        "clio_producer_json": json.dumps(producer, sort_keys=True),
        "clio_storage_receipt_json": json.dumps(receipt, sort_keys=True),
        "clio_mapping_version": CLIO_MAPPING_VERSION,
    }
    return ArtifactEntry(
        artifact_id=artifact_id,
        # The merger takes everything before the first colon as the artifact's
        # path/name (cmf_merger.py:98), so the version suffix stays readable
        # without polluting the name the server stores.
        name=f"{name}:v{version}",
        uri=uri,
        cmf_type=cmf_type,
        properties=properties,
        custom_properties=custom_properties,
        created_ms=_now_ms(),
        producer_call_id=str(producer.get("call_id") or ""),
        producer_tool=str(producer.get("tool") or ""),
    )


def execution_entry(event: dict[str, Any], body: dict[str, Any]) -> ExecutionEntry:
    """Build an :class:`ExecutionEntry` from one transform semantic event.

    Args:
        event: The semantic event envelope.
        body: Its payload.

    Returns:
        The execution entry with its used/generated artifact ids.

    Raises:
        CMFRefusal: The transform carries no ``call_id`` to key it on.
    """
    call_id = str(body.get("call_id") or "")
    if not call_id:
        raise CMFRefusal(
            "cmf_server_rejected_payload",
            "transform event carries no call_id to key a CMF execution on",
            event_id=str(event.get("event_id") or ""),
        )
    instrument = body.get("instrument")
    instrument = instrument if isinstance(instrument, dict) else {}
    custom_properties: dict[str, Any] = {
        "clio_call_id": call_id,
        "clio_arc_event_id": str(event.get("event_id") or body.get("event_id") or ""),
        "clio_session_id": str(body.get("session_id") or event.get("session_id") or ""),
        "clio_workspace_id": str(body.get("workspace_id") or event.get("workspace_id") or ""),
        "clio_turn_id": str(body.get("turn_id") or event.get("turn_id") or ""),
        "clio_status": str(body.get("status") or event.get("status") or ""),
        "clio_kind": str(body.get("kind") or "ordinary"),
        "clio_agent_id": str(body.get("agent_id") or ""),
        "clio_agent_role": str(body.get("agent_role") or ""),
        "clio_tool": str(instrument.get("tool") or ""),
        "clio_replay": str(body.get("replay") or ""),
        "clio_replay_reason": str(body.get("replay_reason") or ""),
        "clio_started_at": str(body.get("started_at") or ""),
        "clio_ended_at": str(body.get("ended_at") or ""),
        "clio_instrument_json": json.dumps(instrument, sort_keys=True),
        "clio_environment_json": json.dumps(body.get("environment") or {}, sort_keys=True),
        "clio_mapping_version": CLIO_MAPPING_VERSION,
    }
    return ExecutionEntry(
        call_id=call_id,
        execution=str(instrument.get("tool") or "clio transform"),
        custom_properties=custom_properties,
        created_ms=_now_ms(),
        used=_edge_artifact_ids(body.get("used")),
        generated=_edge_artifact_ids(body.get("generated")),
    )


def _edge_artifact_ids(raw: Any) -> list[str]:
    """Collect the CLIO artifact ids named by one edge list."""
    ids: list[str] = []
    for edge in raw if isinstance(raw, list) else []:
        if not isinstance(edge, dict):
            continue
        artifact_id = str(edge.get("artifact_id") or "")
        if artifact_id and artifact_id not in ids:
            ids.append(artifact_id)
    return ids


def creation_execution(artifact: ArtifactEntry) -> ExecutionEntry:
    """Synthesize the execution that carries an otherwise-unattached artifact.

    CMF's document has no free-standing artifact list, so an artifact no
    transform claims would vanish from a push that still reports success. Its
    ``producer.call_id`` names the tool call that created it -- CLIO already
    carries that -- so the artifact is attached to that call as an OUTPUT event.

    Args:
        artifact: The unattached artifact entry.

    Returns:
        A named creation execution generating that artifact.

    Raises:
        CMFRefusal: ``cmf_artifact_not_attached_to_execution`` -- the artifact
            has no producer call to anchor it to.
    """
    if not artifact.producer_call_id:
        raise CMFRefusal(
            "cmf_artifact_not_attached_to_execution",
            (
                "artifact has no producing transform and no producer.call_id to "
                "synthesize a creation execution from; CMF's push document "
                "cannot carry a free-standing artifact"
            ),
            artifact_id=artifact.artifact_id,
            name=artifact.name,
        )
    return ExecutionEntry(
        call_id=artifact.producer_call_id,
        execution=artifact.producer_tool or "clio artifact creation",
        custom_properties={
            "clio_call_id": artifact.producer_call_id,
            "clio_tool": artifact.producer_tool,
            "clio_kind": "creation",
            "clio_synthesized": "creation_execution",
            "clio_mapping_version": CLIO_MAPPING_VERSION,
        },
        created_ms=artifact.created_ms,
        generated=[artifact.artifact_id],
        synthesized=True,
    )


def build_push_document(
    *,
    pipeline_name: str,
    artifacts: dict[str, ArtifactEntry],
    executions: list[ExecutionEntry],
    created_ms: int | None = None,
    known_artifacts: dict[str, ArtifactEntry] | None = None,
) -> dict[str, Any]:
    """Assemble the ``{"Pipeline": [...]}`` document for one push batch.

    Every artifact in ``artifacts`` is attached: to the execution that claims it,
    or to a synthesized creation execution built from its producer. An artifact
    that can be attached to neither raises rather than being dropped.

    Edges are resolved against ``known_artifacts`` as well as this batch, which
    is what makes a CROSS-BATCH edge work: an artifact created in turn 1 is used
    by a transform in turn 2, by which time it is no longer in the batch dict.
    Resolving only the batch silently dropped that INPUT event (no ``else``
    branch), so b=transform(a) lost its input whenever the two landed in
    different pushes. Re-sending the artifact document is safe and idempotent --
    the server dedupes on ``uri``.

    Args:
        pipeline_name: The CMF pipeline these records belong to.
        artifacts: Artifact entries minted by THIS batch, by CLIO artifact id.
        executions: Transform executions, in the order they were recorded.
        created_ms: Pipeline/stage creation stamp; defaults to now.
        known_artifacts: Every artifact the provider has seen, for resolving
            edges that reach back into earlier batches.

    Returns:
        The complete push document.

    Raises:
        CMFRefusal: An artifact could not be attached to any execution, or an
            edge names an artifact id nothing knows about.
    """
    stamp = created_ms if created_ms is not None else _now_ms()
    stage_name = f"{pipeline_name}/artifacts"
    known = known_artifacts or {}
    ordered = list(executions)
    claimed = {
        artifact_id
        for entry in ordered
        for artifact_id in (*entry.used, *entry.generated)
        if artifact_id in artifacts
    }
    for artifact_id, artifact in artifacts.items():
        if artifact_id in claimed:
            continue
        ordered.append(creation_execution(artifact))

    def _edge(artifact_id: str, entry: ExecutionEntry, direction: str) -> ArtifactEntry:
        resolved = artifacts.get(artifact_id) or known.get(artifact_id)
        if resolved is None:
            raise CMFRefusal(
                "cmf_artifact_reference_unresolved",
                (
                    f"transform {entry.call_id} names artifact {artifact_id!r} as "
                    f"{direction}, but no artifact record is known for it; the edge "
                    "cannot be written and must not be dropped silently"
                ),
                call_id=entry.call_id,
                artifact_id=artifact_id,
                direction=direction,
            )
        return resolved

    entity_id = _FIRST_ENTITY_ID
    execution_documents: list[dict[str, Any]] = []
    for entry in ordered:
        events: list[dict[str, Any]] = []
        for artifact_id in entry.used:
            events.append(
                {"type": EVENT_INPUT, "artifact": _edge(artifact_id, entry, "used").to_document()}
            )
        for artifact_id in entry.generated:
            events.append(
                {
                    "type": EVENT_OUTPUT,
                    "artifact": _edge(artifact_id, entry, "generated").to_document(),
                }
            )
        execution_documents.append(
            {
                "id": entity_id,
                "name": entry.name,
                "type": stage_name,
                "type_id": 0,
                "create_time_since_epoch": entry.created_ms,
                "last_update_time_since_epoch": entry.created_ms,
                # Encoded: a literal backslash in ANY execution property or
                # custom property makes the cmf-server discard this execution
                # and every event on it, while still answering success.
                "properties": encode_properties(
                    {
                        "Context_Type": stage_name,
                        "Context_ID": _STAGE_ID,
                        "Execution": entry.execution,
                        "Execution_type_name": stage_name,
                        "Pipeline_Type": pipeline_name,
                        "Pipeline_id": _PIPELINE_ID,
                        "Git_Repo": "",
                        "Git_Start_Commit": "",
                        "Git_End_Commit": "",
                        # Mandatory: the federation layer indexes this key
                        # unconditionally, and its absence is answered 422.
                        "Execution_uuid": entry.call_id,
                    }
                ),
                "custom_properties": encode_properties(entry.custom_properties),
                "events": events,
            }
        )
        entity_id += 1
    return {
        "Pipeline": [
            {
                "id": _PIPELINE_ID,
                "name": pipeline_name,
                "type": "Parent_Context",
                "type_id": 0,
                "create_time_since_epoch": stamp,
                "last_update_time_since_epoch": stamp,
                "properties": {"Pipeline": pipeline_name},
                "custom_properties": {
                    "clio_mapping_version": CLIO_MAPPING_VERSION,
                    "clio_source": "arc-semantic-event-highway",
                },
                "stages": [
                    {
                        "id": _STAGE_ID,
                        "name": stage_name,
                        "type": "Pipeline_Stage",
                        "type_id": 0,
                        "create_time_since_epoch": stamp,
                        "last_update_time_since_epoch": stamp,
                        "properties": {"Pipeline_Stage": stage_name},
                        "custom_properties": {"clio_substream": "artifact-provenance"},
                        "executions": execution_documents,
                    }
                ],
            }
        ]
    }


__all__ = [
    "CLIO_MAPPING_VERSION",
    "CMF_TYPE_DATASET",
    "CMF_TYPE_MODEL",
    "EVENT_INPUT",
    "EVENT_OUTPUT",
    "ArtifactEntry",
    "ExecutionEntry",
    "artifact_entry",
    "build_push_document",
    "creation_execution",
    "execution_entry",
    "narrow_artifact_type",
]
