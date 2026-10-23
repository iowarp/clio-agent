"""Recorded context assembly frames shared by the session API and trace reader."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ContextFrameItem(BaseModel):
    """One included or excluded input in a recorded context assembly."""

    kind: str
    source_id: str = ""
    role: str = ""
    path: str = ""
    display_path: str = ""
    included: bool = True
    reason: str = ""
    tokens_estimated: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextFrame(BaseModel):
    """The input composition and estimated size recorded for one agent turn."""

    id: str
    session_id: str
    turn_id: str = ""
    user_message_id: str = ""
    assistant_message_id: str = ""
    created_at: str
    updated_at: str
    status: Literal["assembled", "context_error", "completed", "error", "cancelled"] = "assembled"
    model: dict[str, str] = Field(default_factory=dict)
    agent: dict[str, Any] = Field(default_factory=dict)
    prompt: dict[str, Any] = Field(default_factory=dict)
    items: list[ContextFrameItem] = Field(default_factory=list)
    tokens_estimated: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
