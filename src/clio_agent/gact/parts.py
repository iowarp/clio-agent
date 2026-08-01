"""GACT part grammar models.

This module owns the models that define GACT part payloads and their advertised
capabilities. ``clio_agent.gact.types`` re-exports these names for compatibility.

Attributes:
    CapabilityFlags: Flags describing the supported GACT part and API features.
    Part: The omnibus discriminated model for a GACT message part.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class CapabilityFlags(BaseModel):
    """SPEC §3.3 + v0.2 additions.

    We only claim capabilities we actually implement. Advertising a
    flag = ``True`` that isn't wired lies to the TUI; flip to
    ``True`` only once the underlying surface works.
    """

    # v0.1 baseline
    workspaces: bool = False
    sessions: bool = False
    subagents: bool = False
    mcp: bool = False
    lsp: bool = False
    files: bool = False
    diffs: bool = False
    permissions: bool = False
    providers: bool = False
    commands: bool = False
    voice: bool = False
    scheduled_sessions: bool = False
    hooks: bool = False
    session_tasks: bool = False
    metrics: bool = False
    session_branching: bool = False
    session_sharing: bool = False
    session_export: bool = False
    session_summary: bool = (
        False  # POST /sessions/{id}/summarize - user-facing TLDR (distinct from compact)
    )
    attachments_upload: bool = False  # POST /sessions/{id}/attachments - base64 byte upload
    multimodal_image_parts: bool = False  # POST /messages accepts/preserves image parts
    cost_tracking: bool = False
    thinking_blocks: bool = False
    edit_modes: bool = False
    plan_mode: bool = False
    search_messages: bool = False
    agent_write: bool = False
    skills_extraction: bool = False

    # v0.2 additions — SPEC §3.2.1
    agent_routing: bool = False
    memory: bool = False
    structured_errors: bool = False
    integration_health: bool = False
    tool_telemetry: bool = False

    # CLIO vendor truth flags. GACT conformance treats x_* keys as
    # vendor metadata, so these can carry richer values than booleans.
    x_clio_cancellation: Literal["none", "best_effort", "hard"] = "none"
    x_clio_executor_cancellation: bool = False
    x_clio_text_streaming: Literal["none", "batch", "best_effort_live"] = "none"
    x_clio_synthetic_posthoc_streaming: bool = False
    x_clio_stream_fallback_reasons: dict[str, dict[str, Any]] = Field(default_factory=dict)
    x_clio_direct_delete_permissions: bool = False
    x_clio_prompt_registry: bool = False
    x_clio_expert_packs: bool = False
    x_clio_agent_blueprints: bool = False
    x_clio_user_questions: bool = False
    x_clio_retry_attempts: bool = False
    x_clio_context_frames: bool = False
    x_clio_semantic_events: bool = False
    # #966 S2 / #968 — the /v1/artifacts read surface + user-pin channel, the
    # artifact.* SSE family, and resource_link parts carrying artifact:// wire ids.
    x_clio_artifacts: bool = False
    x_clio_semantic_trace_backend: str = ""
    x_clio_semantic_trace_detail: str = ""
    x_clio_hook_backend: str = ""
    x_clio_hook_events: dict[str, Any] = Field(default_factory=dict)
    x_clio_capability_gaps: dict[str, dict[str, Any]] = Field(default_factory=dict)


class Part(BaseModel):
    """SPEC §4.5. The discriminator is ``type``; fields are
    ``omitempty`` JSON-wise so unused ones don't serialise.

    ``id`` is optional on the wire because v0.1 clients may omit it
    when POSTing user parts (they let the server assign an id). The
    server always populates it before emitting on SSE, so readers
    still see a stable id.
    """

    id: str = ""
    type: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Per-part expert attribution (CLIO extension). Lets a client attribute every
    # turn/part to its source without parsing prose or inferring from metadata.
    agent_id: str = ""
    """The expert/agent that GENERATED this part (e.g. 'geospatial'), so a client
    can attribute every turn/part to its source without inference. Empty for
    user-authored parts."""

    # Monotonic arrival/wire order key within a turn (#731). Assigned when the
    # persisted assistant message is assembled so a reloaded conversation can be
    # restored to the exact order it streamed — even if a client re-sorts the
    # parts list. 1-based; ``0`` means "unsequenced" (a live part, ordered by the
    # SSE event id instead). Force-kept on the wire only when set (>0).
    sequence: int = 0

    # text / error (v0.1 error part shape)
    text: str = ""

    # image (CLIO extension for multimodal user content). A client may send
    # data or url; the server preserves the fields on the transcript and
    # validates provider support before scheduling the turn.
    data: Optional[str] = None
    url: Optional[str] = None
    media_type: Optional[str] = None

    # routing_decision (v0.2 §4.5)
    selected_agent: str = ""
    rationale: str = ""
    confidence: float = 0.0
    heuristic: bool = False
    # iowarp/clio-agent#25: which path the selected expert actually
    # ran on. "fast" = deterministic tool template (no LM); "expert_
    # loop" = full DSPy ReAct iteration with the expert's tool set.
    # Empty when not applicable (e.g. chat path, or branches that
    # haven't been migrated to the classifier yet — analysis /
    # visualization will follow in their own issues). Field values
    # are user-neutral per CLAUDE.md Rule 3 (no DSPy terms in user-
    # facing payload).
    execution_path: str = ""

    # expert_handoff (CLIO extension). Typed mirror of the delegation row; client reads fields,
    # not ``text``. ``parent_agent`` delegated to ``child_agent``; ``stage`` = lifecycle phase
    # (``parent.resumed`` = orchestrator return twin), outcome reuses ``status``. ONE
    # ``delegate.started`` + ONE ``delegate.completed`` per delegation; a FAILURE concludes there
    # with ``status="failed"`` (#882), no dedup. #888: ``delegate.started`` + ``metadata`` carry
    # the typed ``workflow_state`` the parent PASSED INTO the child (only when non-empty, #885).
    parent_agent: str = ""
    child_agent: str = ""
    stage: str = ""

    # P2.10 (#1127): additive task/tool run-handle render contract. The same
    # fields appear for local and relay placements; unused parts omit defaults.
    handle_id: str = ""
    run_label: str = ""
    live_state: str = ""
    host: str = ""
    placement: str = ""

    # P2.11 (#1128): one message-an-agent grammar for step-boundary queueing,
    # relay tasks/update injection, and finished-child wake supersession.
    message_action: str = ""
    supersedes_handle_id: str = ""
    superseded_by_handle_id: str = ""

    # tool_call / tool_result. CLIO emits these as live SSE parts when
    # MCP tools start/finish so clients can show progress before the
    # final assistant message metadata is attached.
    call_id: str = ""
    tool_name: str = ""
    # The model's reasoning for THIS turn, carried on the action part itself
    # (#732): one LLM turn = text (thought, maybe thinking) + the action it
    # chose, as a single ordered event. Populated on ``tool_call`` (the step
    # thought) and on ``expert_handoff`` (the orchestrator's delegation
    # reasoning) so a client renders ``text -> action`` straight from wire order
    # with no join against the telemetry channel. The tool RESPONSE is a separate
    # ``tool_result`` event (the call->response gap can be large).
    thought: str = ""
    input: dict[str, Any] = Field(default_factory=dict)
    content: list["Part"] = Field(default_factory=list)
    is_error: bool = False
    cached: bool = False
    duration_ms: float = 0.0

    # resource_link (SPEC §4.5 core type; #968). Reused to give a generated ARTIFACT
    # outbound wire identity (#966.9): ``uri`` is ``artifact://<ws>/<name>@vN`` (or
    # ``ui://…`` for a ``ui_payload``), ``server_id`` the ``clio-artifacts`` sentinel,
    # ``name`` the artifact name; the identity/provenance block rides ``metadata``.
    uri: str = ""
    name: str = ""
    server_id: str = ""

    # mcp_app (MCP Apps 2026-01-26). This is a public capability reference,
    # never the tool result's private ``_meta``. The host resolves ``data_ref``
    # from its session-local registry and reads ``resource_uri`` only from the
    # exact ``source_server`` that produced the originating tool result.
    app_instance_id: str = ""
    resource_uri: str = ""
    source_server: str = ""
    data_ref: str = ""
    mime_type: str = ""
    height: int = 0

    # file_diff (BBB21 + #4): a proposed edit awaiting apply/reject.
    # ``new_content`` (when present) is what the apply path writes
    # to disk — re-applying a unified diff is fragile so we ship
    # the whole-file replacement alongside the diff.
    path: str = ""
    unified_diff: str = ""
    new_content: str = ""
    status: str = ""  # "pending" | "applied" | "rejected" | "apply_failed"
    # iowarp/clio-agent — capabilities.edit_modes: which mode the
    # session was in when this diff was produced. "diff" = unified_diff
    # is the canonical view; "whole" = render new_content full;
    # "patch" = both fields meaningful. Read by the TUI for rendering.
    edit_mode: str = ""
    lines_added: int = 0
    lines_removed: int = 0

    # compaction part (SPEC §4.5, #832): structured summary replacing archived
    # history, rendered from typed fields (not a ``[compact summary]`` prefix).
    # ``auto`` flags a policy- (vs user-) triggered /compact; ``compacted_message_ids``
    # lists the archived messages it stands in for.
    summary: str = ""
    auto: bool = False
    compacted_message_ids: list[str] = Field(default_factory=list)

    def to_wire(self) -> dict[str, Any]:
        """Project this part to its wire dict via ``exclude_defaults`` (omitempty:
        a part populates only its own ``type``'s fields; the rest sit at
        ``""``/``0``/``[]``/``False`` and are dropped). The ``id``/``type``/``agent_id``
        triple is force-kept so a client can always attribute a part; ``content`` recurses.
        """

        wire = self.model_dump(exclude_defaults=True)
        wire["id"] = self.id
        wire["type"] = self.type
        wire["agent_id"] = self.agent_id
        if self.content:
            wire["content"] = [child.to_wire() for child in self.content]
        return wire
