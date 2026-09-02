"""Wire models for native, MCP, permission, and A2UI interactions."""

from __future__ import annotations

from typing import Any, Literal

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
    kind: Literal["freeform", "choice", "confirmation"] = "freeform"
    options: list[UserQuestionOption] = Field(default_factory=list)
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
    source: PendingInteractionSource
    created_at: str
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
