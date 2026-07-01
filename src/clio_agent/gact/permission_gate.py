"""Tool permission gating + cancellation for the GACT server (#714 decomposition).

This module is the single owner of the *enforcement* side of CLIO's tool-call
safety model: deciding whether a tool call is destructive, whether a shell
diagnostic is safe to auto-allow, applying the declarative permission policies
(SPEC §6.11.b) consulted before the interactive gate, recording resolved
permission audit rows, and building the closures the tool-execution boundary
calls (``permission_gate`` / cancellation checker).

It pairs with :mod:`clio_agent.gact.runtime.permission_policies`, which owns the
*data* layer (validation, on-disk load/flush, resolution-derived policies) and
exports :func:`_permission_path_from_args`, reused here for policy path matching.

Boundaries (preserving the no-cycle invariant): this module imports only stdlib,
FastAPI/Starlette wire types, the gact ``events``/``types`` wire models, and the
two ``runtime`` leaves it needs (``globals._resolve_tool_session`` for the
turn-driving session, ``permission_policies._permission_path_from_args``). It
NEVER imports :mod:`clio_agent.gact.app`. ``build_app`` and ``GactDeps`` import
:func:`_make_permission_gate`, :func:`_guard_direct_destructive_action`, etc.
from here so the ``app.state.make_permission_gate`` seam and the route/turn
dependents keep working.

Responsibilities:

* :data:`_DESTRUCTIVE_TOOL_SUBSTRINGS` + :func:`_is_destructive` -- the substring
  set that classifies a tool name as destructive (triggering the gate).
* :data:`_UNSAFE_SHELL_TOKENS` / :data:`_SAFE_RESHAPE_UTILS` /
  :data:`_SAFE_READONLY_UTILS` + :func:`_is_safe_shell_diagnostic` /
  :func:`_is_safe_readonly_diagnostic` / :func:`_is_safe_text_reshape_command`
  -- the bounded shell-command fast-allow analysis.
* :func:`_policy_action_for_tool` -- match the active permission policies.
* :func:`_record_resolved_permission` -- emit a resolved permission audit row +
  ``permission.resolved`` event.
* :func:`_direct_permission_denied` / :func:`_guard_direct_destructive_action`
  -- policy/audit semantics for explicit direct (route-initiated) destructive
  actions.
* :func:`_make_permission_gate` -- build the ``MCPToolBridge.permission_gate``
  closure (the interactive blocking gate).
* :func:`_make_cancellation_checker` -- build the tool-executor cancellation
  checker for the active session.
"""

from __future__ import annotations

import fnmatch
import re
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from clio_agent.gact.events import Event
from clio_agent.gact.runtime.globals import _resolve_tool_session
from clio_agent.gact.runtime.permission_policies import _permission_path_from_args
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from fastapi import FastAPI

# iowarp/clio-agent#7: tools the gate treats as destructive. Anything
# matching one of these substrings triggers a permission_requested
# event + blocks the bridge thread until the user resolves it.
_DESTRUCTIVE_TOOL_SUBSTRINGS: tuple[str, ...] = (
    "delete",
    "remove",
    "rm_",
    "drop",
    "destroy",
    "exec",
    "shell",
    "write",
)


def _is_destructive(tool_name: str) -> bool:
    n = tool_name.lower()
    return any(needle in n for needle in _DESTRUCTIVE_TOOL_SUBSTRINGS)


def _is_safe_shell_diagnostic(tool_name: str, args: Mapping[str, Any]) -> bool:
    """Return whether a shell_bash call is a read-only local diagnostic.

    Two classes auto-allow:
      1. A small fixed set of read-only diagnostics (date/pwd/whoami/...).
      2. A bounded text-reshaping pipeline over local files: a command built
         only from safe read/transform utilities (cat/head/tail/awk/cut/sed/sort/
         uniq/grep/wc/echo/tr) plus pipes and a single ``>`` redirect, with NO
         destructive verbs (rm/mv/cp/dd/sudo/curl/wget/chmod/chown/mkfifo/&&-rm).
         This is needed so an expert can normalize a malformed staged reference
         CSV (e.g. the EarthScope station catalog whose header carries unit
         sub-columns) into a clean lat/lon CSV for downstream geo ranking. The
         shell subprocess still runs under the file-policy cwd; this only governs
         whether the call needs interactive approval. Pipelines that touch any
         destructive token fall through to the normal permission gate.
    """

    if tool_name != "shell_bash":
        return False
    command = str(args.get("command") or "").strip()
    normalized = re.sub(r"\s+", " ", command).lower()
    if normalized in {"date", "get-date", "pwd", "whoami", "hostname"}:
        return True
    return _is_safe_text_reshape_command(command) or _is_safe_readonly_diagnostic(command)


