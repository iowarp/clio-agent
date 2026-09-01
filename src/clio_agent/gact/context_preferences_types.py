"""Wire models for persisted session context preferences."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

#: The ONE in-code default auto-compaction trigger fraction (0..1].
#:
#: The LIVE path is owned by the config key ``autocompact.pct``
#: (``CLIO_AUTOCOMPACT_PCT``), resolved in
#: :func:`clio_agent.gact.runtime.context_tokens._autocompact_threshold`; this
#: constant is only the fallback that resolver and the wire models below share,
#: so a per-session preference and the configured global cannot document
#: different numbers.
DEFAULT_AUTOCOMPACT_PCT = 0.85


class ContextPreferences(BaseModel):
    """Durable automatic-compaction controls for one session."""

    session_id: str
    automatic_compaction: bool = True
    autocompact_pct: float = Field(default=DEFAULT_AUTOCOMPACT_PCT, gt=0.0, le=1.0)


class UpdateContextPreferencesRequest(BaseModel):
    """Partial update for a session's context controls."""

    automatic_compaction: Optional[bool] = None
    autocompact_pct: Optional[float] = Field(default=None, gt=0.0, le=1.0)
