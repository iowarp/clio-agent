"""What a ``run_code`` script can call: one stub per tool, grouped by server.

A tool named ``<server>_<name>`` (``fs_read_file``, ``synthetic-home_climate_list``)
becomes ``tools.<server>.<name>`` with the server name made a Python identifier
(``tools.synthetic_home.climate_list``); a tool without a server prefix is
``tools.<name>``. Each stub's docstring is the tool's signature (from its JSON schema)
and description, so ``help(tools.fs.read_file)`` explains it. Deferred tools are
included: a script calls them without loading them first.

The conversation's control tools stay out of scripts: finishing, asking the person,
planning, todos, skills and the task and agent controls are steps of the agent, not
data calls.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from clio_agent import conf

__all__ = ["NOT_IN_SCRIPTS", "index_text", "stub_table", "tool_group"]

#: Tools a script cannot call.
NOT_IN_SCRIPTS = frozenset(
    {
        "run_code",
        "submit",
        "search_tools",
        "load_skill",
        "write_todos",
        "plan_enter",
        "plan_update",
        "plan_exit",
        "ask_user",
        "draft_alternatives",
        "spawn_agent_task",
        "spawn_agents_parallel",
        "spawn_skill_task",
        "fork",
        "wait_tasks",
        "query_tasks",
        "observe_tasks",
        "cancel_tasks",
        "get_task_result",
        "send_agent_message",
        "pause_agent",
        "resume_agent",
        "restart_agent",
        "set_goal",
        "wait_for",
        "set_loop",
        "loop_wakeup",
        "cron_create",
        "cron_delete",
        "raise_alert_card",
    }
)


def _doc_chars() -> int:
    """Characters of a tool's description kept in its stub's docstring (at least 1)."""
    value = conf.resolve(
        "code.stub_doc_chars", env="CLIO_CODE_STUB_DOC_CHARS", default=1500, cast=conf.as_int
    )
    if value < 1:
        raise ValueError(f"code.stub_doc_chars must be at least 1, not {value}")
    return value


def _index_line_chars() -> int:
    """Longest line of the ``tools`` index in ``run_code``'s description (at least 40)."""
    value = conf.resolve(
        "code.index_line_chars", env="CLIO_CODE_INDEX_LINE_CHARS", default=320, cast=conf.as_int
    )
    if value < 40:
        raise ValueError(f"code.index_line_chars must be at least 40, not {value}")
    return value


def _identifier(text: str) -> str:
    name = re.sub(r"\W", "_", text)
    return f"_{name}" if name[:1].isdigit() else name


def tool_group(name: str, servers: Iterable[str]) -> tuple[str, str]:
    """``(group, function)`` for a tool: its server's identifier, or ``""``."""
    for server in sorted(servers, key=len, reverse=True):
        if name.startswith(f"{server}_") and len(name) > len(server) + 1:
            return _identifier(server), _identifier(name[len(server) + 1 :])
    return "", _identifier(name)


def _signature(function: str, parameters: Mapping[str, Any]) -> str:
    properties = parameters.get("properties") or {}
    required = set(parameters.get("required") or ())
    args = []
    for arg, spec in properties.items():
        if arg.startswith("_clio_"):
            continue
        kind = spec.get("type", "any") if isinstance(spec, Mapping) else "any"
        kind = "|".join(kind) if isinstance(kind, list) else kind
        args.append(f"{arg}: {kind}" if arg in required else f"{arg}: {kind} = ...")
    return f"{function}({', '.join(args)})"


def stub_table(tools: Mapping[str, Any], servers: Iterable[str]) -> dict[str, Any]:
    """The stubs the kernel builds ``tools`` from (see ``kernel_main._Tools``)."""
    from clio_agent.gact.agents.clio_react import _function_tool  # noqa: PLC0415

    servers = tuple(servers)
    doc_chars = _doc_chars()
    groups: dict[str, dict[str, Any]] = {}
    for name in sorted(tools):
        if name in NOT_IN_SCRIPTS:
            continue
        spec = _function_tool(tools[name])
        group, function = tool_group(name, servers)
        groups.setdefault(group, {})[function] = {
            "tool": name,
            "function": function,
            "signature": _signature(function, spec.parameters or {}),
            "doc": str(spec.description or "")[:doc_chars],
        }
    return {"groups": groups}


def index_text(table: Mapping[str, Any]) -> str:
    """One line per group: what ``tools`` holds, for the tool description."""
    lines = []
    line_chars = _index_line_chars()
    for group, functions in sorted((table.get("groups") or {}).items()):
        prefix = f"tools.{group}." if group else "tools."
        names = sorted(functions)
        line = f"{prefix}{{{', '.join(names)}}}"
        if len(line) > line_chars:
            shown: list[str] = []
            for name in names:
                if len(prefix) + len(", ".join([*shown, name])) > line_chars - 20:
                    break
                shown.append(name)
            line = f"{prefix}{{{', '.join(shown)}, +{len(names) - len(shown)} more}}"
        lines.append(f"- {line}")
    return "\n".join(lines)
