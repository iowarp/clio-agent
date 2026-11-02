"""Per-session runtime context appended to the composed main prompt.

The composed prompt itself comes from the agent's plugins
(:mod:`clio_agent.plugins.compose`). What this module adds is runtime state the
plugins cannot know: the session's bound workspace, its write roots, the machine and
the session's own scratch folder. It is
labelled as runtime context in the prompt view, not attributed to a plugin.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import TYPE_CHECKING, Any

from clio_agent.gact import reminders
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI


def _runtime_active_workspace_context(app: "FastAPI", *, session_id: str = "") -> str:
    """Surface the session's workspace scope to the agent.

    Workspace facts tell the agent where generated artifacts belong. They change
    no permissions and force no paths: the file policy validates access. The text is
    the reminder ``clio.reminder.session_facts`` followed by its ``session_facts_*``
    pieces (``prompt_packs/builtin/reminders/``); the code decides which pieces apply.
    """

    if app is None:
        return ""
    from clio_agent.gact.agents import resolution as _resolution  # noqa: PLC0415

    root = _resolution._runtime_workspace_catalog_cwd(app, session_id=session_id)
    if root is None:
        return ""
    try:
        allowed_roots = _session_write_roots(root)
    except Exception as exc:  # noqa: BLE001 - policy env read; degrade to no roots line
        trace.event("WORKSPACE-CTX", "allowed-roots read failed (%s); omitting roots line", exc)
        allowed_roots = []
    from clio_agent.gact.node_folders import session_node_folder  # noqa: PLC0415

    def fact(piece: str, variables: dict[str, Any]) -> str | None:
        return reminders.render(f"session_facts{piece}", variables, app=app, session_id=session_id)

    tail = _session_scratch_lines(app, session_id)
    head = fact("", {"root": root})
    if head is None:  # switched off by the person: no session facts at all
        return ""
    lines: list[str | None] = [head]
    if node_folder := session_node_folder(app, session_id):
        lines.append(fact("_node_folder", {"folder": node_folder}))
    else:
        lines.append(fact("_workspace", {}))
        if allowed_roots:
            lines.append(fact("_write_roots", {"roots": ", ".join(allowed_roots)}))
    return "\n".join(line for line in [*lines, *tail] if line)


def _session_scratch_lines(app: Any, session_id: str) -> list[str | None]:
    """The machine and the session's own scratch folder (TD56), both per session.

    The folder is created here: naming it is the session's first need of it, and the file
    tools and the kernel fence only accept a folder that exists.
    """

    from clio_agent.runtime import session_scratch  # noqa: PLC0415

    where = {"app": app, "session_id": session_id}
    machine = {"machine": session_scratch.machine_name()}
    lines = [reminders.render("session_facts_machine", machine, **where)]
    if folder := session_scratch.session_scratch_dir(session_id):
        lines.append(reminders.render("session_facts_scratch", {"folder": folder}, **where))
    return lines


def _session_write_roots(root: Any) -> list[str]:
    """The roots this session's file tools may write, its workspace first.

    Read with the workspace bound, as a tool call reads them: outside that binding the
    policy holds only ``CLIO_ALLOWED_ROOTS`` (by default the server's working directory),
    and naming that alone would point the agent at the server's folder instead of its
    workspace. CLIO's own state and cache folders for the workspace are left out; the
    agent's file tools do not write there.
    """
    from clio_agent import paths  # noqa: PLC0415
    from clio_agent.tools.execution import tool_workspace_context  # noqa: PLC0415
    from clio_agent.tools.file_policy import FileAccessPolicy  # noqa: PLC0415

    with tool_workspace_context(root):
        roots = FileAccessPolicy.from_env().allowed_roots
    internal = {
        paths.workspace_state_dir(root).resolve(),
        paths.workspace_cache_dir(root).resolve(),
    }
    return [str(item) for item in roots if item not in internal]


def _provider_summary(provider: Any) -> str:
    if provider is None:
        return "{}"
    # ``app.state.lm_config`` is a plain dict (set by ``PUT /v1/providers/lm``);
    # a genuinely unserializable input is stringified with a trace reason.
    try:
        if is_dataclass(provider) and not isinstance(provider, type):
            payload: Any = asdict(provider)
        elif isinstance(provider, Mapping):
            payload = dict(provider)
        else:
            raise TypeError(
                f"provider summary is neither a dataclass nor a mapping: {type(provider).__name__}"
            )
        return json.dumps(payload, sort_keys=True)
    except Exception as exc:  # noqa: BLE001 - genuinely bad input; stringify
        trace.event("PROMPT-CTX", "provider summary serialize failed (%s); using repr", exc)
        return str(provider)


def _prompt_render_context(app: "FastAPI") -> dict[str, str]:
    """The CLIO-owned dynamic context exposed to prompt templates.

    Session-scoped keys (the session's agent, its subagents and the commands
    it may invoke) are added by the request-scoped builder in ``gact.app``.
    """

    from clio_agent.gact.catalog import _builtin_tools  # noqa: PLC0415

    tools = [f"- {tool.id}: {tool.description}" for tool in _builtin_tools()]
    return {
        "tools.available": "\n".join(tools) or "(no declared tools)",
        "memory.policy_summary": "Session-local by default; same-workspace/global reads require explicit policy or user intent.",
        "permissions.policy_summary": "Permission-controlled actions must use CLIO policy gates and visible provenance.",
        "provider.current": _provider_summary(getattr(app.state, "lm_config", None)),
    }
