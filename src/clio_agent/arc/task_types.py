"""Shared task and session-mail records, without execution or delivery policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

TaskKind = Literal["Shell", "Download", "Indexing", "MCP", "Session", "Subagent", "Question"]
TaskStatus = Literal[
    "accepted",
    "queued",
    "running",
    "working",
    "active",
    "input_required",
    "paused",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
    "task_state_uncertain",
    "unknown",
]
TaskActivity = Literal[
    "",
    "busy",
    "executing",
    "idle",
    "yielded",
    "stopping",
    "stopped",
    "interrupted",
    "cancelling",
    "paused",
    "recovering",
    "unavailable",
    "waiting_for_input",
    "input_required",
    "submitting",
    "queued",
    "pending",
]
TaskSettlement = Literal[
    "unknown",
    "confirmed",
    "external_acknowledged",
    "never_started",
    "cleanup_pending",
    "historical_unknown",
]
TaskFreshness = Literal["fresh", "lost", "unavailable", "unknown"]
CoordinationRight = Literal["read", "observe", "message", "control"]
MailKind = Literal[
    "human",
    "peer",
    "delegation",
    "task_result",
    "attention",
    "question_answer",
    "control",
    "goal",
    "loop",
    "schedule",
    "hook",
    "a2ui",
    "spotter",
    "memory",  # a memory curator's brief (gact/memory/background.py)
    "approval",
    "composer",
]


class TaskRecord(BaseModel):
    """Immutable validated data written through Core's confirmed writer."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1


