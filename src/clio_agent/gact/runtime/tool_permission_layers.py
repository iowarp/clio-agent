"""The permission gate's layers for one tool call, in one place (owner module, TD53).

Order, highest first:

1. **deny from any layer**: a person's rule or the tool's default permission;
2. **the person's rules** (``/v1/policies``: the tools page, "allow for this
   session/workspace" answers, rules saved by hand);
3. **the tool's default permission**: the deployment's ``tools.permissions``, else the
   plugin's declaration (:mod:`clio_agent.gact.agents.tool_settings`);
4. **the approval mode** (``default_decision`` in :mod:`clio_agent.gact.permission_gate`)
   and the script grant of a ``run_code`` call.

So a plugin can keep ``send_email`` on ``ask`` in ``auto-edits``, and ``bypass`` still
skips a default's question but never a deny. A person's own ``ask`` rule asks even in
``bypass``, as before. A tool with no default and no rule keeps what its annotations
imply: read-only calls run, others go to the approval mode.

The gate (:func:`clio_agent.gact.permission_gate._make_permission_gate`) asks
:func:`decide` for every call, nested ``run_code`` calls included, and records the
returned typed reason.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from clio_agent.gact.runtime.permission_actions import POLICY_ALLOWING

__all__ = [
    "REASON_DEFAULT_ALLOW",
    "REASON_DEFAULT_DENY",
    "TOOL_DEFAULT_UNRESOLVED",
    "Layered",
    "answered",
    "decide",
    "native_gate",
    "person_rule",
    "tool_default",
]

logger = logging.getLogger(__name__)

#: Typed audit reasons for a call the tool's default permission decided.
REASON_DEFAULT_DENY = "tool_default_deny"
REASON_DEFAULT_ALLOW = "tool_default_allow"
#: Typed reason: the session's resolution could not be read, so only the deployment's
#: ``tools.permissions`` supplied a default for this call.
TOOL_DEFAULT_UNRESOLVED = "tool_default_unresolved"


@dataclass(frozen=True)
class Layered:
    """The layers' verdict: ``deny``, ``allow``, ``ask`` or ``""`` (the approval mode
    decides), the layer that decided (``rule``, ``default`` or ``mode``) and the raw
    rule action (``allow_session`` and so on) for the audit reason."""

    action: str
    source: str
    rule: str = ""


def decide(rule: str, default: str, *, approval_mode: str) -> Layered:
    """Combine a person's rule action and a tool's default permission."""
    if rule == "deny":
        return Layered("deny", "rule", rule)
    if default == "deny":
        return Layered("deny", "default", rule)
    if rule in POLICY_ALLOWING:
        return Layered("allow", "rule", rule)
    if rule == "ask":
        return Layered("ask", "rule", rule)
    if default == "allow":
        return Layered("allow", "default", rule)
    if default == "ask":
        # The default sits above the approval mode, but bypass skips every question.
        if approval_mode == "bypass":
            return Layered("", "mode", rule)
        return Layered("ask", "default", rule)
    return Layered("", "mode", rule)


def person_rule(app: Any, session: Any | None, session_id: str, name: str, path: str) -> str:
    """The person's rules for ``name`` alone (no mode lock): the raw winning action."""
    from clio_agent.gact.runtime.grant_resolver import resolve  # noqa: PLC0415

    return resolve(
        "tool",
        name,
        policies=getattr(app.state, "permission_policies", []),
        session_id=session_id,
        workspace_id=str(getattr(session, "workspace_id", "") or "") if session else "",
        path=path,
    )


def tool_default(app: Any, session_id: str, name: str) -> str:
    """``name``'s default permission for this session (``""`` when none)."""
    from clio_agent.gact.agents.tool_settings import declared_permission  # noqa: PLC0415
    from clio_agent.tools.tool_settings import configured_permissions  # noqa: PLC0415

    configured = configured_permissions().get(name)
    if configured:
        return configured
    resolution = _pinned(app, session_id)
    return declared_permission(name, resolution).value if resolution is not None else ""


def _pinned(app: Any, session_id: str) -> Any | None:
    """The session's pinned resolution, without pinning one as a side effect."""
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        AGENT_REF_KEY,
        AGENT_RESOLUTION_KEY,
        SessionAgentUnavailable,
        pinned_resolution,
    )

    session = app.state.sessions.get(session_id) if session_id else None
    metadata = getattr(session, "metadata", None)
    digest = str(metadata.get(AGENT_RESOLUTION_KEY) or "") if isinstance(metadata, Mapping) else ""
    if not digest:
        return None
    try:
        return pinned_resolution(app, digest, str((metadata or {}).get(AGENT_REF_KEY) or ""))
    except SessionAgentUnavailable as exc:
        logger.warning("%s session=%s reason=%s", TOOL_DEFAULT_UNRESOLVED, session_id, exc)
        return None


#: The tool whose call a person already approved at :func:`native_gate`; the gate allows
#: that tool's own permission check in the same call instead of asking twice.
_ANSWERED: ContextVar[str] = ContextVar("clio_tool_answered", default="")


def answered(name: str) -> bool:
    """Whether a person approved this very call of ``name`` already."""
    return bool(name) and _ANSWERED.get() == name


def _native(tool: Any) -> bool:
    from clio_agent.gact.agents.dspy_native_tool import ClioNativeTool  # noqa: PLC0415

    return isinstance(tool, ClioNativeTool)


@contextlib.contextmanager
def native_gate(tool: Any, name: str, args: Mapping[str, Any]) -> Iterator[str]:
    """Apply the layers to a call of a CLIO built-in or a plugin's Python tool.

    Those tools do not pass the permission gate on their own (MCP and gateway tools do,
    in their call path), so without a rule or a default they run as before. A deny
    refuses the call (yields the refusal text); an ask asks the person, as the gate
    does, with a ``run_code`` script's grant shown. Yields ``""`` when the call may run.
    """
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    app = _ctx.active_app()
    sid = _ctx.active_session_id() or _ctx.active_tool_session_id() or ""
    if app is None or not sid or not _native(tool):
        yield ""
        return
    from clio_agent.gact import permission_gate as gate  # noqa: PLC0415
    from clio_agent.gact.runtime.permission_policies import (  # noqa: PLC0415
        _permission_path_from_args,
    )

    session = app.state.sessions.get(sid)
    mode = str(getattr(session, "approval_mode", "") or "ask")
    rule = person_rule(app, session, sid, name, _permission_path_from_args(args))
    layered = decide(rule, tool_default(app, sid, name), approval_mode=mode)
    if layered.action == "deny":
        gate._layered_deny(app, sid, name, args, "native tool", layered, "")
        yield f"{name} was not run: a permission setting blocks it."
        return
    if layered.action != "ask":
        yield ""
        return
    context = {"kind": "native", "summary": f"Use {name}"}
    with gate.script_grants.question():
        decision = gate._ask_person(
            app,
            sid,
            name,
            args,
            context,
            "native tool",
            mode,
            "ask",
            app.state.cancel_events.get(sid),
        )
    if decision != "allow":
        yield f"{name} was not run: the person did not approve it."
        return
    token = _ANSWERED.set(name)
    try:
        yield ""
    finally:
        _ANSWERED.reset(token)
