"""Distinguish conversation branches from sessions owned by an agent or lookup."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

SessionKind = Literal["conversation", "branch", "agent", "lookup"]


def session_kind(session: Any) -> SessionKind:
    """Classify persisted records, including forks created before kind was recorded."""
    metadata = getattr(session, "metadata", {})
    metadata = metadata if isinstance(metadata, Mapping) else {}
    agent = getattr(session, "agent", {})
    agent = agent if isinstance(agent, Mapping) else {}
    if metadata.get("side_session"):
        return "lookup"
    if metadata.get("session_type") == "agent_task" or agent.get("mode") == "subagent":
        return "agent"
    if metadata.get("session_kind") == "branch":
        return "branch"
    if getattr(session, "parent_session_id", ""):
        # Historical forks used the default main agent; other child records
        # remain managed sessions until their ownership is known.
        return "branch" if agent.get("id") == "main" else "agent"
    return "conversation"
