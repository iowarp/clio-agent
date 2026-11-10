"""Approval-aware SSH execution for agents with shell capability."""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from clio_agent.gact import context
from clio_agent.gact.agents.tool_instrumentation import declare_structured_content, native_tool
from clio_agent.gact.infrastructure.models import SshRoute
from clio_agent.gact.infrastructure.ssh_execution import execute_ssh_command
from clio_agent.gact.infrastructure.ssh_result import SshCommandResult
from clio_agent.gact.permission_gate import _invoke_permission_gate
from clio_agent.tools.execution import current_tool_runtime
from clio_agent.tools.tool_presentation import present_mcp_result


def ssh_execute(
    command: str,
    ssh_profile: str = "",
    ssh_route: dict[str, Any] | None = None,
    timeout_s: float = 60,
    max_output_bytes: int = 8192,
) -> SshCommandResult:
    """Run a remote shell command through an approved, host-verified SSH route.

    Use it for remote commands: direct `ssh` in the local shell may be blocked by
    its network fence, which approval or bypass mode does not lift.
    Use ssh_profile for an existing OpenSSH alias, such as ares. For a custom
    host or hops use ssh_route instead: {host: compute.example, jump_hosts:
    [login], user: scientist}; the shared route also supports port and
    identity_file on this CLIO. Supply exactly one route, without a local
    `ssh` prefix. The command follows the REMOTE shell's syntax and account
    permissions; CLIO's local workspace write fence does not confine that host.
    Normal approval shows the destination and exact command. Bypass mode uses
    the same audited gate and verified connector without an interactive ask.
    No terminal/password prompts; unknown host keys require user setup.
    Command deadline is 0 < timeout_s <= 600 seconds. Oversize output is saved
    in session-owned spill files. A timeout/cancellation closes this channel
    but cannot guarantee remote descendants stopped; inspect before retrying.
    Returns normalized ssh_route, command, exit_code (null if unknown), status,
    stop_reason, timeout_s, stdout/stderr and their truncated flags. A truncated
    stream includes a *_spill receipt with path, byte/line counts and excerpts,
    or an explicit save error. An oversized command echo may be omitted with a
    command_omitted receipt; the original call input remains authoritative.
    Validation, approval and connection failures raise tool errors, not success.
    """
    if not isinstance(command, str) or not command.strip() or "\0" in command:
        raise ValueError("Provide a nonempty remote command without NUL characters")
    if len(command) > 65536:
        raise ValueError("Remote command exceeds 65,536 characters; use a remote script")
    if isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 600:
        raise ValueError("SSH command timeout_s must be greater than zero and at most 600")
    if (
        not isinstance(max_output_bytes, int)
        or isinstance(max_output_bytes, bool)
        or not 1 <= max_output_bytes <= 1048576
    ):
        raise ValueError("max_output_bytes must be between 1 and 1,048,576")
    if bool(ssh_profile) == bool(ssh_route):
        raise ValueError("Choose exactly one SSH profile or shared SSH route")
    route = SshRoute.model_validate(ssh_route) if ssh_route else SshRoute(profile=ssh_profile)
    if not (route.profile or route.host):
        raise ValueError("Choose an SSH destination")
    app, sid = context.active_app(), context.active_session_id()
    session = app.state.sessions.get(sid) if app is not None and sid else None
    if app is None or session is None:
        raise ValueError("SSH execution requires an active workspace conversation")
    workspace = app.state.workspaces.get(session.workspace_id)
    if workspace is None or not workspace.root_path:
        raise ValueError("SSH execution requires an active workspace conversation")
    history = app.state.task_history
    with history.serialized():
        assignment = history.latest_assignment(sid)
        if assignment is None:
            raise ValueError("SSH execution requires a confirmed assignment")
        app.state.conversations.require_admission(assignment)
    args = {
        "command": command,
        "ssh_route": route.model_dump(),
        "timeout_s": timeout_s,
        "max_output_bytes": max_output_bytes,
    }
    destination = " → ".join([*route.jump_hosts, route.profile or route.host])
    gate = getattr(app.state, "pending_permission_gate", None) or app.state.make_permission_gate()
    if (
        _invoke_permission_gate(
            gate,
            "ssh_execute",
            args,
            {"kind": "native", "summary": f"Run SSH command on {destination}: {command}"[:1000]},
        )
        != "allow"
    ):
        raise PermissionError("SSH command was not approved")
    cancelled = current_tool_runtime().cancellation_checker
    with history.serialized():
        app.state.conversations.require_admission(assignment)
    if cancelled is not None and cancelled():
        raise InterruptedError("SSH command cancelled before connection")
    result = execute_ssh_command(
        route,
        command,
        root=Path(workspace.root_path),
        session_id=sid,
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes,
        cancelled=cancelled,
    )
    declare_structured_content(result)
    return cast("SshCommandResult", result)


