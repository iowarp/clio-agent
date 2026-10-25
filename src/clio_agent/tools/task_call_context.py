"""CLIO-owned task metadata, kept outside backend tool arguments."""

from __future__ import annotations

from contextvars import ContextVar, Token
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Lock
from typing import Any


@dataclass(repr=False)
class InvocationApproval:
    """One resolved gate decision, bound to this exact native submission only."""

    app: Any
    session_id: str
    workspace_id: str
    workspace_root: str
    assignment_id: str
    invocation_id: str
    tool: str
    arguments_digest: str
    permissions_digest: str
    _consumed: bool = field(default=False, init=False, repr=False)
    _lock: Any = field(default_factory=Lock, init=False, repr=False)

    def bind(self, invocation_id: str) -> InvocationApproval | None:
        """Bind to one observer call; never clone or rebind an approval."""
        with self._lock:
            if self.invocation_id or self._consumed or not invocation_id:
                return None
            self.invocation_id = invocation_id
            return self

    def consume(
        self,
        app: Any,
        session_id: str,
        assignment_id: str,
        invocation_id: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> bool:
        """Consume once; changed arguments, custody or policy require a fresh gate."""
        from clio_agent.arc.task_types import argument_digest

        with self._lock:
            if self._consumed:
                return False
            self._consumed = True
        session = app.state.sessions.get(session_id)
        workspace = app.state.workspaces.get(session.workspace_id) if session else None
        return bool(
            app is self.app
            and session is not None
            and workspace is not None
            and session_id == self.session_id
            and session.workspace_id == self.workspace_id
            and str(workspace.root_path) == self.workspace_root
            and assignment_id == self.assignment_id
            and bool(self.invocation_id)
            and invocation_id == self.invocation_id
            and tool == self.tool
            and argument_digest(arguments) == self.arguments_digest
            and argument_digest(permission_snapshot(app, session_id)) == self.permissions_digest
        )


@dataclass(frozen=True)
class TaskCall:
    """The original invocation identity and application-owned executor."""

    executor: Any = None
    description: str = ""
    invocation_id: str = ""
    owner_agent: str = ""
    session_id: str = ""
    submission_handle: str = ""
    approval: InvocationApproval | None = None


_EMPTY_CALL = TaskCall()
TASK_CALL: ContextVar[TaskCall] = ContextVar("clio_task_call", default=_EMPTY_CALL)


def permission_snapshot(app: Any, sid: str) -> dict[str, Any]:
    """Capture current session posture and only policies relevant to this owner."""
    from clio_agent.gact.planning import active_playbook_allowed_tools

    session = app.state.sessions.get(sid)
    if session is None:
        raise RuntimeError("Task permission owner is absent")
    policies = [
        row
        for row in getattr(app.state, "permission_policies", [])
        if row.get("scope", "global") == "global"
        or row.get("scope") == "session"
        and (not row.get("scope_id") or row.get("scope_id") == sid)
        or row.get("scope") == "workspace"
        and (not row.get("scope_id") or row.get("scope_id") == session.workspace_id)
    ]
    return deepcopy(
        {
            "mode": session.mode,
            "approval_mode": session.approval_mode,
            "approval_profile": session.approval_profile,
            "playbook_allowed": active_playbook_allowed_tools(session),
            "policies": policies,
        }
    )


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
            approval=inherited.approval,
        )
    )
    return forwarded, token


def capture_approval(tool: str, arguments: dict[str, Any]) -> InvocationApproval | None:
    """Capture pre-gate custody; bind only after that gate allows these arguments."""
    from clio_agent.arc.task_types import argument_digest
    from clio_agent.gact import context

    turn = context.current_turn()
    app, sid = turn.app, context.active_session_id()
    session = app.state.sessions.get(sid) if app is not None else None
    workspaces = getattr(app.state, "workspaces", None) if app is not None else None
    workspace = workspaces.get(session.workspace_id) if session and workspaces else None
    if turn.assignment_handle and session is not None and workspace is not None:
        public = dict(arguments)
        public.pop("_clio_task_description", None)
        return InvocationApproval(
            app=app,
            session_id=sid,
            workspace_id=session.workspace_id,
            workspace_root=str(workspace.root_path),
            assignment_id=turn.assignment_handle,
            invocation_id="",
            tool=tool,
            arguments_digest=argument_digest(public),
            permissions_digest=argument_digest(permission_snapshot(app, sid)),
        )
    return None


def begin_invocation(*, approval: InvocationApproval | None = None) -> Token[TaskCall]:
    """Bind an optional resolved approval to the actual observer invocation once."""
    from clio_agent.gact import context
    from clio_agent.gact.artifacts.observer_bridge import observer_call_id

    invocation_id = observer_call_id()
    bound = approval.bind(invocation_id) if approval is not None else None
    return TASK_CALL.set(
        TaskCall(
            invocation_id=invocation_id,
            owner_agent=context.active_react_scope(),
            session_id=context.active_session_id(),
            approval=bound,
        )
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
            "description": (
                "Optional assignment label for accepted work; stripped before backend dispatch. "
                "This metadata does not enable background execution or turn a plain call into a task."
            ),
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
