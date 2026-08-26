"""Wire models for persisted session context preferences."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ContextPreferences(BaseModel):
    """Durable automatic-compaction controls for one session."""

    session_id: str
    automatic_compaction: bool = True
    autocompact_pct: float = Field(default=0.85, gt=0.0, le=1.0)


class UpdateContextPreferencesRequest(BaseModel):
    """Partial update for a session's context controls."""

    automatic_compaction: Optional[bool] = None
    autocompact_pct: Optional[float] = Field(default=None, gt=0.0, le=1.0)
