"""What a ``run_code`` script can call: one stub per tool, grouped by server.

A tool named ``<server>_<name>`` (``fs_read_file``, ``synthetic-home_climate_list``)
becomes ``tools.<server>.<name>`` with the server name made a Python identifier
(``tools.synthetic_home.climate_list``); a tool without a server prefix is
``tools.<name>``. Each stub's docstring is the tool's signature (from its JSON schema)
and its whole description, so ``help(tools.fs.read_file)`` explains it. Deferred tools are
included: a script calls them without loading them first.

Tools whose ``callers`` setting is ``direct`` stay out of scripts: finishing, asking
the person, planning, todos, skills, memory writes and the task and agent controls
declare it, because they are steps of the agent, not data calls
(:mod:`clio_agent.gact.agents.tool_settings`). ``code`` tools are only here: the index
marks them, since the tool list does not show them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from clio_agent import conf

__all__ = ["index_text", "owning_server", "stub_table", "tool_group"]


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


def owning_server(name: str, servers: Iterable[str]) -> str:
    """The server a tool named ``<server>_<name>`` belongs to, ``""`` for none.

    The longest matching prefix wins (``home_lab_x`` is ``home_lab``'s, not ``home``'s);
    the one rule for the stubs, the tool settings and the deferred catalog's owners.
    """
    for server in sorted(servers, key=len, reverse=True):
        if name.startswith(f"{server}_") and len(name) > len(server) + 1:
            return server
    return ""


def tool_group(name: str, servers: Iterable[str]) -> tuple[str, str]:
    """``(group, function)`` for a tool: its server's identifier, or ``""``."""
    server = owning_server(name, servers)
    if server:
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


def stub_table(
    tools: Mapping[str, Any],
    servers: Iterable[str],
    *,
    direct_only: frozenset[str] = frozenset(),
    code_only: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """The stubs the kernel builds ``tools`` from (see ``kernel_main._Tools``).

    ``direct_only`` tools get no stub; ``code_only`` tools are marked for the index.
    """
    from clio_agent.gact.agents.clio_react import _function_tool  # noqa: PLC0415

    servers = tuple(servers)
    groups: dict[str, dict[str, Any]] = {}
    for name in sorted(tools):
        if name in direct_only:
            continue
        spec = _function_tool(tools[name])
        group, function = tool_group(name, servers)
        groups.setdefault(group, {})[function] = {
            "tool": name,
            "function": function,
            "signature": _signature(function, spec.parameters or {}),
            # Whole: help() is read on request, never placed in the prompt.
            "doc": str(spec.description or ""),
            "code_only": name in code_only,
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
    code_only = sorted(
        f"tools.{group}.{name}" if group else f"tools.{name}"
        for group, functions in (table.get("groups") or {}).items()
        for name, stub in functions.items()
        if stub.get("code_only")
    )
    if code_only:
        lines.append(f"Only in scripts (not in your tool list): {', '.join(code_only)}")
    return "\n".join(lines)
