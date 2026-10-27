"""GACT's confirmed Core task observations, mail and recipient-input records.

This writer neither executes work nor decides when it finishes. Execution owners
publish snapshots; the conversation engine commits inputs. clio-core is the
memory: each task tree's passive indexes are loaded on demand from its member
sessions' event logs (:mod:`clio_agent.gact.task_tree_state`) and found through
derived lookup/open-work records (:mod:`clio_agent.gact.task_index_records`).
Nothing is replayed globally at boot. All methods perform finite store I/O and
must run off the application event loop.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter

from clio_agent.arc.loop_guard import assert_store_write_off_loop
from clio_agent.arc.task_types import (
    CoordinationGrant,
    CoreReference,
    MailItem,
    NativeAttempt,
    RecipientInput,
    SubmissionContext,
    TaskAlias,
    TaskSnapshot,
    TaskSubscription,
    WorkflowCheckpoint,
)
from clio_agent.errors import ClioError
from clio_agent.gact import (
    task_event_lane,
    task_history_projection,
    task_history_queries,
    task_index_build,
    task_index_writes,
)
from clio_agent.gact.core_io import CoreIO
from clio_agent.gact.task_history_projection import _encoded
from clio_agent.gact.task_index_records import TaskIndex
from clio_agent.gact.task_tree_state import TreeCache, TreeState

if TYPE_CHECKING:
    from clio_agent.arc.memory import ARCMemory

CONTENT_SCOPE = "_events/t/content/"
OUTPUT_SCOPE = "_events/t/output/"
_CONTENT_SCOPES = (CONTENT_SCOPE, "_task_content/")
_OUTPUT_SCOPES = (OUTPUT_SCOPE, "_task_output/")
logger = logging.getLogger(__name__)
_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def output_scope(handle: str) -> str:
    """Derive a task-local content namespace for untrusted observation continuations."""
    return OUTPUT_SCOPE + hashlib.sha256(handle.encode()).hexdigest() + "/"


def owns_output_reference(handle: str, reference: CoreReference) -> bool:
    """Bind current and retained output addresses to their original exact task."""
    suffix = hashlib.sha256(handle.encode()).hexdigest() + "/" + reference.content_id
    return any(reference.scope == prefix + suffix for prefix in _OUTPUT_SCOPES)


class MailboxFull(ValueError):
    """Retryable refusal of new peer mail under an explicit pending-only limit."""


class RecordingUnconfirmed(RuntimeError):
    """A prior Core write needs reconciliation before accepting new durable input."""


class TaskHistory:
    """One serialized writer over the existing ARC/Core record and context lanes."""

    def __init__(self, arc: ARCMemory) -> None:
        self.arc = arc
        self.io = CoreIO()
        self.recovery_requested: Callable[[], None] | None = None
        self.observation_committed: Callable[[TaskSnapshot], None] | None = None
        self._requires_recovery = False
        self._lock = threading.RLock()
        self._view_lock = threading.RLock()
        self.index = TaskIndex(arc._store)
        self.trees = TreeCache(self)
        # Set only between a one-time build's full replay and its record emission.
        self._global: TreeState | None = None
        self.index_build_pending = False
        self.revision = 0
        self.replay_seconds = 0.0
        self.requires_recovery = False
        self._listeners: set[Callable[[int], None]] = set()
        self._handle_listeners: dict[str, set[Callable[[int], None]]] = {}
        self._recipient_listeners: dict[str, set[Callable[[int], None]]] = {}
        self._mail_listeners: set[Callable[[str], None]] = set()
        self._listener_lock = threading.RLock()

    @property
    def requires_recovery(self) -> bool:
        """Whether confirmed Core writing must reconcile before new admission."""
        return self._requires_recovery

    @requires_recovery.setter
    def requires_recovery(self, value: bool) -> None:
        changed = value and not self._requires_recovery
        self._requires_recovery = value
        if changed and self.recovery_requested is not None:
            self.recovery_requested()

    _session_state = task_history_queries.session_state
    _handle_state = task_history_queries.handle_state
    _load_tree = task_history_queries.load_session_tree

    def write_content(self, session_id: str, value: object) -> CoreReference:
        """Confirm content once, before an observation or mailbox item refers to it."""
        return self._write_content(session_id, value, CONTENT_SCOPE)

    def write_output(self, handle: str, value: object) -> CoreReference:
        """Confirm output in its execution's namespace using the same content writer."""
        with self._lock:
            task = self.task_snapshot(handle)
            if task is None:
                raise ValueError("Output task is unavailable")
            return self._write_content(task.owner.session_id, value, output_scope(handle))

    def _write_content(self, session_id: str, value: object, scope: str) -> CoreReference:
        assert_store_write_off_loop("task.content")
        value = _JSON_VALUE.validate_python(value, strict=True)
        digest = hashlib.sha256(_encoded(value)).hexdigest()
        content_id = digest
        reference = CoreReference(
            session_id=session_id,
            scope=scope + content_id,
            content_id=content_id,
            sha256=digest,
        )
        with self._writing():
            existing = self.arc.list_segments(session_id, reference.scope)
            if existing:
                self.read_content(reference)
                return reference
            self.confirm_write(
                lambda: self.arc.append_segment(
                    session_id,
                    reference.scope,
                    "task_content",
                    {"content_id": content_id, "sha256": digest, "value": value},
                )
            )
        return reference

    def read_content(self, reference: CoreReference) -> JsonValue:
        """Read an exact reference, rejecting missing or changed content."""
        assert_store_write_off_loop("task.content.read")
        return self._read_content(reference)

    def _read_content(self, reference: CoreReference) -> JsonValue:
        scoped_output = any(
            reference.scope.startswith(prefix)
            and reference.scope[len(prefix) :].count("/") == 1
            and reference.scope.endswith("/" + reference.content_id)
            for prefix in _OUTPUT_SCOPES
        )
        if (
            not any(reference.scope == prefix + reference.content_id for prefix in _CONTENT_SCOPES)
            and not scoped_output
        ):
            rows = [
                row
                for row in self.arc.list_segments(
                    reference.session_id, reference.scope, include_tombstoned=True
                )
                if str(row.id) == reference.content_id
                and row.kind in {"observation", "recipient_input", "thought"}
            ]
            if (
                len(rows) != 1
                or hashlib.sha256(_encoded(rows[0].content)).hexdigest() != reference.sha256
            ):
                raise ValueError("Invalid or changed tool observation reference")
            return rows[0].content
        rows = self.arc.list_segments(reference.session_id, reference.scope)
        if len(rows) != 1:
            raise ValueError("Task content is absent or ambiguous")
        content = rows[0].content
        value = content.get("value")
        digest = hashlib.sha256(_encoded(value)).hexdigest()
        if content.get("content_id") != reference.content_id or digest != reference.sha256:
            raise ValueError("Task content failed integrity validation")
        return value

    def record_snapshot(self, snapshot: TaskSnapshot, *, observation_id: str) -> None:
        """Record an owner's observation without imposing cross-owner transitions."""
        assert_store_write_off_loop("task.observation")
        snapshot = TaskSnapshot.model_validate(snapshot)
        with self._writing():
            previous = self.task_snapshot(snapshot.handle)
            snapshot.validate_identity(previous)
            replacement = (
                SubmissionContext.model_validate(self.read_content(snapshot.submission))
                if previous is not None
                and previous.submission is not None
                and snapshot.submission is not None
                and previous.submission != snapshot.submission
                else None
            )
            snapshot.validate_custody(previous, replacement)
            if snapshot.result is not None:
                self.read_content(snapshot.result)
            reference = (
                previous.description_reference
                if previous is not None
                else snapshot.description_reference
            )
            if snapshot.description is not None and reference is None:
                reference = self.write_content(snapshot.owner.session_id, snapshot.description)
            if reference is not None and self.read_content(reference) != snapshot.description:
                raise ValueError("Task description reference differs from its immutable assignment")
            snapshot = snapshot.model_copy(update={"description_reference": reference})
            stored = snapshot.model_dump(mode="json")
            if reference is not None:
                stored["description"] = None
            state = task_index_writes.open_task(self, previous, snapshot)
            self._record(
                "task.observed",
                snapshot.owner.session_id,
                observation_id,
                stored,
            )
            self._project(state, "task.observed", stored)
            task_index_writes.close_task(self, previous, snapshot)
            if self.observation_committed is not None:
                try:
                    self.observation_committed(snapshot)
                except Exception:
                    logger.exception(
                        "Task UI hint failed after confirmed observation handle=%s", snapshot.handle
                    )

    def record_mail(self, item: MailItem, *, max_pending_messages: int = 0) -> MailItem:
        """Accept durable session mail; only pending peer messages count toward a cap."""
        assert_store_write_off_loop("task.mail")
        if max_pending_messages < 0:
            raise ValueError("Pending-message limit must be nonnegative")
        with self._writing():
            state = self._session_state(item.recipient_session)
            existing = state._mail.get(item.recipient_session, {}).get(item.id)
            if existing is not None:
                if existing != item:
                    raise ValueError("Mailbox identity was reused with different content")
                return existing
            if item.kind == "peer" and max_pending_messages:
                count = sum(row.kind == "peer" for row in self.pending_mail(item.recipient_session))
                if count >= max_pending_messages:
                    raise MailboxFull("Undelivered peer-message capacity reached; retry later")
            self.read_content(item.content)
            payload = item.model_dump(mode="json")
            task_index_writes.open_mail(self, item)
            self._record("session.mail.accepted", item.recipient_session, item.id, payload)
            self._project(state, "session.mail.accepted", payload)
            return item

    def record_submission(self, context: SubmissionContext) -> CoreReference:
        """Confirm trusted context once and index its opaque worker-only reference."""
        with self._writing():
            existing = task_history_queries.submission(self, context.reference)
            if existing is not None:
                if self.read_content(existing) != context.model_dump(mode="json"):
                    raise ValueError("Submission reference cannot be reused")
                return existing
            state = self._session_state(context.owner.session_id)
            content = self.write_content(context.owner.session_id, context.model_dump(mode="json"))
            payload = {"reference": context.reference, "content": content.model_dump(mode="json")}
            task_index_writes.open_reference(self, context)
            self._record("task.submission", context.owner.session_id, context.reference, payload)
            self._project(state, "task.submission", payload)
            return content

    def submission_context(self, reference: str) -> SubmissionContext:
        """Resolve an exact confirmed worker reference without scanning other tasks."""
        with self._lock:
            content = task_history_queries.submission(self, reference)
            if content is None:
                raise ValueError("Submission reference is unavailable")
            return SubmissionContext.model_validate(self.read_content(content))

    def record_attempt(self, attempt: NativeAttempt) -> None:
        """Retain native admission/cleanup evidence under its unique incarnation."""
        with self._writing():
            state = self._handle_state(attempt.handle)
            previous = (
                state._attempts.get(attempt.handle, {}).get(attempt.incarnation) if state else None
            )
            if previous is not None:
                lifecycle = {
                    "cleanup_receipt": None,
                    "finished_at": None,
                    "cleanup_state": "pending",
                }
                if previous.model_copy(update=lifecycle) != (attempt.model_copy(update=lifecycle)):
                    raise ValueError("Native incarnation identity cannot change")
                if previous.cleanup_state in {"confirmed", "never_started"} and previous != attempt:
                    raise ValueError("Native cleanup evidence cannot be replaced")
                if previous == attempt:
                    return
            current = self.task_snapshot(attempt.handle)
            if (
                state is None
                or current is None
                or (previous is None and current.backend != attempt.backend)
            ):
                raise ValueError("Native admission does not match the bound task")
            if attempt.cleanup_receipt is not None:
                receipt = self.read_content(attempt.cleanup_receipt)
                if (
                    not isinstance(receipt, dict)
                    or receipt.get("incarnation") != attempt.incarnation
                    or receipt.get("execution_key") != attempt.execution_key
                ):
                    raise ValueError("Cleanup receipt belongs to another native incarnation")
            payload = attempt.model_dump(mode="json")
            self._record("task.native_attempt", current.owner.session_id, uuid4().hex, payload)
            self._project(state, "task.native_attempt", payload)

    def native_attempts(self, handle: str) -> tuple[NativeAttempt, ...]:
        """Read observed attempts; a receipt never settles a different incarnation."""
        with self._lock:
            state = self._handle_state(handle)
            return tuple(state._attempts.get(handle, {}).values()) if state else ()

    def withdraw_mail(self, session_id: str, notification_id: str) -> None:
        """Withdraw uncommitted input; committed model input cannot be rewritten."""
        assert_store_write_off_loop("task.mail.withdraw")
        with self._writing():
            state = self._session_state(session_id)
            if notification_id in state._acknowledged.get(session_id, set()):
                raise ValueError("The input was already committed to a model iteration")
            if notification_id not in state._mail.get(session_id, {}):
                raise KeyError(notification_id)
            payload = {"recipient_session": session_id, "id": notification_id}
            self._record("session.mail.withdrawn", session_id, uuid4().hex, payload)
            self._project(state, "session.mail.withdrawn", payload)
            task_index_writes.close_mail(self, session_id, (notification_id,))

    def acknowledged_notifications(self, session_id: str) -> frozenset[str]:
        """Return confirmed input identities; uncommitted context is not delivered."""
        with self._lock:
            return self._session_state(session_id).acknowledged_notifications(session_id)

    def committed_observations(self, session_id: str, scope: str) -> frozenset[str]:
        """Read durable collection proofs even after their input record was compacted."""
        with self._lock:
            state = self._session_state(session_id)
            return frozenset(state._observation_inputs.get((session_id, scope), ()))

    def latest_input(
        self, assignment_id: str, *, epoch: str | None = None
    ) -> RecipientInput | None:
        """Read the latest confirmed assignment input from its rebuildable projection."""
        with self._lock:
            return task_history_queries.latest_input(self, assignment_id, epoch)

    def committed_notification_input(self, session_id: str, identity: str) -> RecipientInput | None:
        """Resolve a confirmed exact notification input without scanning retained session history."""
        with self._lock:
            state = self._session_state(session_id)
            record_id = state._notification_inputs.get((session_id, identity))
            return state._inputs.get((session_id, record_id)) if record_id is not None else None

    def record_input(self, record: RecipientInput) -> RecipientInput:
        """Confirm the sole delivery acknowledgement in the recipient's context lane.

        A canonical preparation event names the context address before its write.
        Startup can rediscover the confirmed input even after all passive indexes
        are deleted. Preparation alone never acknowledges a notification.
        """
        assert_store_write_off_loop("task.input")
        if record.boundary.session_id != record.session_id:
            raise ValueError("Recipient context belongs to another session")
        if len(set(record.notification_ids)) != len(record.notification_ids):
            raise ValueError("An input cannot include a notification twice")
        if len(record.notification_ids) != len(record.content):
            raise ValueError("Each notification must name its exact content reference")
        with self._writing():
            state = self._session_state(record.session_id)
            key = (record.session_id, record.id)
            previous = state._inputs.get(key)
            if previous is not None:
                if previous != record:
                    raise ValueError("Recipient input identity was reused")
                return previous
            task_history_projection._validate_input_eligibility(state, record)
            if record.legacy_notification_ids:
                if (
                    record.legacy_proof is None
                    or record.legacy_proof.session_id != record.session_id
                ):
                    raise ValueError(
                        "Legacy acknowledgements require their original recipient input"
                    )
                proof = self.read_content(record.legacy_proof)
                proof_ids = proof.get("notification_ids") if isinstance(proof, dict) else None
                if (
                    not isinstance(proof_ids, list)
                    or any(not isinstance(value, str) for value in proof_ids)
                    or not set(record.legacy_notification_ids) <= set(proof_ids)
                ):
                    raise ValueError("Legacy acknowledgement lacks a confirmed context record")
            for notification_id, reference in zip(
                record.notification_ids, record.content, strict=True
            ):
                self.read_content(reference)
                item = state._mail.get(record.session_id, {}).get(notification_id)
                if item is not None and item.content != reference:
                    raise ValueError("Input content differs from the accepted mailbox item")
            if record.tool_observation is not None:
                task_history_projection._validate_observed_input(state, record)
            from clio_agent.gact.agents.clio_react_record import _token_estimate

            input_tokens = 0
            for reference in record.rendered_content:
                value = self.read_content(reference)
                if not isinstance(value, dict):
                    raise ValueError("Rendered model input must be a message record")
                input_tokens += _token_estimate(value)
            payload = record.model_dump(mode="json")
            locator = {
                "session_id": record.session_id,
                "scope": record.boundary.scope,
                "id": record.id,
            }
            existing_locator = state._input_locations.get(key)
            if existing_locator is not None:
                if existing_locator != locator:
                    raise ValueError("Input locator identity was reused")
                recovered = task_history_projection._find_input(
                    state, record.session_id, record.boundary.scope, record.id
                )
                if recovered is not None:
                    if recovered != record:
                        raise ValueError("Durable input identity was reused")
                    self.confirm_write(
                        lambda: task_history_projection._accept_input(state, recovered)
                    )
                    task_index_writes.close_mail(self, record.session_id, record.notification_ids)
                    return recovered
            else:
                self._record("session.input.prepared", record.session_id, record.id, locator)
                self._project(state, "session.input.prepared", locator)
            task_index_writes.open_display(self, state, record)
            self.confirm_write(
                lambda: self.arc.append_segment(
                    record.session_id,
                    record.boundary.scope,
                    "recipient_input",
                    payload,
                    turn_id=record.turn_id or record.iteration_id,
                    token_count=input_tokens,
                )
            )
            self.confirm_write(lambda: task_history_projection._accept_input(state, record))
            task_index_writes.close_mail(self, record.session_id, record.notification_ids)
            return record

    def record_grant(self, grant: CoordinationGrant, *, observation_id: str) -> None:
        """Record an already authenticated human/configuration relationship command."""
        assert_store_write_off_loop("task.grant")
        with self._writing():
            state = self._session_state(grant.recipient_session)
            previous = state._grants.get(grant.id)
            if (
                previous is not None
                and previous.model_copy(update={"revoked_at": grant.revoked_at}) != grant
            ):
                raise ValueError("A grant cannot silently change its recipient, scope or rights")
            if previous is not None and previous.revoked_at is not None and previous != grant:
                raise ValueError("A revoked grant is historical; issue a new authenticated grant")
            payload = grant.model_dump(mode="json")
            self._record(
                "session.coordination.grant", grant.recipient_session, observation_id, payload
            )
            self._project(state, "session.coordination.grant", payload)

    def task_snapshot(self, handle: str) -> TaskSnapshot | None:
        """Read the internal passive view; public controls must authorize first."""
        state = self._handle_state(handle)
        if state is None:
            return None
        with self._view_lock:
            return state._tasks.get(handle)

    def resolve_handle(self, value: str) -> str | None:
        """Resolve an exact public handle or an unambiguous historical alias only."""
        handles = {row.handle for row in self.aliases(value)}
        if self.task_snapshot(value) is not None:
            handles.add(value)
        return next(iter(handles)) if len(handles) == 1 else None

    def record_alias(self, alias: TaskAlias) -> None:
        """Confirm a migration mapping without retargeting its original execution."""
        with self._writing():
            previous = next(
                (
                    row
                    for row in self.aliases(alias.alias)
                    if row.source_identity == alias.source_identity
                ),
                None,
            )
            if previous is not None:
                if previous != alias:
                    raise ValueError("Historical identity mapping cannot be replaced")
                return
            task = self.task_snapshot(alias.handle)
            state = self._handle_state(alias.handle)
            if task is None or state is None:
                raise ValueError("Alias target has not been confirmed")
            payload = alias.model_dump(mode="json")
            task_index_writes.open_alias(self, alias)
            self._record("task.alias", task.owner.session_id, uuid4().hex, payload)
            self._project(state, "task.alias", payload)

    aliases = task_history_queries.aliases

    def record_source_operation_migration(
        self, session_id: str, identity: str, payload: dict[str, Any]
    ) -> None:
        """Confirm retained storage evidence before obsolete SQLite fields are pruned."""
        with self._writing():
            self._record("task.migration.source_operation", session_id, identity, payload)

    listen = task_history_queries.listen
    listen_mail = task_history_queries.listen_mail

    def update_mail(self, item: MailItem, *, expected_revision: int) -> MailItem:
        """Edit/promote accepted but uncommitted input without changing its identity."""
        with self._writing():
            state = self._session_state(item.recipient_session)
            current = state._mail.get(item.recipient_session, {}).get(item.id)
            if current is None or current.revision != expected_revision:
                raise ValueError("Mailbox revision changed; refresh before retrying")
            if item.id in state._acknowledged.get(
                item.recipient_session, set()
            ) or item.id in state._withdrawn.get(item.recipient_session, set()):
                raise ValueError("Committed or withdrawn input cannot be edited")
            if (
                item.sender_session != current.sender_session
                or item.sender_human != current.sender_human
                or item.kind != current.kind
                or item.created_at != current.created_at
                or item.revision != current.revision + 1
            ):
                raise ValueError("Mailbox identity or revision cannot be replaced")
            self.read_content(item.content)
            payload = item.model_dump(mode="json")
            self._record("session.mail.updated", item.recipient_session, uuid4().hex, payload)
            self._project(state, "session.mail.updated", payload)
            return item

    def equivalent_active_task(self, session_id: str, key: str) -> TaskSnapshot | None:
        """Resolve a reserved equivalent request without another scheduler or owner scan."""
        with self._lock:
            state = self._session_state(session_id)
            handle = state._deduplicated.get((session_id, key))
            return state._tasks.get(handle) if handle is not None else None

    tree_handles = task_history_queries.tree_handles
    descendant_handles = task_history_queries.descendant_handles
    direct_child_by_invocation = task_history_queries.direct_child_by_invocation
    session_handles = task_history_queries.session_handles
    task_handles = task_history_queries.task_handles
    open_handles = task_history_queries.open_handles
    resource_handles = task_history_queries.resource_handles
    task_order = task_history_queries.task_order
    session_tree_ids = task_history_queries.session_tree_ids
    pending_sessions = task_history_queries.pending_sessions
    acknowledged_mail = task_history_queries.acknowledged_mail
    pending_workflow_references = task_history_queries.pending_workflow_references
    display_settled = task_index_writes.display_settled
    task_obligation_settled = task_index_writes.task_obligation_settled
    reconcile_open = task_index_writes.reconcile_open
    release_session_tree = task_index_writes.release_session_tree
    complete_index_build = task_index_build.complete_index_build

    def latest_assignment(self, session_id: str) -> TaskSnapshot | None:
        """Resolve a stable session's latest finite assignment from its passive Core index."""
        state = self._session_state(session_id)
        with self._view_lock:
            handle = state._assignments.get(session_id)
            return state._tasks.get(handle) if handle is not None else None

    def assignment_turns(self, handle: str) -> frozenset[str]:
        """Read admitted turn identities from the existing session mailbox headers."""
        state = self._handle_state(handle)
        with self._view_lock:
            task = state._tasks.get(handle) if state is not None else None
            if state is None or task is None:
                return frozenset()
            return frozenset(
                item.admission.turn_id
                for item in state._mail.get(task.owner.session_id, {}).values()
                if item.admission is not None and item.admission.assignment_id == handle
            )

    def coordination_grants(self, recipient_session: str) -> tuple[CoordinationGrant, ...]:
        """Read exact human/configuration relationships for one recipient."""
        state = self._session_state(recipient_session)
        with self._view_lock:
            return tuple(
                state._grants[key] for key in state._recipient_grants.get(recipient_session, set())
            )

    def record_subscription(self, subscription: TaskSubscription) -> None:
        """Confirm an exact-handle notification preference; the caller authorizes first.

        The record is filed under the task owner's session, so the owner's tree
        sees every subscriber of its tasks without reading other trees.
        """
        with self._writing():
            task = self.task_snapshot(subscription.handle)
            if task is None:
                raise ValueError("Subscription target is unavailable")
            owner = task.owner.session_id
            state = self._session_state(owner)
            payload = subscription.model_dump(mode="json")
            self._record("task.subscription", owner, uuid4().hex, payload)
            self._project(state, "task.subscription", payload)
            state.subscription_files[(subscription.handle, subscription.recipient_session)] = owner

    def subscriptions(self, handle: str) -> tuple[TaskSubscription, ...]:
        """Read subscribers from the passive relationship index, without per-subscriber workers."""
        state = self._handle_state(handle)
        with self._view_lock:
            return tuple(state._subscriptions.get(handle, {}).values()) if state else ()

    def pending_mail(self, session_id: str) -> tuple[MailItem, ...]:
        """Read pending inputs without claiming, consuming or imposing retention."""
        assert_store_write_off_loop("task.mail.read")
        state = self._session_state(session_id)
        with self._view_lock:
            return tuple(state._pending.get(session_id, {}).values())

    @contextmanager
    def read_view(self) -> Iterator[None]:
        """Read last-confirmed indexes without waiting for an in-flight Core RPC."""
        with self._view_lock:
            yield

    @contextmanager
    def serialized(self, *, writing: bool = False) -> Iterator[None]:
        """Serialize owner operations; durable admission also respects recording holds.

        Each write confirms separately. This boundary is not a transaction and
        cannot roll back a session or task observation whose reply was lost.
        """
        with self._writing() if writing else self._lock:
            yield

    def mail_item(self, session_id: str, identity: str) -> MailItem | None:
        """Read one immutable mailbox header without enumerating retained messages."""
        with self._lock:
            return self._session_state(session_id).mail_item(session_id, identity)

    def require_mail(self, session_id: str, identity: str) -> MailItem:
        """Read a header already required by an owner operation, or report its absence."""
        item = self.mail_item(session_id, identity)
        if item is None:
            raise KeyError(identity)
        return item

    def mail_withdrawn(self, session_id: str, identity: str) -> bool:
        """Read a retained withdrawal without exposing the mutable projection."""
        with self._lock:
            return self._session_state(session_id).mail_withdrawn(session_id, identity)

    def recipient_input(self, session_id: str, identity: str) -> RecipientInput | None:
        """Read one confirmed recipient-input record by its stable identity."""
        with self._lock:
            return self._session_state(session_id)._inputs.get((session_id, identity))

    def workflow_reference_error(self, root: str) -> str | None:
        """Report unavailable provenance without publishing an execution outcome."""
        with self._lock:
            state = self._handle_state(root)
            return state._workflow_unresolved.get(root) if state else None

    def workflow_reference_unavailable(self, root: str, reason: str) -> None:
        """Retain a decoder failure in the rebuildable provenance projection."""
        with self._lock:
            state = self._handle_state(root)
            if state is not None:
                state._workflow_unresolved[root] = reason

    def workflow_reference_repaired(self, root: str) -> None:
        """Clear projection flags after the owner confirms the current provenance reference."""
        with self._lock:
            state = self._handle_state(root)
            if state is not None:
                state._workflow_dirty.discard(root)
                state._workflow_unresolved.pop(root, None)
            task_index_writes.close_workflow(self, root)

    def record_intervention(self, session_id: str, identity: str, payload: dict[str, Any]) -> None:
        """Confirm an authenticated owner's control record through the same Core writer."""
        with self._writing():
            self._record("task.human.intervention", session_id, identity, payload)

    def restore(self) -> None:
        """Start (or restart after a write failure) without replaying Core.

        Once the derived records exist this only drops cached trees; each tree
        reloads from its own sessions on next use. Before the one-time build it
        replays everything once (:mod:`clio_agent.gact.task_index_build`).
        """
        return task_index_build.restore(self)

    def record_workflow(self, record: WorkflowCheckpoint) -> None:
        """Confirm owner-authored checkpoint provenance, then update passive indexes."""
        task_history_projection.record_workflow(self, record)

    def workflow_checkpoint(
        self, root: str, invocation: str | None = None
    ) -> WorkflowCheckpoint | None:
        """Resolve an exact invocation without scanning history or retargeting a retry."""
        with self._lock:
            state = self._handle_state(root)
            if state is None:
                return None
            binding = (
                state._workflow_bindings.get((root, invocation))
                if invocation is not None
                else state._workflow_latest.get(root)
            )
            return state._workflow_executions.get((root, binding.execution_id)) if binding else None

    @contextmanager
    def _writing(self) -> Iterator[None]:
        with self._lock:
            if self.requires_recovery:
                raise RecordingUnconfirmed("Core observations require reconciliation")
            try:
                yield
            except (ClioError, OSError, TimeoutError):
                self.requires_recovery = True
                raise

    def _record(
        self, event_type: str, session_id: str, identity: str, payload: dict[str, Any]
    ) -> None:
        # Import only the existing data envelope; no GACT application or execution owner.
        from clio_agent.gact.semantic_events import SemanticEvent

        assert_store_write_off_loop("task.observation")
        self.confirm_write(  # the task-event lane first, then the canonical log
            lambda: task_event_lane.record(
                self.arc,
                SemanticEvent(
                    event_type=event_type,
                    session_id=session_id,
                    trace_id="",
                    span_id=identity,
                    payload={"schema_version": 1, "observation_id": identity, "record": payload},
                ),
            )
        )

    def confirm_write(self, operation: Callable[[], Any]) -> Any:
        """Confirm an ARC write, holding later durable admission if its outcome is uncertain."""
        try:
            return operation()
        except Exception:
            # This boundary covers actual persistence/derivation, not ordinary
            # validation. A sink can fail after the Core append succeeded.
            self.requires_recovery = True
            raise

    def _project(self, state: TreeState, event_type: str, payload: dict[str, Any]) -> None:
        def project() -> None:
            snapshot = None
            if event_type == "task.observed":
                snapshot = task_history_projection._prepare_snapshot(state, payload)
            with self._view_lock:
                task_history_projection._project(state, event_type, payload, snapshot=snapshot)

        self.confirm_write(project)
        task_history_queries.notify_projected(self, state, event_type, payload)

    def context_inputs(self, session_id: str, scope: str) -> dict[str, RecipientInput]:
        """Read proven model inputs for one fixed Core context scope."""
        return task_history_projection._scope_inputs(self, session_id, scope)