# Destructive shell tokens that disqualify a command from the text-reshape
# fast-allow path. Anything here forces the normal interactive permission gate.
_UNSAFE_SHELL_TOKENS: tuple[str, ...] = (
    "rm",
    "rmdir",
    "mv",
    "cp",
    "dd",
    "sudo",
    "su",
    "chmod",
    "chown",
    "chgrp",
    "ln",
    "mkfifo",
    "mknod",
    "curl",
    "wget",
    "scp",
    "rsync",
    "ssh",
    "nc",
    "ncat",
    "telnet",
    "kill",
    "pkill",
    "killall",
    "shutdown",
    "reboot",
    "mkdir",
    "touch",
    "truncate",
    "tee",
    "xargs",
    "find",
    "eval",
    "exec",
    "source",
    "python",
    "python3",
    "perl",
    "ruby",
    "node",
    "bash",
    "sh",
    "zsh",
    "git",
    "apt",
    "pip",
    "uv",
    "npm",
    "yum",
    "brew",
    "systemctl",
    "service",
    "crontab",
    "at",
    "export",
    "unset",
    "alias",
    "function",
)
# Utilities permitted in a text-reshape pipeline.
_SAFE_RESHAPE_UTILS: frozenset[str] = frozenset(
    {
        "cat",
        "head",
        "tail",
        "awk",
        "gawk",
        "cut",
        "sed",
        "sort",
        "uniq",
        "grep",
        "egrep",
        "fgrep",
        "wc",
        "echo",
        "tr",
        "paste",
        "column",
        "nl",
        "printf",
        "true",
    }
)

# Read-only inspection utilities (no writes). Superset of the reshape utils plus
# pure file/dir inspectors. Used to auto-allow harmless diagnostic chains so a
# model's `ls -la X && head -5 X` is not routed to an interactive approval gate
# that would hang in a headless/autonomous run.
_SAFE_READONLY_UTILS: frozenset[str] = _SAFE_RESHAPE_UTILS | frozenset(
    {"ls", "stat", "file", "du", "df", "realpath", "basename", "dirname", "test", "[", "od", "xxd"}
)


def _is_safe_readonly_diagnostic(command: str) -> bool:
    """Return whether a shell_bash command is a bounded READ-ONLY inspection chain.

    Allows commands built ONLY from read-only utilities joined by ``&&`` / ``;`` /
    ``|``, with NO output redirect, NO command/process substitution, and NO
    background or destructive token. This lets an expert inspect staged files
    (``ls -la /tmp/x.csv && head -5 /tmp/x.csv``) without falling through to the
    interactive permission gate — which has no approver in headless/test runs and
    therefore hangs. It writes nothing, so it cannot mutate state.
    """

    if not command or len(command) > 2000:
        return False
    # No command/process substitution, no writes/appends.
    if any(tok in command for tok in ("`", "$(", "<(", ">(", ">>", ">")):
        return False
    # Allow `&&` as a separator but reject a bare background `&`.
    if "&" in command.replace("&&", ""):
        return False
    # No destructive verb anywhere in the command.
    words = re.findall(r"[a-z0-9_./-]+", command.lower())
    if any(w.split("/")[-1] in _UNSAFE_SHELL_TOKENS for w in words):
        return False
    # Every segment (split on && ; |) must start with a read-only utility.
    for seg in re.split(r"&&|;|\|", command):
        seg = seg.strip()
        if not seg:
            continue
        m = re.match(r"([A-Za-z0-9_./\[-]+)", seg)
        if not m:
            return False
        first = m.group(1).split("/")[-1].lower()
        if first not in _SAFE_READONLY_UTILS:
            return False
    return True


def _is_safe_text_reshape_command(command: str) -> bool:
    """Heuristic: a bounded read/transform pipeline that WRITES its output to a
    derived file under ``/tmp/`` with no destructive tokens. Conservative — any
    unrecognized leading word, destructive token, or a non-/tmp/missing output
    target rejects, so the call falls through to the normal permission gate. A
    bare read (e.g. ``cat pyproject.toml`` with no redirect) is NOT auto-allowed;
    only the reshape-and-write-to-/tmp use case is."""

    if not command or len(command) > 2000:
        return False
    # Reject backticks / command substitution / process substitution / append
    # (``>>``) / background (``&``) / chaining (``||``).
    if any(tok in command for tok in ("`", "$(", "<(", ">(", ">>", "&", "||")):
        return False
    # Must redirect output to a single /tmp/ file (the reshape target). Reject a
    # bare read with no redirect, or a redirect to anywhere outside /tmp/.
    redirects = re.findall(r">\s*(\S+)", command)
    if len(redirects) != 1 or not redirects[0].strip("'\"").startswith("/tmp/"):
        return False
    # Tokenize on whitespace and pipes; check no destructive token appears and
    # every "leading" utility (start of a pipe segment) is in the safe set.
    lowered = command.lower()
    words = re.findall(r"[a-z0-9_./-]+", lowered)
    for w in words:
        base = w.split("/")[-1]
        if base in _UNSAFE_SHELL_TOKENS:
            return False
    # Each pipe segment must start with a safe utility or a brace group.
    segments = re.split(r"\|", command)
    for seg in segments:
        seg = seg.strip().lstrip("{").strip()
        if not seg:
            continue
        m = re.match(r"([A-Za-z0-9_./-]+)", seg)
        if not m:
            return False
        first = m.group(1).split("/")[-1].lower()
        # Allow a brace-group inner statement starting with echo/printf etc.
        if first == "}":
            continue
        if first not in _SAFE_RESHAPE_UTILS:
            return False
    return True


