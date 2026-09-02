"""GACT 0.3 message submission and acceptance models."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from clio_agent.gact.types import AgentRef, ModelRef, Part


class MessageBehavior(BaseModel):
    """User-selected behavior captured with a message or queued item."""

    reasoning_effort: Literal["off", "low", "medium", "high", "xhigh"] = "medium"
    execution_mode: Literal["execute", "plan", "deep_research"] = "execute"
    confirmation_policy: Literal["ask", "auto-edits", "bypass", "ai-review", "spotter-ai"] = "ask"


class PostMessageRequest(BaseModel):
    """Typed user message submission with explicit delivery intent."""

    parts: list[Part] = Field(default_factory=list)
    text: Optional[str] = None
    model: Optional[ModelRef] = None
    agent: Optional[AgentRef] = None
    agent_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    client_message_id: str = ""
    idempotency_key: str = ""
    delivery: Literal["start", "steer", "auto"] = "auto"
    behavior: MessageBehavior = Field(default_factory=MessageBehavior)

    def extract_text(self) -> str:
        """Return ordered text parts, falling back to the legacy text field."""

        text_parts = [part.text for part in self.parts if part.type == "text" and part.text]
        return "\n".join(text_parts).strip() if text_parts else self.text or ""

    def image_parts(self) -> list[Part]:
        """Return image parts supplied in this message."""

        return [part for part in self.parts if part.type == "image"]

    def resource_parts(self) -> list[Part]:
        """Return immutable workspace resource references supplied in this message."""

        return [part for part in self.parts if part.type == "resource_ref"]

    def extract_agent_id(self) -> str:
        """Return a per-turn agent override, if supplied."""

        if self.agent is not None and self.agent.id:
            return self.agent.id
        return self.agent_id or ""


class PostMessageResponse(BaseModel):
    """Immediate durable acceptance for a submitted user message.

    ``state`` has exactly two reachable values. ``queued`` was never produced:
    POST /messages either starts a turn or accepts a pending steer, and it must
    NOT quietly enqueue a future message behind the user's back — the durable
    queue is its own explicit surface (``POST .../queued-messages``). An
    unreachable wire value is a promise a client can wait forever for, so it is
    gone rather than left as decoration.
    """

    message_id: str
    accepted_at: str
    delivery: Literal["start", "steer", "auto"] = "auto"
    state: Literal["started", "pending_steer"] = "started"
    effective_model: ModelRef = Field(default_factory=ModelRef)
    behavior: MessageBehavior = Field(default_factory=MessageBehavior)
    idempotent_replay: bool = False
