"""What a session may do with long-term memory.

* **Use memory** (``memory.enabled``) off, or a session started with **Memory off**,
  removes the memory tools, the snapshot and the background pass for it; documents
  stay.
* Only a root conversation and a fork of one may write (``create_memory``,
  ``update_memory``, ``forget_memory``). Delegated children (``agent`` sessions) and
  side sessions (``lookup``) read only (memory ledger MD5). Remote agents do not
  exist on this base (MD4).
* ``propose_memory_change`` exists only in the background pass's ``curator`` run.
* A workspace session reads user scope plus its project scope, or only its project
  scope when the workspace turned **Use account memory** off.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from clio_agent import conf
from clio_agent.gact.memory.scope import session_scopes
from clio_agent.gact.session_kind import session_kind

#: Session metadata: the session was started with **Memory off** (fixed for its life).
SESSION_MEMORY_OFF = "memory_off"
#: Session metadata: the session is the background pass's curator run.
SESSION_CURATOR = "memory_curator"
#: Workspace config: the workspace reads account (user) memory (default on).
WORKSPACE_USE_ACCOUNT = "memory_use_account"

READ_TOOLS = ("memory_search", "memory_read")
WRITE_TOOLS = ("create_memory", "update_memory", "forget_memory")
CURATOR_TOOLS = ("propose_memory_change",)
RECALL_TOOLS = ("session_search", "session_read_summary", "session_read_context_frame")
MEMORY_TOOLS = (*READ_TOOLS, *WRITE_TOOLS, *CURATOR_TOOLS)


def setting(key: str, default: Any) -> Any:
    """A ``memory.*`` runtime setting (``/v1/settings/runtime``; env ``CLIO_MEMORY_*``)."""
    cast: Callable[[Any], Any] = conf.as_bool if isinstance(default, bool) else conf.as_str
    if isinstance(default, int) and not isinstance(default, bool):
        cast = conf.as_int
    env = "CLIO_" + key.upper().replace(".", "_")
    return conf.resolve(key, env=env, default=default, cast=cast)


def memory_enabled() -> bool:
    """**Use memory** (``memory.enabled``, default on)."""
    return bool(setting("memory.enabled", True))


def _root_conversation(app: Any, session: Any) -> bool:
    """Whether ``session`` is a conversation, or a fork whose root is one."""
    seen: set[str] = set()
    current = session
    while current is not None and current.id not in seen:
        seen.add(current.id)
        kind = session_kind(current)
        if kind == "conversation":
            return True
        if kind != "branch" or not current.parent_session_id:
            return False
        current = app.state.sessions.get(current.parent_session_id)
    return False


def use_account_memory(app: Any, workspace_id: str) -> bool:
    """The workspace's **Use account memory** switch (default on)."""
    workspaces = getattr(app.state, "workspaces", None)
    workspace = workspaces.get(workspace_id) if workspaces is not None else None
    config = getattr(workspace, "config", {}) or {}
    return bool(config.get(WORKSPACE_USE_ACCOUNT, True))


@dataclass(frozen=True)
class MemoryPolicy:
    """One session's memory rights."""

    enabled: bool
    reason: str  # why memory is off for the session ("" when on)
    scopes: tuple[str, ...]
    may_write: bool
    curator: bool
    session_search: bool

    def withheld(self, tool: str) -> str:
        """Why ``tool`` is withheld from this session, or ``""``."""
        if tool in RECALL_TOOLS:
            # A conversation started with memory off reads nothing from before it,
            # past sessions included; otherwise their own switch decides.
            if self.reason == "session_memory_off":
                return self.reason
            return "" if self.session_search else "session_search_off"
        if tool not in MEMORY_TOOLS:
            return ""
        if not self.enabled:
            return self.reason
        if tool in WRITE_TOOLS and not self.may_write:
            return "memory_write_root_only"
        if tool in CURATOR_TOOLS and not self.curator:
            return "memory_proposals_curator_only"
        return ""


def session_policy(app: Any, session: Any) -> MemoryPolicy:
    """The memory rights of ``session``."""
    metadata = getattr(session, "metadata", {}) or {}
    workspace_id = str(getattr(session, "workspace_id", "") or "")
    scopes = session_scopes(workspace_id, use_account_memory=use_account_memory(app, workspace_id))
    reason = ""
    if not memory_enabled():
        reason = "memory_disabled"
    elif metadata.get(SESSION_MEMORY_OFF):
        reason = "session_memory_off"
    return MemoryPolicy(
        enabled=not reason,
        reason=reason,
        scopes=scopes,
        may_write=_root_conversation(app, session),
        curator=bool(metadata.get(SESSION_CURATOR)),
        session_search=bool(setting("memory.session_search", True)),
    )


def active_policy() -> tuple[Any, Any, MemoryPolicy] | None:
    """``(app, session, policy)`` for the session a tool runs in, or ``None`` outside one."""
    from clio_agent.gact import context  # noqa: PLC0415

    app = context.active_app()
    session_id = context.active_session_id()
    if app is None or not session_id:
        return None
    session = app.state.sessions.get(session_id)
    if session is None:
        return None
    return app, session, session_policy(app, session)


def withheld_memory_tools(wanted: set[str]) -> dict[str, str]:
    """Typed reasons the active session may not have each of ``wanted``."""
    claimed = wanted & {*MEMORY_TOOLS, *RECALL_TOOLS}
    if not claimed:
        return {}
    active = active_policy()
    if active is None:
        return {}  # the product catalog describes every tool; each call checks again
    policy = active[2]
    out = {name: policy.withheld(name) for name in claimed}
    return {name: reason for name, reason in out.items() if reason}


__all__ = [
    "CURATOR_TOOLS",
    "MEMORY_TOOLS",
    "READ_TOOLS",
    "RECALL_TOOLS",
    "SESSION_CURATOR",
    "SESSION_MEMORY_OFF",
    "WORKSPACE_USE_ACCOUNT",
    "WRITE_TOOLS",
    "MemoryPolicy",
    "active_policy",
    "memory_enabled",
    "session_policy",
    "setting",
    "use_account_memory",
    "withheld_memory_tools",
]