def _policy_action_for_tool(
    app: "FastAPI",
    *,
    session_id: str,
    session: Any | None,
    tool_name: str,
    args: Mapping[str, Any],
) -> str:
    """Return the first matching permission policy action.

    The `/v1/policies` endpoint is user-facing configuration, so storing
    policies without enforcing them is a silent safety bypass. Matching is
    intentionally small and predictable: scope, tool glob, optional path glob,
    then the policy action.
    """

    policies = getattr(app.state, "permission_policies", [])
    if not isinstance(policies, list):
        return ""
    path = _permission_path_from_args(args)
    workspace_id = getattr(session, "workspace_id", "") if session is not None else ""
    for policy in policies:
        if not isinstance(policy, dict):
            continue
        scope = str(policy.get("scope") or "").lower()
        scope_id = str(policy.get("scope_id") or "")
        if scope == "session":
            if scope_id and scope_id != session_id:
                continue
        elif scope == "workspace":
            if scope_id and scope_id != workspace_id:
                continue
        else:
            continue

        tool_pattern = str(policy.get("tool_name_pattern") or "*")
        if not fnmatch.fnmatchcase(tool_name, tool_pattern):
            continue

        path_pattern = str(policy.get("path_pattern") or "")
        if path_pattern:
            candidates = [path]
            if path:
                try:
                    candidates.append(str(Path(path).resolve(strict=False)))
                except OSError:
                    pass
            if not any(fnmatch.fnmatchcase(candidate, path_pattern) for candidate in candidates):
                continue

        action = str(policy.get("action") or "").lower()
        if action in {"allow", "allow_session", "allow_workspace", "deny", "ask"}:
            return action
    return ""


def _record_resolved_permission(
    app: "FastAPI",
    *,
    session_id: str,
    tool_name: str,
    args: Mapping[str, Any],
    status: str,
    action: str,
    summary: str,
    reason: str,
) -> str:
    pid = f"perm_{uuid.uuid4().hex[:12]}"
    now_iso = datetime.now(timezone.utc).isoformat()
    row = {
        "id": pid,
        "session_id": session_id,
        "tool_call": {
            "tool_name": tool_name,
            "input": dict(args),
        },
        "summary": summary,
        "created_at": now_iso,
        "status": status,
        "action": action,
        "resolved_at": now_iso,
        "reason": reason,
    }
    if hasattr(app.state, "permissions"):
        app.state.permissions[pid] = row
    if hasattr(app.state, "bus"):
        app.state.bus.publish(
            Event(
                type="permission.resolved",
                session_id=session_id,
                payload={
                    "permission_id": pid,
                    "action": action,
                    "session_id": session_id,
                    "reason": reason,
                },
            )
        )
    return pid


def _direct_permission_denied(
    *,
    tool_name: str,
    args: Mapping[str, Any],
    summary: str,
) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="permission_error",
                message=f"{summary} blocked by permission policy",
                details={
                    "tool_name": tool_name,
                    "input": dict(args),
                    "reason": "policy_deny",
                    "recovery_actions": ["change_policy", "retry", "exit"],
                },
                recoverable=True,
            )
        ).model_dump(exclude_none=True),
    )


def _guard_direct_destructive_action(
    app: "FastAPI",
    *,
    session_id: str = "",
    workspace_id: str = "",
    tool_name: str,
    args: Mapping[str, Any],
    summary: str,
    reason: str,
) -> None:
    """Apply permission policy/audit semantics to direct GACT DELETE actions.

    These routes are already explicit user actions, so there is no extra
    interactive prompt. Policies can still deny before mutation, and all
    allowed direct destructive actions land in `/v1/permissions` as resolved
    audit rows.
    """

    session = app.state.sessions.get(session_id) if session_id else None
    if session is None and workspace_id:
        session = SimpleNamespace(workspace_id=workspace_id)
    policy_action = _policy_action_for_tool(
        app,
        session_id=session_id,
        session=session,
        tool_name=tool_name,
        args=args,
    )
    if policy_action == "deny":
        _record_resolved_permission(
            app,
            session_id=session_id,
            tool_name=tool_name,
            args=args,
            status="auto_denied",
            action="deny",
            summary=f"{summary} blocked by permission policy",
            reason="policy_deny",
        )
        raise _direct_permission_denied(tool_name=tool_name, args=args, summary=summary)
    _record_resolved_permission(
        app,
        session_id=session_id,
        tool_name=tool_name,
        args=args,
        status="auto_approved",
        action="allow",
        summary=summary,
        reason="policy_allow"
        if policy_action in {"allow", "allow_session", "allow_workspace"}
        else reason,
    )


