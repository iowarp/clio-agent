"""Server-mode CMF writes: conf declaration only, no local CMF/MLMD runtime.

Deployment shape (a). ``provenance.artifacts.cmf.server_url`` alone gives a
working write path on every client OS: CLIO synthesizes CMF's push document
(:mod:`clio_agent.gact.artifacts.provenance.cmf_document`) from records it
already holds and POSTs it to ``{server_url}/api/mlmd_push``. Nothing here
imports ``cmflib`` or ``ml_metadata``, so the platform limitation that blocks
local worker mode (no ml-metadata wheels) does not apply.

The wire contract, from ``cmflib/server_interface/server_interface.py`` and the
server's own ``MLMDPushRequest`` model::

    POST {server_url}/api/mlmd_push
    Content-Type: application/json
    {"exec_uuid": null,
     "pipeline_name": "<name>",
     "json_payload": "<the document, as a JSON STRING>"}

``json_payload`` is a *string* containing the JSON, not a nested object -- the
server validates it with ``json.loads`` before doing anything else.

**Pushes are incremental and verified.** Each emit batch pushes only its own
records; named executions make that idempotent (see ``cmf_document``). Every
document is verified BEFORE it is sent, because the server drops entities
silently: ``handle_event`` ends in ``else: pass`` for an artifact type it does
not know, and ``log_model_with_version``'s "Model uri empty" raises into a bare
``except Exception`` that only logs. Both produce ``{"status": "success"}`` with
the artifact missing. A push therefore only counts as written once the document
is proven to contain nothing the server would silently discard.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from clio_agent.gact.artifacts.provenance.cmf_confirmation import PushConfirmer
from clio_agent.gact.artifacts.provenance.cmf_document import (
    CMF_TYPE_DATASET,
    CMF_TYPE_MODEL,
    ArtifactEntry,
    ExecutionEntry,
    artifact_entry,
    build_push_document,
    execution_entry,
)
from clio_agent.gact.artifacts.provenance.cmf_encoding import has_hostile_value
from clio_agent.gact.artifacts.provenance.cmf_reasons import CMFRefusal
from clio_agent.gact.provenance.protocol import ProviderReceipt

if TYPE_CHECKING:
    from clio_agent.gact.artifacts.provenance.protocol import ArtifactStore
    from clio_agent.gact.semantic_events import SemanticEvent

logger = logging.getLogger(__name__)

#: Event types that mint an artifact entry.
_ARTIFACT_EVENTS = frozenset({"artifact.created", "artifact.version.added"})
#: Event type that mints an execution entry.
_TRANSFORM_EVENT = "artifact.transform.recorded"
#: Event types that annotate an already-pushed artifact. CMF's push document has
#: no update verb, so these carry no new entity; they are reported FILTERED
#: rather than counted as a write that did not happen.
_ANNOTATION_EVENTS = frozenset({"artifact.alias.moved", "artifact.enriched", "artifact.used"})

#: Statuses a 200 can carry (cmf_federation.update_mlmd).
_STATUS_WRITTEN = "success"
_STATUS_EXISTS = "exists"

#: How many publishes an execution may be held back waiting for an edge's
#: artifact event before the reference is treated as genuinely unknown.
_MAX_EDGE_DEFERRALS = 3


def verify_push_document(document: dict[str, Any]) -> None:
    """Refuse a document containing anything the CMF server would silently drop.

    The server has three silent-loss branches, all of which still answer
    ``{"status": "success"}``: an unknown ``artifact.type`` falls through
    ``handle_event``'s ``else: pass``; a Model with no ``properties.uri`` raises
    into a bare ``except Exception`` that only logs; and an execution with no
    ``Execution_uuid`` crashes ``update_mlmd``'s unguarded index. Proving the
    document clean before the POST is what lets a 200 be counted as a write.

    Args:
        document: The synthesized ``{"Pipeline": [...]}`` document.

    Raises:
        CMFRefusal: The document would lose an entity server-side.
    """
    pipelines = document.get("Pipeline")
    if not isinstance(pipelines, list) or not pipelines:
        raise CMFRefusal(
            "cmf_server_rejected_payload",
            "document carries no Pipeline entry; the server answers 400",
        )
    for pipeline in pipelines:
        for stage in pipeline.get("stages") or []:
            for execution in stage.get("executions") or []:
                _verify_execution(execution)


def _verify_execution(execution: dict[str, Any]) -> None:
    """Verify one execution and every artifact nested in its events."""
    properties = execution.get("properties") or {}
    if not str(properties.get("Execution_uuid") or "").strip():
        raise CMFRefusal(
            "cmf_server_version_incompatible",
            "execution carries no Execution_uuid; the server answers 422 version_update",
            execution=str(execution.get("name") or ""),
        )
    # A literal backslash in ANY execution property makes the server discard the
    # execution and all its events while still answering success. The encoding
    # in cmf_encoding removes them; this asserts the encoding actually ran.
    if has_hostile_value(properties) or has_hostile_value(execution.get("custom_properties") or {}):
        raise CMFRefusal(
            "cmf_server_discarded_entities",
            (
                "execution carries a raw backslash, which the cmf-server discards "
                "silently along with every event on it; the value encoding did not run"
            ),
            execution=str(execution.get("name") or ""),
        )
    for event in execution.get("events") or []:
        artifact = event.get("artifact") or {}
        artifact_type = str(artifact.get("type") or "")
        if artifact_type not in {CMF_TYPE_DATASET, CMF_TYPE_MODEL}:
            raise CMFRefusal(
                "cmf_artifact_kind_not_representable",
                (
                    f"artifact type {artifact_type!r} falls through the server's "
                    "handle_event else-branch and would be dropped silently"
                ),
                artifact=str(artifact.get("name") or ""),
                artifact_type=artifact_type,
            )
        if not str(artifact.get("uri") or "").strip():
            raise CMFRefusal(
                "cmf_server_rejected_payload",
                "artifact carries no uri; the server cannot key or dedupe it",
                artifact=str(artifact.get("name") or ""),
            )
        if (
            artifact_type == CMF_TYPE_MODEL
            and not str((artifact.get("properties") or {}).get("uri") or "").strip()
        ):
            raise CMFRefusal(
                "cmf_server_rejected_payload",
                (
                    "Model artifact carries no properties.uri; "
                    "log_model_with_version drops it into a logged-only exception"
                ),
                artifact=str(artifact.get("name") or ""),
            )


@dataclass(frozen=True)
class CMFServerConfig:
    """Everything server mode needs, all of it from the conf declaration."""

    server_url: str
    pipeline_name: str = "clio-agent"
    publish_timeout_s: float = 30.0
    #: Bound on records held while the server is unreachable. Eviction is
    #: reported in the refusal payload, never silent.
    max_pending_records: int = 2048
    #: Bound on the cross-batch artifact memory used to resolve edges that
    #: reach back into earlier turns.
    max_known_artifacts: int = 4096

    def __post_init__(self) -> None:
        if not self.server_url.strip():
            raise ValueError("provenance.artifacts.cmf.server_url must not be empty")
        if not self.pipeline_name.strip():
            raise ValueError("provenance.artifacts.cmf.pipeline_name must not be empty")
        if self.publish_timeout_s <= 0:
            raise ValueError("provenance.artifacts.cmf.publish_timeout_s must be greater than zero")

    @property
    def push_url(self) -> str:
        """The metadata-push endpoint."""
        return f"{self.server_url.strip().rstrip('/')}/api/mlmd_push"


class CMFServerPublisher:
    """Accumulate one batch of records and push it to the CMF server."""

    def __init__(self, config: CMFServerConfig, *, client: httpx.Client | None = None) -> None:
        self.config = config
        self._client = client
        self._owns_client = client is None
        self._lock = threading.Lock()
        self._artifacts: dict[str, ArtifactEntry] = {}
        self._executions: list[ExecutionEntry] = []
        #: Every artifact this provider has seen, kept across batches so an edge
        #: reaching back to an earlier turn still resolves. Bounded, insertion
        #: ordered; eviction is counted, never silent.
        self._known: dict[str, ArtifactEntry] = {}
        self._evicted = 0
        self._confirmer = PushConfirmer(config, self._http)
        self.last_publication: dict[str, Any] | None = None

    @property
    def pending(self) -> int:
        """Records buffered for the next push."""
        with self._lock:
            return len(self._artifacts) + len(self._executions)

    def record(self, event: dict[str, Any]) -> bool:
        """Buffer one semantic event.

        Args:
            event: The full semantic-event dict.

        Returns:
            ``True`` when the event minted a pushable record, ``False`` when the
            event type carries no CMF entity.

        Raises:
            CMFRefusal: The event cannot be represented (kind, missing id).
        """
        event_type = str(event.get("event_type") or "")
        payload = event.get("payload")
        body = payload if isinstance(payload, dict) else {}
        if event_type in _ARTIFACT_EVENTS:
            entry = artifact_entry(event, body)
            with self._lock:
                self._artifacts[entry.artifact_id] = entry
                # Remembered beyond this batch so a later turn's transform can
                # still name it as an input.
                self._known[entry.artifact_id] = entry
                while len(self._known) > self.config.max_known_artifacts:
                    self._known.pop(next(iter(self._known)))
                self._evict_if_over_bound()
            return True
        if event_type == _TRANSFORM_EVENT:
            execution = execution_entry(event, body)
            with self._lock:
                self._executions.append(execution)
                self._evict_if_over_bound()
            return True
        return False

    def _evict_if_over_bound(self) -> None:
        """Drop the oldest executions once the buffer exceeds its bound.

        Called under ``self._lock``. Eviction is counted and reported on the next
        refusal, so a long server outage degrades loudly rather than silently.
        """
        bound = self.config.max_pending_records
        while len(self._artifacts) + len(self._executions) > bound and self._executions:
            self._executions.pop(0)
            self._evicted += 1

    def publish(self) -> dict[str, Any]:
        """Push the buffered batch and clear it once the server confirms.

        Returns:
            The publication record (status, status_code, counts).

        Raises:
            CMFRefusal: The server was unreachable, refused the payload, or the
                document would have lost an entity server-side.
        """
        with self._lock:
            artifacts = dict(self._artifacts)
            executions = list(self._executions)
            known = dict(self._known)
            evicted = self._evicted
        if not artifacts and not executions:
            return {"status": "empty", "artifacts": 0, "executions": 0}
        executions = self._resolvable(executions, artifacts, known, evicted=evicted)
        if not artifacts and not executions:
            # Everything in this batch is waiting on an artifact event that has
            # not arrived yet; nothing to push, nothing lost.
            return {"status": "deferred", "artifacts": 0, "executions": 0}
        document = build_push_document(
            pipeline_name=self.config.pipeline_name,
            artifacts=artifacts,
            executions=executions,
            known_artifacts=known,
        )
        verify_push_document(document)
        status, status_code = self._post(document, evicted=evicted)
        self._confirmer.confirm(document, evicted=evicted)
        with self._lock:
            for artifact_id in artifacts:
                self._artifacts.pop(artifact_id, None)
            # Remove exactly what was published: a deferred execution stays
            # pending for the batch whose artifact event unblocks it.
            published = {id(entry) for entry in executions}
            self._executions = [entry for entry in self._executions if id(entry) not in published]
            self._evicted = 0
        self.last_publication = {
            "status": status,
            "status_code": status_code,
            "pipeline_name": self.config.pipeline_name,
            "artifacts": len(artifacts),
            "executions": len(document["Pipeline"][0]["stages"][0]["executions"]),
        }
        return dict(self.last_publication)

    def _resolvable(
        self,
        executions: list[ExecutionEntry],
        artifacts: dict[str, ArtifactEntry],
        known: dict[str, ArtifactEntry],
        *,
        evicted: int,
    ) -> list[ExecutionEntry]:
        """Split off executions whose edges cannot be resolved yet.

        Artifact and transform events have no guaranteed order, so a transform
        can name an artifact whose ``artifact.created`` has not been emitted
        yet. Refusing that outright would make event ordering load-bearing, and
        pushing it would drop the edge silently -- so the execution is HELD in
        the pending buffer and retried on the next publish, which costs nothing
        and resolves as soon as the artifact lands.

        The excuse is bounded: after :data:`_MAX_EDGE_DEFERRALS` publishes the
        reference is treated as genuinely unknown and refused with a typed
        reason, so a real dangling edge stays loud instead of being deferred
        forever.

        Args:
            executions: The pending executions.
            artifacts: Artifacts minted by this batch.
            known: Artifacts seen in earlier batches.
            evicted: Records dropped from the pending bound, for the refusal.

        Returns:
            The executions whose edges all resolve now.

        Raises:
            CMFRefusal: An edge stayed unresolved past the deferral bound.
        """
        available: dict[str, Any] = {**known, **artifacts}
        ready: list[ExecutionEntry] = []
        for entry in executions:
            unresolved = entry.unresolved_edges(available)
            if not unresolved:
                ready.append(entry)
                continue
            entry.deferrals += 1
            if entry.deferrals > _MAX_EDGE_DEFERRALS:
                raise CMFRefusal(
                    "cmf_artifact_reference_unresolved",
                    (
                        f"transform {entry.call_id} still names unknown artifact(s) "
                        f"after {entry.deferrals} publishes; the edge cannot be "
                        "written and must not be dropped silently"
                    ),
                    call_id=entry.call_id,
                    artifact_ids=unresolved[:10],
                    deferrals=entry.deferrals,
                    evicted_records=evicted,
                )
            logger.debug(
                "cmf server-mode deferring execution %s: waiting on artifact events %s",
                entry.call_id,
                unresolved[:5],
            )
        return ready

    def _post(self, document: dict[str, Any], *, evicted: int) -> tuple[str, int]:
        """POST one document and map the answer onto the refusal catalog."""
        body = {
            "exec_uuid": None,
            "pipeline_name": self.config.pipeline_name,
            # A STRING containing the JSON: MLMDPushRequest.json_payload is
            # typed `str` and validated with json.loads.
            "json_payload": json.dumps(document, separators=(",", ":")),
        }
        try:
            response = self._http().post(
                self.config.push_url, json=body, timeout=self.config.publish_timeout_s
            )
        except httpx.HTTPError as exc:
            raise CMFRefusal(
                "cmf_server_unreachable",
                f"{type(exc).__name__}: {exc}",
                server_url=self.config.server_url,
                timeout_s=self.config.publish_timeout_s,
                evicted_records=evicted,
            ) from exc
        return self._interpret(response, evicted=evicted)

    def _interpret(self, response: httpx.Response, *, evicted: int) -> tuple[str, int]:
        """Map one HTTP answer onto a confirmed write or a typed refusal."""
        status_code = int(response.status_code)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        status = str(payload.get("status") or "") if isinstance(payload, dict) else ""
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if status_code == 422 or detail == "version_update":
            raise CMFRefusal(
                "cmf_server_version_incompatible",
                "the CMF server answered version_update for a document that stamps "
                "Execution_uuid on every execution",
                status_code=status_code,
                detail=str(detail or ""),
                evicted_records=evicted,
            )
        if status_code != 200 or status not in {_STATUS_WRITTEN, _STATUS_EXISTS}:
            raise CMFRefusal(
                "cmf_server_rejected_payload",
                f"CMF metadata push refused (status={status_code}, result={status or 'unknown'})",
                status_code=status_code,
                result=status,
                detail=str(detail or "")[:200],
                evicted_records=evicted,
            )
        if evicted:
            logger.warning(
                "cmf server-mode push recovered after evicting %d buffered records "
                "(reason=cmf_server_unreachable backlog bound)",
                evicted,
            )
        return status, status_code

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.config.publish_timeout_s)
        return self._client

    def close(self) -> None:
        """Close the HTTP client this publisher owns."""
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None


class CMFServerModeProvider:
    """The CMF artifact-provenance provider for a declared ``server_url``."""

    name = "cmf"
    durable = True
    queryable = True

    def __init__(
        self,
        config: CMFServerConfig,
        store: "ArtifactStore",
        *,
        publisher: CMFServerPublisher | None = None,
        reader: Any | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self._publisher = publisher or CMFServerPublisher(config)
        self._reader = reader

    def emit(self, event: "SemanticEvent") -> ProviderReceipt:
        """Record one artifact event and push the batch it completes.

        Returns ``ACCEPTED`` only after the server has confirmed the write, so
        the dispatcher's accepted counter cannot outrun what CMF actually holds.
        """
        if not self._publisher.record(event.to_dict("full")):
            return ProviderReceipt.FILTERED
        result = self._publisher.publish()
        if str(result.get("status") or "") == "deferred":
            # Recorded, but nothing was written: this batch is waiting on an
            # artifact event that has not arrived. Claiming ACCEPTED here would
            # put the counter back in front of reality. The record is pushed by
            # a later emit, which UNDER-counts rather than over-counts -- the
            # safe direction for a "did CMF really get it" number.
            return ProviderReceipt.FILTERED
        return ProviderReceipt.ACCEPTED

    def flush(self) -> None:
        """Push anything still buffered.

        A complete barrier: :meth:`emit` publishes synchronously, so this only
        has work to do when a previous push was refused and left records
        pending -- in which case it raises the same typed refusal.
        """
        self._publisher.publish()

    def lineage(
        self,
        artifact_id: str,
        *,
        direction: str,
        depth: int,
        complete: bool = False,
    ) -> dict[str, Any] | None:
        """Answer a lineage query through the server's REST read surface."""
        if self._reader is None:
            raise CMFRefusal(
                "cmf_lineage_query_unavailable",
                "server mode has no configured CMF REST reader for lineage queries",
                server_url=self.config.server_url,
            )
        return self._reader.lineage(
            artifact_id, direction=direction, depth=depth, complete=complete
        )

    def probe(self) -> dict[str, Any]:
        """Return the server-mode runtime state (no local CMF runtime exists)."""
        return {
            "ok": True,
            "mode": "server",
            "server_url": self.config.server_url,
            "pipeline_name": self.config.pipeline_name,
            "pending_records": self._publisher.pending,
            "last_publication": self._publisher.last_publication,
        }

    def close(self) -> None:
        """Push what is left, then release the HTTP client."""
        try:
            self._publisher.publish()
        finally:
            self._publisher.close()
            if self._reader is not None:
                self._reader.close()


__all__ = [
    "CMFServerConfig",
    "CMFServerModeProvider",
    "CMFServerPublisher",
    "verify_push_document",
]