def ssh_start_presentation(args: Mapping[str, Any]) -> dict[str, Any]:
    """Show the real remote destination and command before execution completes."""
    presentation = present_mcp_result("shell_bash", args, {"structuredContent": {}})
    route = args.get("ssh_route")
    destination = (
        (route.get("profile") or route.get("host"))
        if isinstance(route, Mapping)
        else args.get("ssh_profile")
    )
    presentation["action"] = f"Run SSH on {destination}" if destination else "Run SSH"
    return presentation


def ssh_presentation(args: Mapping[str, Any], result: Any, structured: Any) -> dict[str, Any]:
    """Present the actual remote exit state, including failed command results."""
    row = structured if isinstance(structured, Mapping) else result
    if not isinstance(row, Mapping):
        return {
            "summary": "SSH result unavailable",
            "blocks": [],
            "diagnostic": "invalid_ssh_result",
        }
    failed = row.get("status") != "completed"
    presentation = present_mcp_result(
        "shell_bash",
        args,
        {"structuredContent": {**row, "timed_out": row.get("stop_reason") == "timeout"}},
    )
    presentation["action"] = ssh_start_presentation({**args, "ssh_route": row.get("ssh_route")})[
        "action"
    ]
    stop = row.get("stop_reason", "failed")
    exit_code = row.get("exit_code")
    presentation["summary"] = (
        f"Exit {exit_code}" if stop == "completed" else f"SSH {stop}; exit status unavailable"
    )
    presentation["status"] = "failed" if failed else "completed"
    if (
        not row.get("stdout")
        and not row.get("stderr")
        and not any(row.get(f"{stream}_truncated") for stream in ("stdout", "stderr"))
    ):
        presentation["blocks"].append(
            {"id": "empty-output", "type": "text", "text": "No stdout or stderr was captured."}
        )
    if row.get("remote_process_state"):
        presentation["blocks"].append(
            {"id": "remote-state", "type": "text", "text": row["remote_process_state"]}
        )
    return presentation


def build_ssh_execute_tool() -> Any:
    """Declare shared SSH execution without granting an agent local shell authority."""
    return native_tool(
        ssh_execute,
        name="ssh_execute",
        desc=ssh_execute.__doc__,
        args={
            "command": {
                "type": "string",
                "minLength": 1,
                "maxLength": 65536,
                "description": "Command in the remote shell's syntax; do not prefix with local ssh.",
            },
            "ssh_profile": {
                "type": "string",
                "default": "",
                "description": "Existing OpenSSH alias. Supply this or ssh_route, exactly one.",
            },
            "ssh_route": {
                **SshRoute.model_json_schema(),
                "default": None,
                "description": "Shared profile/custom host route, including ordered jump aliases. Use instead of ssh_profile.",
            },
            "timeout_s": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": 600,
                "default": 60,
                "description": "Remote command deadline after connection; not an end-to-end connection timeout.",
            },
            "max_output_bytes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1048576,
                "default": 8192,
                "description": "Per-stream inline capture limit; whole-result budget may also spill output to session files.",
            },
        },
        title="Run SSH command",
        domain="shell",
        presentation=ssh_presentation,
        presentation_start=ssh_start_presentation,
    )