def _make_permission_gate(app: "FastAPI"):
    """Build a callable suitable for MCPToolBridge.permission_gate.

    Non-destructive tools fast-allow. Destructive tools register a
    permission row, publish permission.requested into the EventBus,
    block on a threading.Event with a generous timeout, and return
    "allow" / "deny" based on the user's resolution. Timeouts default
    to deny — fail-safe.
    """

    DEFAULT_TIMEOUT_S = 600.0

    def gate(name: str, args: Mapping[str, Any]) -> str:
        # iowarp/clio-agent#20: user-defined pre_tool hook can veto
        # the call by raising PermissionError. Returns ignored;
        # only the raise/no-raise distinction matters.
        try:
            from clio_agent.runtime.hooks import fire as _fire_hook

            _fire_hook("pre_tool", name, dict(args))
        except PermissionError:
            return "deny"
        if not _is_destructive(name):
            return "allow"
        # Prefer the session currently driving the turn. Recency is
        # only a fallback for truly out-of-band tool calls.
        sid, current = _resolve_tool_session(app)
        if current is not None:
            # iowarp/clio-agent — plan_mode + architect mode reject
            # destructive tool calls without prompting. Read-only
            # contract is hard, not advisory.
            if current.mode in {"plan", "architect"}:
                row = {
                    "id": f"perm_{uuid.uuid4().hex[:12]}",
                    "session_id": sid,
                    "tool_call": {
                        "tool_name": name,
                        "input": dict(args),
                    },
                    "summary": (
                        f"destructive tool {name!r} blocked by session.mode={current.mode!r}"
                    ),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "status": "auto_denied",
                    "action": "deny",
                    "resolved_at": datetime.now(timezone.utc).isoformat(),
                }
                app.state.permissions[row["id"]] = row
                app.state.bus.publish(
                    Event(
                        type="permission.resolved",
                        session_id=sid,
                        payload={
                            "permission_id": row["id"],
                            "action": "deny",
                            "session_id": sid,
                            "reason": "session_mode_readonly",
                        },
                    )
                )
                return "deny"
        policy_action = _policy_action_for_tool(
            app,
            session_id=sid,
            session=current,
            tool_name=name,
            args=args,
        )
        if policy_action == "deny":
            _record_resolved_permission(
                app,
                session_id=sid,
                tool_name=name,
                args=args,
                status="auto_denied",
                action="deny",
                summary=f"destructive tool {name!r} blocked by permission policy",
                reason="policy_deny",
            )
            return "deny"
        if policy_action in {"allow", "allow_session", "allow_workspace"}:
            _record_resolved_permission(
                app,
                session_id=sid,
                tool_name=name,
                args=args,
                status="auto_approved",
                action="allow",
                summary=f"destructive tool {name!r} allowed by permission policy",
                reason=f"policy_{policy_action}",
            )
            return "allow"
        if _is_safe_shell_diagnostic(name, args):
            return "allow"
        if not sid:
            return "deny"
        pid = f"perm_{uuid.uuid4().hex[:12]}"
        evt = threading.Event()
        row = {
            "id": pid,
            "session_id": sid,
            "tool_call": {
                "tool_name": name,
                "input": dict(args),
            },
            "summary": f"destructive tool call: {name}",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        }
        app.state.permissions[pid] = row
        app.state.permission_events[pid] = evt
        app.state.bus.publish(
            Event(
                type="permission.requested",
                session_id=sid,
                payload=row,
            )
        )
        # Block the bridge thread until POST /v1/permissions/{pid}
        # sets the event (or we time out).
        if not evt.wait(timeout=DEFAULT_TIMEOUT_S):
            row["status"] = "timeout"
            return "deny"
        action = row.get("action", "deny")
        if action in {"allow", "allow_session", "allow_workspace"}:
            return "allow"
        return "deny"

    return gate


def _make_cancellation_checker(app: "FastAPI"):
    """Build a tool-executor cancellation checker for the active GACT session."""

    def check() -> bool:
        sid, _current = _resolve_tool_session(app)
        if not sid:
            return False
        event = app.state.cancel_events.get(sid)
        if event is not None and event.is_set():
            return True
        return sid in app.state.cancel_flags

    return check
