"""Passive per-tree indexes for the existing confirmed Core task writer.

Every function projects into one :class:`~clio_agent.gact.task_tree_state.TreeState`;
the history façade chooses the tree and owns locking, records and wake hints.
"""

from __future__ import annotations

import json
import logging
from bisect import bisect_left
from typing import TYPE_CHECKING, Any

from clio_agent.arc.task_types import (
    CoordinationGrant,
    CoreReference,
    MailItem,
    NativeAttempt,
    RecipientInput,
    TaskAlias,
    TaskSnapshot,
    TaskSubscription,
    WorkflowCheckpoint,
    argument_digest,
)
from clio_agent.errors import ClioError

if TYPE_CHECKING:
    from clio_agent.gact.task_history import TaskHistory
    from clio_agent.gact.task_tree_state import TreeState


def _project_workflow(self: TreeState, record: WorkflowCheckpoint) -> None:
    """Index immutable invocation bindings; canonical checkpoints outrank legacy projections."""
    binding_key = record.root_handle, record.invocation_id
    execution_key = record.root_handle, record.execution_id
    prior = self._workflow_bindings.get(binding_key)
    if prior is not None and (
        prior.execution_id != record.execution_id
        or prior.declaration != record.declaration
        or prior.request_digest != record.request_digest
        or prior.session_id != record.session_id
    ):
        raise ValueError("Workflow invocation identity was reused")

    def newer(previous: WorkflowCheckpoint | None) -> bool:
        return (
            previous is None
            or previous.source == "legacy_snapshot"
            or record.source == "checkpoint"
        )

    if newer(prior):
        self._workflow_bindings[binding_key] = record
    if newer(self._workflow_executions.get(execution_key)):
        self._workflow_executions[execution_key] = record
    if newer(self._workflow_latest.get(record.root_handle)):
        self._workflow_latest[record.root_handle] = record
    self._workflow_dirty.add(record.root_handle)


def record_workflow(self: TaskHistory, record: WorkflowCheckpoint) -> None:
    """Write existing runner provenance through the same confirmed Core writer."""
    from clio_agent.gact import task_index_writes

    with self._writing():
        owner = self.task_snapshot(record.root_handle)
        state = self._handle_state(record.root_handle)
        body = self.read_content(record.content)
        if (
            owner is None
            or state is None
            or owner.owner.session_id != record.session_id
            or record.content.session_id != record.session_id
            or not isinstance(body, dict)
            or body.get("id") != record.execution_id
            or body.get("root_handle") != record.root_handle
            or body.get("root_session") != record.session_id
            or body.get("declaration") != record.declaration
            or argument_digest({"request": body.get("request")}) != record.request_digest
            or record.source != "checkpoint"
        ):
            raise ValueError("Workflow checkpoint differs from its owner or declared identity")
        previous = state._workflow_bindings.get((record.root_handle, record.invocation_id))
        if previous is not None and (
            previous.execution_id,
            previous.declaration,
            previous.request_digest,
        ) != (record.execution_id, record.declaration, record.request_digest):
            raise ValueError("Workflow invocation identity was reused")
        payload = record.model_dump(mode="json")
        task_index_writes.open_workflow(self, record.root_handle)
        self._record(
            "workflow.checkpoint",
            record.session_id,
            record.root_handle + ":" + record.invocation_id + ":" + record.content.sha256,
            payload,
        )
        self._project(state, "workflow.checkpoint", payload)


