"""CLIO-owned task metadata, kept outside backend tool arguments."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TaskCall:
    """The original invocation identity and application-owned executor."""

    executor: Any = None
    description: str = ""
    invocation_id: str = ""
    owner_agent: str = ""
    session_id: str = ""


_EMPTY_CALL = TaskCall()
TASK_CALL: ContextVar[TaskCall] = ContextVar("clio_task_call", default=_EMPTY_CALL)


def require_admission(app: Any = None, sid: str | None = None) -> None:
    """Reject submissions once their conversation subtree or application starts cancelling."""
    from clio_agent.gact.context import active_app, active_session_id

    app = app if app is not None else active_app()
    sid = sid if sid is not None else active_session_id()
    if app is not None:
        session = app.state.sessions.get(sid) if sid else None
        if (
            sid in getattr(app.state, "task_admission_closed", set())
            or bool((getattr(session, "metadata", None) or {}).get("task_admission_closed"))
            or getattr(getattr(app.state, "task_supervisor", None), "closing", False)
        ):
            raise RuntimeError("Task admission is closed")


def begin_backend_call(
    executor: Any, name: str, args: dict[str, Any]
) -> tuple[dict[str, Any], Token[TaskCall]]:
    """Strip owned metadata and carry invocation ownership across the MCP boundary."""
    inherited = TASK_CALL.get()
    from clio_agent.gact.context import active_session_id

    forwarded = dict(args)
    description = forwarded.pop("_clio_task_description", None)
    if description is not None and (not isinstance(description, str) or len(description) > 2000):
        raise ValueError("Task description must be a string of at most 2000 characters")
    token = TASK_CALL.set(
        TaskCall(
            executor=executor,
            description=str(description or safe_description(name, forwarded)),
            invocation_id=inherited.invocation_id,
            owner_agent=inherited.owner_agent,
            session_id=inherited.session_id or active_session_id(),
        )
    )
    return forwarded, token


def begin_invocation() -> Token[TaskCall]:
    """Capture the real product tool observer's identity before dispatch."""
    from clio_agent.gact.artifacts.observer_bridge import observer_call_id
    from clio_agent.gact.context import active_react_scope

    return TASK_CALL.set(
        TaskCall(invocation_id=observer_call_id(), owner_agent=active_react_scope())
    )


def application_loop() -> Any:
    """Resolve the existing application loop, leaving CLI executors to own their loop."""
    from clio_agent.gact.context import active_app

    return getattr(getattr(active_app(), "state", None), "mcp_app_loop", None)


def task_arguments(properties: Any) -> dict[str, Any]:
    """Add CLIO-only optional description metadata to an ordinary tool's declared arguments."""
    return {
        **(properties if isinstance(properties, dict) else {}),
        "_clio_task_description": {
            "type": "string",
            "maxLength": 2000,
            "description": "Optional task assignment; stripped before backend dispatch.",
        },
    }


def safe_description(tool: str, args: dict[str, Any]) -> str:
    """Derive a concise assignment using public location/query fields only."""
    for key in ("query", "target", "path", "root"):
        value = args.get(key)
        if isinstance(value, str) and value:
            # URLs may carry credentials/query tokens. Keep only the public path.
            if "://" in value:
                from urllib.parse import urlsplit, urlunsplit

                url = urlsplit(value)
                host = url.hostname or ""
                if ":" in host:
                    host = f"[{host}]"
                if url.port is not None:
                    host += f":{url.port}"
                value = urlunsplit((url.scheme, host, url.path, "", ""))
            return f"{tool}: {value[:240]}"
    return tool.replace("_", " ")
