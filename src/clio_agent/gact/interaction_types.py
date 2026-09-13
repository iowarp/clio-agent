"""Wire models for native, MCP, permission, and A2UI interactions."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class UserQuestionOption(BaseModel):
    """One selectable answer to a user question."""

    label: str
    value: str = ""
    description: str = ""


class UserQuestion(BaseModel):
    """Authoritative question row with separate owner and attended sessions."""

    id: str
    session_id: str
    owner_session_id: str = ""
    attended_session_id: str = ""
    prompt: str
    status: Literal["pending", "answered", "cancelled", "expired"] = "pending"
    # "multi_choice" (SEP-1330, C1-S4/#1284): a multi-select elicitation field
    # (a flat array-of-enum) -- distinct from "choice" (a single scalar enum).
    kind: Literal["freeform", "choice", "confirmation", "multi_choice"] = "freeform"
    options: list[UserQuestionOption] = Field(default_factory=list)
    allow_freeform: bool = False
    created_at: str
    updated_at: str
    expires_at: str = ""
    source: str = "orchestrator"
    turn_id: str = ""
    attempt_id: str = ""
    answer: str = ""
    selected_options: list[str] = Field(default_factory=list)
    answer_metadata: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Agent-driven elicitation (#1309, C1-S7): additive wire fields, all absent
    # (excluded by ``exclude_none``) unless the server marked this question
    # ``x-clio-agent/audience: "agent"`` -- a question with no audience hint
    # dumps byte-identical to the pre-#1309 shape.
    #
    # ``audience`` -- the SERVER's own declared hint, stamped at mint time
    # regardless of the routing outcome (observability: "this WAS marked for
    # the agent" even when it fell back to the human).
    audience: Optional[Literal["human", "agent"]] = None
    # ``agent_elicitation_routing`` / ``agent_elicitation_fallback_detail`` --
    # the ROUTING decision for an audience="agent" question: either routed
    # ("elicitation_routed_to_agent") or fell back to the human path
    # ("agent_elicitation_fallback_to_human", with a typed ``..._detail`` key
    # from ``clio_agent.gact.agent_elicitation.AGENT_ELICITATION_FALLBACK_DETAILS``
    # naming WHY -- policy/url-mode/recursion/schema/timeout/etc; never silent).
    agent_elicitation_routing: Optional[
        Literal["elicitation_routed_to_agent", "agent_elicitation_fallback_to_human"]
    ] = None
    agent_elicitation_fallback_detail: Optional[str] = None
    # ``answered_by`` -- attribution stamped on the ONE atomic "answered"
    # transition (:func:`clio_agent.gact.elicitation_bridge.claim_question_transition`):
    # who actually produced the accepted answer. Absent means "human" (today's
    # implicit default, preserved byte-for-byte); "agent" only when the
    # session's agent's answer won the atomic transition and passed the
    # server's own ``requestedSchema`` validation (the semantic firewall).
    answered_by: Optional[Literal["human", "agent"]] = None

    @model_validator(mode="after")
    def fill_interaction_session_ids(self) -> "UserQuestion":
        """Populate additive ownership fields when reading legacy question rows."""

        elicitation = self.metadata.get("elicitation")
        elicitation = elicitation if isinstance(elicitation, dict) else {}
        if not self.owner_session_id:
            self.owner_session_id = str(
                self.metadata.get("forwarded_from_session")
                or elicitation.get("forwarded_from_session")
                or self.session_id
            )
        if not self.attended_session_id:
            self.attended_session_id = self.session_id
        return self


class CreateUserQuestionRequest(BaseModel):
    """Request to create one native question."""

    prompt: str
    kind: Literal["freeform", "choice", "confirmation"] = "freeform"
    options: list[UserQuestionOption] = Field(default_factory=list)
    allow_freeform: bool = False
    source: str = "orchestrator"
    turn_id: str = ""
    attempt_id: str = ""
    expires_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class AnswerUserQuestionRequest(BaseModel):
    """Answer payload for a native question."""

    answer: str = ""
    selected_options: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PendingInteractionSource(BaseModel):
    """Protocol and producer correlation for one pending interaction."""

    protocol: Literal["native", "mcp"]
    tool_name: str = ""
    invocation_id: str = ""
    surface_id: str = ""


class PendingInteraction(BaseModel):
    """Normalized pending human interaction projected from authoritative stores."""

    id: str
    kind: Literal["question", "permission", "a2ui", "mcp_task_input"]
    owner_session_id: str
    attended_session_id: str
    task_id: str = ""
    status: Literal["pending", "answered", "cancelled", "expired"] = "pending"
    title: str
    prompt: str = ""
    requires_human_response: bool = False
    audience: Optional[Literal["human", "agent"]] = None
    routing_state: Optional[
        Literal["elicitation_routed_to_agent", "agent_elicitation_fallback_to_human"]
    ] = None
    fallback_detail: str = ""
    answered_by: Optional[Literal["human", "agent"]] = None
    source: PendingInteractionSource
    created_at: str
    #: Monotonic-per-row marker of the version this projection saw. A poll is a
    #: full re-derivation from four ledgers, so two responses can arrive out of
    #: order; without a revision a client cannot tell a stale one from an update
    #: and re-renders a settled interaction as pending. Empty when the underlying
    #: row carries no version of its own.
    revision: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    actions: list[str] = Field(default_factory=list)


class RespondInteractionRequest(BaseModel):
    """Kind-neutral response routed by the server-side interaction identity."""

    action: str = ""
    answer: str = ""
    selected_options: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    message: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "AnswerUserQuestionRequest",
    "CreateUserQuestionRequest",
    "PendingInteraction",
    "PendingInteractionSource",
    "RespondInteractionRequest",
    "UserQuestion",
    "UserQuestionOption",
]