def _legacy_workflow(
    self: TreeState, snapshot: TaskSnapshot, annotation: dict[str, Any] | None = None
) -> None:
    """Decode retained workflow provenance without disabling unrelated canonical history.

    ``annotation`` is the lane's record of the details' workflow reference and, when
    needed, the checkpoint identity (:mod:`clio_agent.gact.task_lane_reads`); without
    one the details and checkpoint are read here.
    """
    from clio_agent.gact.task_lane_reads import checkpoint_identity, workflow_annotation

    if snapshot.details is None:
        return
    try:
        latest = self._workflow_latest.get(snapshot.handle)
        if annotation is None:
            known = latest.content if latest is not None else None
            annotation = workflow_annotation(self.read_content, snapshot, known=known)
        if annotation.get("reference") is None:
            return
        reference = CoreReference.model_validate(annotation["reference"])
        if latest is not None and latest.content == reference:
            # The owner points at a checkpoint recorded canonically (the flow journal
            # records it before pointing its owner at it): nothing legacy to decode.
            self._workflow_dirty.discard(snapshot.handle)
            return
        identity = annotation.get("checkpoint") or checkpoint_identity(self.read_content(reference))
        if not identity.get("complete"):
            raise ValueError("Historical workflow identity is incomplete")
        if (
            identity.get("root_handle", snapshot.handle) != snapshot.handle
            or identity.get("root_session", snapshot.owner.session_id) != snapshot.owner.session_id
        ):
            raise ValueError("Historical workflow checkpoint belongs to another owner")
        _project_workflow(
            self,
            WorkflowCheckpoint.model_validate(
                {
                    "root_handle": snapshot.handle,
                    "session_id": snapshot.owner.session_id,
                    "invocation_id": identity["invocation_id"],
                    "execution_id": identity["id"],
                    "declaration": identity["declaration"],
                    "request_digest": identity["request_digest"],
                    "content": reference,
                    "source": "legacy_snapshot",
                }
            ),
        )
        latest = self._workflow_latest.get(snapshot.handle)
        if latest is not None and latest.content == reference:
            self._workflow_dirty.discard(snapshot.handle)
    except (ValueError, OSError, ClioError) as error:
        logger.exception(
            "Historical workflow unavailable root=%s error_type=%s",
            snapshot.handle,
            type(error).__name__,
        )
        self._workflow_unresolved[snapshot.handle] = str(error)


