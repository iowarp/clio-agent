"""A person's approval of an MCP call answers that call's own plain confirm.

A server may confirm a call it was sent ("Submit job 42 to the scheduler?") with a
form that offers nothing to choose: no fields, or one yes/no boolean. When a person
already allowed that exact call at CLIO's permission prompt, asking again is the same
question twice. Such a confirm is accepted for them, and the Question stays in the
transcript under the call, answered, with the server's message and the typed reason
:data:`REASON_COVERED`.

Anything else still asks: a form with fields, choices or numbers, a URL request, or a
call that only a rule, mode, reviewer or script grant allowed (no person answered it).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Typed reason recorded on a confirm answered by the person's approval of its call.
REASON_COVERED = "mcp_confirm_covered_by_host_permission"
#: The answer shown under the call, next to "Yes".
COVERED_ANSWER = "Answered by your approval of this call"

_BOOLEAN_KEYS = frozenset({"type", "title", "description", "default"})
#: ``_meta`` keys that carry no data for the person: MCP's own (related-task
#: correlation and the like) and CLIO's routing hints.
_UNTYPED_META_PREFIXES = ("io.modelcontextprotocol/", "x-clio-agent/")


def plain_confirm(params: Any, meta: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """The content that accepts a plain-confirm form, else ``None``.

    Plain means the form adds no choice beyond the call: no fields (``{}``), or one
    boolean field and nothing else (``{field: True}``). A request that carries a
    server's own typed data in ``meta`` (any namespaced key beyond MCP's own, such as a
    job plan to render) is a card for the person to read, so it is never plain.
    """

    if getattr(params, "mode", "form") != "form":
        return None
    if any(not str(k).startswith(_UNTYPED_META_PREFIXES) for k in (meta or {})):
        return None
    schema = getattr(params, "requested_schema", None)
    if not isinstance(schema, Mapping) or schema.get("type") != "object":
        return None
    properties = schema.get("properties")
    if properties is None:
        properties = {}
    if not isinstance(properties, Mapping) or len(properties) > 1:
        return None
    if not properties:
        return {}
    ((name, spec),) = properties.items()
    if not isinstance(spec, Mapping) or spec.get("type") != "boolean" or set(spec) - _BOOLEAN_KEYS:
        return None
    return {str(name): True}


def person_allowed(app: Any, session_id: str, invocation_id: str) -> str:
    """The id of the permission a person granted for this very call, else ``""``.

    The call's invocation is its observed tool row (the id the permission request
    named), optionally followed by the model's call id. Rows a rule, mode, reviewer or
    script grant resolved carry no person's grant and never match.
    """

    from clio_agent.gact.runtime.grants import GRANTOR_USER  # noqa: PLC0415
    from clio_agent.gact.runtime.permission_actions import allows  # noqa: PLC0415
    from clio_agent.gact.spawn_invocation import MODEL_CALL_SEPARATOR  # noqa: PLC0415

    if not session_id or not invocation_id:
        return ""
    for row in list((getattr(app.state, "permissions", None) or {}).values()):
        call = row.get("tool_call")
        call_id = str(call.get("call_id") or "") if isinstance(call, Mapping) else ""
        same_call = invocation_id == call_id or invocation_id.startswith(
            call_id + MODEL_CALL_SEPARATOR
        )
        if (
            call_id
            and same_call
            and row.get("session_id") == session_id
            and row.get("status") == "resolved"
            and row.get("grantor") == GRANTOR_USER
            and allows(row.get("action", ""))
        ):
            return str(row.get("id") or "")
    return ""


def approval_comment(app: Any, permission_id: str) -> str:
    """What the person wrote when granting ``permission_id``; ``""`` when nothing.

    A covered confirm is answered by that grant, so the grant's comment is the
    person's reply to the server too.
    """

    rows = getattr(app.state, "permissions", None) or {}
    row = rows.get(permission_id)
    return str((row or {}).get("comment") or "").strip()


__all__ = [
    "COVERED_ANSWER",
    "REASON_COVERED",
    "approval_comment",
    "person_allowed",
    "plain_confirm",
]