def argument_digest(arguments: Mapping[str, object]) -> str:
    """Bind confirmed custody to the exact JSON arguments sent to the backend."""
    encoded = json.dumps(
        dict(arguments), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class CoreReference(TaskRecord):
    """Address and integrity of content held once in the Core hierarchy."""

    session_id: str
    scope: str
    content_id: str
    sha256: str


class TaskOwner(TaskRecord):
    """Original human, stable session and finite assignment owning accepted work."""

    human_id: str
    session_id: str
    assignment_id: str
    parent_handle: str | None = None


class BackendReference(TaskRecord):
    """Complete native identity; transport and placement are independent attributes."""

    owner: Literal["native", "external_mcp", "conversation", "question"]
    server: str = ""
    auth_scope: str = ""
    session: str = ""
    task_id: str = ""
    queue: str = ""
    execution_key: str = ""
    transport: str = ""
    placement: str = "local"


class TaskProgress(TaskRecord):
    """Owner-reported progress, with no invented percentage or terminal inference."""

    message: str = ""
    completed: float | None = Field(default=None, ge=0)
    total: float | None = Field(default=None, ge=0)
    unit: str = ""
    output: CoreReference | None = None


class TaskSnapshot(TaskRecord):
    """One passive common view; the execution owner supplies status and settlement."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")
    handle: str
    kind: TaskKind
    owner: TaskOwner
    description: str | None = None
    description_reference: CoreReference | None = None
    description_missing: bool = True
    label: str
    invocation_id: str
    tool: str
    created_at: str
    updated_at: str
    backend: BackendReference | None = None
    raw_status: str = ""
    status: TaskStatus = "accepted"
    activity: TaskActivity = ""
    progress: TaskProgress = Field(default_factory=TaskProgress)
    supported_actions: tuple[str, ...] = ()
    cancellation_requested_at: str | None = None
    connection_freshness: TaskFreshness = "unknown"
    settlement: TaskSettlement = "unknown"
    recording: Literal["confirmed", "unconfirmed"] = "confirmed"
    terminal_at: str | None = None
    result: CoreReference | None = None
    details: CoreReference | None = None
    submission: CoreReference | None = None
    backend_observation: CoreReference | None = None
    deduplication_key: str | None = None
    attention_episode: str | None = None
    resource_id: str | None = None
    #: The handle this task was forked from: whoever waits on that handle waits on this too.
    fork_of: str | None = None

    @classmethod
    def read_history(cls, record: dict[str, Any]) -> TaskSnapshot:
        """Decode older vocabularies as unavailable data; never infer an outcome or rewrite Core."""
        values = dict(record)
        for field, vocabulary, fallback in (
            (
                "status",
                get_args(TaskStatus),
                "unknown" if values.get("terminal_at") else "task_state_uncertain",
            ),
            ("activity", get_args(TaskActivity), "unavailable"),
            ("settlement", get_args(TaskSettlement), "historical_unknown"),
            ("connection_freshness", get_args(TaskFreshness), "unknown"),
        ):
            value = values.get(field)
            if isinstance(value, str) and value not in vocabulary:
                values[field] = fallback
        return cls.model_validate(values)

    def validate_identity(self, previous: TaskSnapshot | None) -> None:
        """Reject changes to a handle's immutable identity before resolving its content."""
        if previous is not None and any(
            getattr(previous, name) != getattr(self, name)
            for name in (
                "owner",
                "kind",
                "invocation_id",
                "tool",
                "created_at",
                "description",
                "description_missing",
                "fork_of",
            )
        ):
            raise ValueError("Task identity cannot change under an existing handle")

    def validate_custody(
        self, previous: TaskSnapshot | None, replacement: SubmissionContext | None
    ) -> None:
        """Validate retained identities and outcomes without deciding execution transitions."""
        if previous is None:
            return
        continuation = replacement.provider_continuation if replacement is not None else None
        continuation_reserved = (
            previous.kind == "Download"
            and previous.terminal_at is None
            and previous.submission is not None
            and self.submission is not None
            and previous.submission != self.submission
            and self.backend is None
            and replacement is not None
            and replacement.tool == "connected_data_continue"
            and replacement.handle == previous.handle
            and replacement.owner == previous.owner
            and replacement.invocation_id == previous.invocation_id
            and continuation is not None
            and continuation.previous_submission == previous.submission
            and continuation.previous_backend == previous.backend
            and continuation.operation_id == previous.handle
            and continuation.source_id == previous.resource_id
        )
        if (
            previous.backend is not None
            and previous.backend != self.backend
            and not continuation_reserved
        ):
            raise ValueError("An accepted backend identity cannot be rebound")
        if (
            previous.submission is not None
            and previous.submission != self.submission
            and not continuation_reserved
        ):
            raise ValueError("Confirmed submission context cannot be replaced")
        if previous.resource_id is not None and previous.resource_id != self.resource_id:
            raise ValueError("A task's accepted resource identity cannot be rebound")
        if (
            previous.cancellation_requested_at is not None
            and previous.cancellation_requested_at != self.cancellation_requested_at
        ):
            raise ValueError("A cancellation intent cannot be silently cleared")
        if previous.terminal_at is not None and (
            self.terminal_at != previous.terminal_at
            or self.status != previous.status
            or self.result != previous.result
        ):
            raise ValueError("A historical terminal outcome cannot be reopened or replaced")

    @model_validator(mode="after")
    def validate_description_presence(self) -> TaskSnapshot:
        """Missing historical descriptions stay explicit rather than invented."""
        if self.description_missing != (
            self.description is None and self.description_reference is None
        ):
            raise ValueError("Description presence and missing-description marker disagree")
        return self


class TaskAlias(TaskRecord):
    """Historical identifier mapping; collisions deliberately remain ambiguous."""

    alias: str
    handle: str
    source_identity: str


class LocalRuntimeIdentity(TaskRecord):
    """Process birth identity for an in-process native runtime, never a PID alone."""

    host: str
    pid: int = Field(gt=0)
    created_at: float = Field(gt=0)
    boot_id: str | None = None
    started_since_boot: float | None = None
    start_ticks: int | None = None


class ProviderContinuation(TaskRecord):
    """Custody of a new local execution leg for an already verified provider job."""

    original_submission: CoreReference
    previous_submission: CoreReference
    previous_backend: BackendReference
    operation_id: str
    source_id: str
    provider_job_id: str = Field(min_length=1)
    previous_incarnations: tuple[str, ...] = ()
    cancellation_only: bool = False


class SubmissionContext(TaskRecord):
    """Trusted pre-submission context referenced explicitly by an owned worker."""

    reference: str
    handle: str
    owner: TaskOwner
    invocation_id: str
    tool: str
    argument_digest: str
    tool_version: str | None = None
    output_schema: dict[str, JsonValue] | None = None
    arguments: dict[str, JsonValue]
    workspace: str
    permissions: dict[str, JsonValue]
    confinement: dict[str, JsonValue]
    attribution: dict[str, JsonValue]
    execution_limits: dict[str, JsonValue]
    backend: BackendReference
    local_runtime: LocalRuntimeIdentity | None = None
    provider_continuation: ProviderContinuation | None = None


class NativeAttempt(TaskRecord):
    """An observed execution incarnation, distinct from Docket's retry counter."""

    handle: str
    incarnation: str
    backend: BackendReference
    leg: int = Field(ge=1)
    started_at: str
    execution_key: str
    cleanup_receipt: CoreReference | None = None
    finished_at: str | None = None
    cleanup_state: Literal["pending", "never_started", "confirmed", "unknown"] = "pending"


class MailAdmission(TaskRecord):
    """Exact uncommitted input selected for one assignment segment."""

    assignment_id: str
    turn_id: str
    input_revision: int = Field(ge=1)
    content: CoreReference


class MailItem(TaskRecord):
    """An attributed session input; acceptance does not acknowledge delivery."""

    id: str
    recipient_session: str
    sender_session: str | None = None
    sender_human: str | None = None
    kind: MailKind
    content: CoreReference
    created_at: str
    handle: str | None = None
    episode: str | None = None
    revision: int = Field(default=1, ge=1)
    delivery: Literal["eligible", "queued", "held"] = "eligible"
    held_reason: str = ""
    approval_question: str | None = None
    queued_order: int | None = None
    automatic_promotion: bool = False
    admission: MailAdmission | None = None


class ContextBoundary(TaskRecord):
    """Fixed existing Core context checkpoint used to reconstruct a model input."""

    session_id: str
    scope: str
    logical_time: int = Field(ge=0)


class RecipientInput(TaskRecord):
    """The sole notification acknowledgement, confirmed before model execution."""

    id: str
    session_id: str
    assignment_id: str
    iteration_id: str
    boundary: ContextBoundary
    notification_ids: tuple[str, ...]
    content: tuple[CoreReference, ...]
    committed_at: str
    tool_observation: CoreReference | None = None
    rendered_content: tuple[CoreReference, ...] = ()
    turn_id: str = ""
    human_input: bool = False
    legacy_proof: CoreReference | None = None
    legacy_notification_ids: tuple[str, ...] = ()


class CoordinationGrant(TaskRecord):
    """Human/configuration-created relationship; model intent conveys no rights."""

    id: str
    granting_human: str
    recipient_session: str
    target_session: str
    rights: tuple[CoordinationRight, ...]
    created_at: str
    target_handle: str | None = None
    target_scope: Literal["session", "tree"] = "session"
    revoked_at: str | None = None


class TaskSubscription(TaskRecord):
    """Recipient-specific observation of an exact historical execution handle."""

    handle: str
    recipient_session: str
    recipient_human: str | None = None
    enabled: bool
    updated_at: str
    generation: int = Field(default=1, ge=1)


class AssignmentEvidence(TaskRecord):
    """An explicitly shared immutable result snapshot, without rights over its source task."""

    handle: str
    owner: TaskOwner
    status: str
    terminal_at: str
    result: CoreReference
    description: str | None = None
    description_missing: bool = True
    partial: bool = False


class DeferredCompletion(TaskRecord):
    """The exact model outcome awaiting a trusted completion decision."""

    result: CoreReference
    context: ContextBoundary | None = None
    failed: bool = False


class AssignmentDetails(TaskRecord):
    """Conversation-owned finite assignment and fixed context, without copied history."""

    predecessor: str | None = None
    parent_turn_id: str | None = None
    context: ContextBoundary | None = None
    context_epoch: str = ""
    context_epoch_after: str | None = None
    last_output: CoreReference | None = None
    disposition: Literal["yield", "complete"] = "yield"
    #: Declared at spawn: a standing assignment (SPOTTER's watcher) keeps its handle
    #: across check segments; only cancellation or a failed check ends it.
    standing: bool = False
    dependencies: tuple[str, ...] = ()
    return_when: Literal["any", "all"] = "all"
    workflow_id: str | None = None
    workflow_step: str | None = None
    workflow: CoreReference | None = None
    execution_deadline: str | None = None
    inference_concurrency: int | None = Field(default=None, ge=1)
    cancellation_members: tuple[str, ...] | None = None
    input_evidence: tuple[AssignmentEvidence, ...] = ()
    evidence_assignment: str | None = None

    @model_validator(mode="after")
    def validate_execution_deadline(self) -> AssignmentDetails:
        """Require a finite, timezone-qualified absolute deadline when explicitly set."""
        for stamp in (self.execution_deadline,):
            if stamp is not None:
                value = datetime.fromisoformat(stamp)
                if value.tzinfo is None or value.astimezone(timezone.utc).timestamp() <= 0:
                    raise ValueError(
                        "An execution deadline requires a positive timezone-qualified timestamp"
                    )
        return self


class WorkflowCheckpoint(TaskRecord):
    """Declared-run provenance; execution remains owned by the existing workflow runner."""

    root_handle: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    invocation_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    declaration: str = Field(min_length=1)
    request_digest: str = Field(min_length=1)
    content: CoreReference
    source: Literal["checkpoint", "legacy_snapshot"] = "checkpoint"


class QuestionOption(TaskRecord):
    """One displayed option and its exact answer value."""

    label: str
    value: str
    description: str = ""


class QuestionDefinition(TaskRecord):
    """Question content; lifecycle exists only in its common TaskSnapshot."""

    prompt: str = Field(min_length=1)
    kind: Literal["freeform", "choice", "confirmation", "multi_choice"] = "freeform"
    options: tuple[QuestionOption, ...] = ()
    allow_freeform: bool = True
    requested_schema: dict[str, JsonValue] | None = None
    expires_at: str | None = None
    source: str = "ask_user"
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    resolution: CoreReference | None = None


class QuestionAnswerAction(TaskRecord):
    """Stable reference to the human action whose answer entered model context."""

    session_id: str
    surface_id: str
    action_id: str


class QuestionReply(TaskRecord):
    """Stored answer, with the existing composer attachment/reference payload."""

    answer: str = ""
    selected_options: tuple[str, ...] = ()
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    parts: tuple[dict[str, JsonValue], ...] = ()
    responder_human: str | None = None
    responder_session: str | None = None
    source_action: QuestionAnswerAction | None = None
    action: Literal["accept", "decline", "cancel"] = "accept"


def question_surface_resource(session_id: str, surface_id: str) -> str:
    """Key a visual Question's existing Core resource index without collisions."""
    return "question-surface:" + json.dumps([session_id, surface_id], separators=(",", ":"))