def _encoded(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def _index_task_order(self: TreeState, snapshot: TaskSnapshot, *, known: bool) -> None:
    """Project immutable ordering and active tree membership from confirmed observations."""
    pending, seen = [snapshot.handle], set()
    while pending:
        handle = pending.pop()
        if handle in seen:
            continue
        seen.add(handle)
        task = self._tasks[handle]
        scopes, ancestry = {task.owner.session_id}, {handle}
        parent = task.owner.parent_handle
        while parent is not None and parent not in ancestry:
            ancestry.add(parent)
            ancestor = self._tasks.get(parent)
            if ancestor is None:
                break
            scopes.add(ancestor.owner.session_id)
            parent = ancestor.owner.parent_handle
        key = task.created_at, handle
        buckets = {("human", task.owner.human_id, ""), ("session", task.owner.session_id, "")}
        direct_parent = self._tasks.get(task.owner.parent_handle or "")
        if direct_parent is not None and direct_parent.kind in {"Session", "Subagent"}:
            buckets.update(
                ("direct", direct_parent.owner.session_id, human)
                for human in ("", task.owner.human_id)
            )
        buckets.update(
            ("tree", sid, human) for sid in scopes for human in ("", task.owner.human_id)
        )
        for domain, identity, human in buckets:
            for kind in ("", task.kind):
                for active in (False, True):
                    ordering = self._query_order.setdefault(
                        (domain, identity, human, kind, active), []
                    )
                    if self._replaying:
                        if not active or task.terminal_at is None:
                            ordering.append(key)
                        continue
                    at = bisect_left(ordering, key)
                    present = at < len(ordering) and ordering[at] == key
                    if active and task.terminal_at is not None:
                        if present:
                            ordering.pop(at)
                    elif not present:
                        ordering.insert(at, key)
        for sid in scopes:
            for human in ("", task.owner.human_id):
                self._tree_sessions.setdefault((sid, human), set()).add(task.owner.session_id)
            active_handles = self._active_trees.setdefault(sid, set())
            if task.terminal_at is None:
                active_handles.add(handle)
            else:
                active_handles.discard(handle)
        if not known:
            # Historical records may name a parent before its first observation.
            # Its eventual arrival extends only that known descendant branch.
            pending.extend(self._children.get(handle, ()))


logger = logging.getLogger(__name__)


_PROJECTION_FIELDS = (
    "_tasks",
    "_aliases",
    "_mail",
    "_pending",
    "_inputs",
    "_latest_inputs",
    "_notification_inputs",
    "_iterations",
    "_input_locations",
    "_acknowledged",
    "_observation_inputs",
    "_withdrawn",
    "_grants",
    "_submissions",
    "_attempts",
    "_deduplicated",
    "_session_tasks",
    "_kind_tasks",
    "_human_tasks",
    "_active_tasks",
    "_query_order",
    "_active_trees",
    "_tree_sessions",
    "_resource_tasks",
    "_assignments",
    "_children",
    "_recipient_grants",
    "_subscriptions",
    "_workflow_bindings",
    "_workflow_executions",
    "_workflow_latest",
    "_workflow_dirty",
    "_workflow_unresolved",
)


def _prepare_snapshot(self: TreeState, payload: dict[str, Any]) -> TaskSnapshot:
    """Resolve content before publishing; legacy vocabulary belongs to replay only."""
    record = payload.get("record", payload)
    lane = payload.get("lane") or {}
    snapshot = (
        TaskSnapshot.read_history(record)
        if self._replaying
        else TaskSnapshot.model_validate(record)
    )
    if snapshot.description_reference is not None:
        previous = self._tasks.get(snapshot.handle) if self._replaying else None
        if previous is not None and (
            previous.description_reference == snapshot.description_reference
        ):
            description = previous.description
        elif "description" in lane:
            description = lane["description"]  # recorded by the writer: no content read
        else:
            description = self.read_content(snapshot.description_reference)
        if not isinstance(description, str):
            raise ValueError("Task assignment content must be text")
        snapshot = snapshot.model_copy(update={"description": description})
    return snapshot


def _project(
    self: TreeState,
    event_type: str,
    payload: dict[str, Any],
    *,
    snapshot: TaskSnapshot | None = None,
) -> None:
    lane: dict[str, Any] = {}
    if "observation_id" in payload and "record" in payload:
        if payload.get("schema_version") != 1:
            raise ValueError("Unsupported task observation version; migration required")
        lane = payload.get("lane") or {}
        payload = payload["record"]
    if event_type == "task.alias":
        alias = TaskAlias.model_validate(payload)
        self._aliases.setdefault(alias.alias, {})[alias.source_identity] = alias
    elif event_type == "task.observed":
        if snapshot is None:
            raise ValueError("Task projection requires its prepared confirmed snapshot")
        prior_task = self._tasks.get(snapshot.handle)
        known = prior_task is not None
        self._tasks[snapshot.handle] = snapshot
        if (
            self._replaying
            and snapshot.kind in {"Session", "Subagent"}
            and snapshot.details
            and (prior_task is None or prior_task.details != snapshot.details)
        ):
            _legacy_workflow(self, snapshot, lane.get("workflow"))
        self._session_tasks.setdefault(snapshot.owner.session_id, set()).add(snapshot.handle)
        self._kind_tasks.setdefault(snapshot.kind, set()).add(snapshot.handle)
        self._human_tasks.setdefault(snapshot.owner.human_id, set()).add(snapshot.handle)
        if snapshot.resource_id is not None:
            self._resource_tasks.setdefault(snapshot.resource_id, set()).add(snapshot.handle)
        if snapshot.terminal_at is None:
            self._active_tasks.add(snapshot.handle)
        else:
            self._active_tasks.discard(snapshot.handle)
        if snapshot.kind in {"Session", "Subagent"}:
            if not known or snapshot.owner.session_id not in self._assignments:
                self._assignments[snapshot.owner.session_id] = snapshot.handle
        if snapshot.owner.parent_handle is not None:
            self._children.setdefault(snapshot.owner.parent_handle, set()).add(snapshot.handle)
        if snapshot.deduplication_key:
            key = (snapshot.owner.session_id, snapshot.deduplication_key)
            if snapshot.terminal_at is None and snapshot.cancellation_requested_at is None:
                self._deduplicated[key] = snapshot.handle
            elif self._deduplicated.get(key) == snapshot.handle:
                self._deduplicated.pop(key, None)
        if not self._replaying:
            _index_task_order(self, snapshot, known=known)
    elif event_type in {"session.mail.accepted", "session.mail.updated"}:
        item = MailItem.model_validate(payload)
        previous = self._mail.get(item.recipient_session, {}).get(item.id)
        self._mail.setdefault(item.recipient_session, {})[item.id] = item
        excluded = self._acknowledged.get(item.recipient_session, set()) | self._withdrawn.get(
            item.recipient_session, set()
        )
        if item.id not in excluded:
            if (
                previous is not None
                and previous.delivery == "queued"
                and item.delivery == "eligible"
            ):
                self._pending.get(item.recipient_session, {}).pop(item.id, None)
            self._pending.setdefault(item.recipient_session, {})[item.id] = item
    elif event_type == "session.mail.withdrawn":
        self._withdrawn.setdefault(payload["recipient_session"], set()).add(payload["id"])
        self._pending.get(payload["recipient_session"], {}).pop(payload["id"], None)
    elif event_type == "workflow.checkpoint":
        _project_workflow(self, WorkflowCheckpoint.model_validate(payload))
    elif event_type == "session.coordination.grant":
        grant = CoordinationGrant.model_validate(payload)
        self._grants[grant.id] = grant
        self._recipient_grants.setdefault(grant.recipient_session, set()).add(grant.id)
    elif event_type == "session.input.prepared":
        key = (payload["session_id"], payload["id"])
        previous_location = self._input_locations.get(key)
        if previous_location is not None and previous_location != payload:
            raise ValueError("Canonical input location identity was reused")
        self._input_locations[key] = payload
    elif event_type == "task.submission":
        self._submissions[payload["reference"]] = CoreReference.model_validate(payload["content"])
    elif event_type == "task.native_attempt":
        attempt = NativeAttempt.model_validate(payload)
        self._attempts.setdefault(attempt.handle, {})[attempt.incarnation] = attempt
    elif event_type == "task.subscription":
        subscription = TaskSubscription.model_validate(payload)
        self._subscriptions.setdefault(subscription.handle, {})[subscription.recipient_session] = (
            subscription
        )
    else:
        return
    self.revision += 1


def _scope_inputs(
    self: TreeState | TaskHistory, session_id: str, scope: str
) -> dict[str, RecipientInput]:
    records: dict[str, RecipientInput] = {}
    for segment in self.arc.list_segments(session_id, scope, include_tombstoned=True):
        if segment.kind != "recipient_input":
            continue
        if "schema_version" not in segment.content:
            continue  # decoded once by the data-only historical migration
        record = RecipientInput.model_validate(segment.content)
        if record.session_id != session_id or record.boundary.scope != scope:
            raise ValueError("Recipient input is stored under a different context")
        previous = records.get(record.id)
        if previous is not None and previous != record:
            raise ValueError("Conflicting canonical recipient inputs")
        records[record.id] = record
    return records


def _find_input(
    self: TreeState, session_id: str, scope: str, identity: str
) -> RecipientInput | None:
    return self.context_inputs(session_id, scope).get(identity)


def _accept_input(self: TreeState, record: RecipientInput) -> None:
    from clio_agent.gact.context import scope_epoch

    key = (record.session_id, record.id)
    previous = self._inputs.get(key)
    if previous is not None:
        if previous != record:
            raise ValueError("Recipient input identity was reused")
        return
    _validate_input_eligibility(self, record)
    self._inputs[key] = record
    for epoch in (None, scope_epoch(record.boundary.scope)):
        latest_key = (record.assignment_id, epoch)
        latest = self._latest_inputs.get(latest_key)
        if latest is None or record.committed_at > latest.committed_at:
            self._latest_inputs[latest_key] = record
    self._iterations[(record.session_id, record.assignment_id, record.iteration_id)] = record.id
    self._acknowledged.setdefault(record.session_id, set()).update(record.notification_ids)
    self._acknowledged[record.session_id].update(record.legacy_notification_ids)
    if record.tool_observation is not None:
        reference = record.tool_observation
        self._observation_inputs.setdefault((reference.session_id, reference.scope), set()).add(
            reference.content_id
        )
    for notification_id in record.notification_ids:
        self._notification_inputs[(record.session_id, notification_id)] = record.id
        self._pending.get(record.session_id, {}).pop(notification_id, None)
    self.revision += 1


def _validate_observed_input(self: TreeState, record: RecipientInput) -> None:
    """An empty/subset acknowledgement cannot authenticate undisclosed result members."""
    reference = record.tool_observation
    if reference is None:
        return
    if reference.session_id != record.session_id or reference.scope != record.boundary.scope:
        raise ValueError("Collected observation belongs to a different recipient context")
    observation = self.read_content(reference)
    identities = observation.get("notification_ids", ()) if isinstance(observation, dict) else ()
    if not isinstance(identities, (list, tuple)):
        raise ValueError("Collected observation notification identities are malformed")
    accepted = set(record.notification_ids) | self.acknowledged_notifications(record.session_id)
    required: list[str] = []
    for identity in identities:
        if not isinstance(identity, str):
            raise ValueError("Collected observation notification identities are malformed")
        required.append(identity)
        item = self.mail_item(record.session_id, identity)
        if (
            self.mail_withdrawn(record.session_id, identity)
            and item is not None
            and item.kind in {"task_result", "question_answer"}
            and item.handle is not None
        ):
            accepted.add(identity)
    if not set(required) <= accepted:
        raise ValueError("Collected observation contains results outside the committed selection")


def _validate_input_eligibility(self: TreeState, record: RecipientInput) -> None:
    iteration = (record.session_id, record.assignment_id, record.iteration_id)
    if iteration in self._iterations:
        raise ValueError("The model iteration already has a committed input")
    withdrawn = self._withdrawn.get(record.session_id, set())
    acknowledged = self._acknowledged.get(record.session_id, set())
    if any(identity in withdrawn for identity in record.notification_ids):
        raise ValueError("Input includes withdrawn mail; restage the model input")
    if any(identity in acknowledged for identity in record.notification_ids):
        raise ValueError("Input includes already committed mail; restage the model input")
