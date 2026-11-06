"""The one command table a session can run, and how a typed name finds its row.

Rows come from four kinds of source:

* ``builtin``: the core plugin's command files that name a ``handler:``.
* ``command``: a resolved plugin's ``commands/`` files, and the workspace and user
  command files (``.clio``/``.claude`` in the workspace, then the user's).
* ``skill``: every skill the session's agent resolves (:mod:`clio_agent.gact.skill_commands`).
* ``mcp_prompt``: every prompt the agent's MCP servers offer
  (:mod:`clio_agent.gact.mcp_prompt_commands`).

Every plugin command answers to ``/<plugin>:<name>`` as well as ``/<name>``; a
workspace file to ``/project:<name>`` and a user file to ``/user:<name>``. Clashes
are visible, never silent: a core name used elsewhere is ``command_name_reserved``,
a command file that loses on precedence is ``command_shadowed`` (naming the file
that wins), and a bare name offered by more than one source is ambiguous, so only
its qualified names run it. ``GET /v1/commands`` and the dispatch route read this
same table, so listing and running cannot disagree.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.agents.resolution import SessionAgentUnavailable, session_resolution
from clio_agent.plugins.command_text import expand_plugin_text
from clio_agent.plugins.commands import (
    CORE_COMMANDS,
    command_files,
    command_name,
    read_command_meta,
    read_commands,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

#: Frontmatter a command file may carry that CLIO lists but does not act on.
UNSUPPORTED_FIELDS = ("model", "allowed-tools", "allowed_tools", "disable-model-invocation")
#: Frontmatter asking CLIO to run a local shell command, which user commands never do.
SHELL_FIELDS = ("shell", "exec", "run", "command_line", "command-line")
#: The qualifier of a command file by where it lives.
FILE_QUALIFIER = {
    "clio_workspace": "project",
    "claude_workspace": "project",
    "clio_user": "user",
    "claude_user": "user",
}
#: Fields a row carries for the server only; the wire row leaves them out.
_INTERNAL = ("prompt_template", "root")


def _slash(name: str) -> str:
    name = str(name or "").strip()
    return name if name.startswith("/") else f"/{name}"


def command_cwd_for_request(
    app: "FastAPI", session_id: str = "", workspace_id: str = ""
) -> Path | None:
    """The workspace root a request's command files are read from."""

    wid = workspace_id
    if session_id:
        sess = app.state.sessions.get(session_id)
        if sess is not None:
            wid = wid or str(getattr(sess, "workspace_id", "") or "")
    if not wid:
        return None
    ws = app.state.workspaces.get(wid)
    root_path = str(getattr(ws, "root_path", "") or "") if ws is not None else ""
    return Path(root_path).expanduser() if root_path else None


def file_roots(home: Path, cwd: Path) -> list[tuple[Path, str]]:
    """Command-file folders in precedence order: workspace ``.clio``, ``.claude``, then user."""

    from clio_agent import paths  # noqa: PLC0415 - avoid an import cycle at module load

    return [
        (paths.workspace_config_path(cwd, "commands"), "clio_workspace"),
        (cwd / ".claude" / "commands", "claude_workspace"),
        (paths.user_config_dir_for(home, os.environ) / "commands", "clio_user"),
        (home / ".claude" / "commands", "claude_user"),
    ]


def _arguments(meta: Mapping[str, Any]) -> list[Any]:
    value = meta.get("arguments") or []
    return value if isinstance(value, list) else [value]


def file_row(
    path: Path,
    *,
    label: str,
    source: str,
    kind: str = "command",
    plugin: str = "",
    root: str = "",
) -> dict[str, Any]:
    """One command file as a table row (not yet placed: clashes are decided by the table)."""

    meta, body, problem = read_command_meta(path)
    body = body.strip()
    if root:
        # A plugin's command names its own files; resolve them against the plugin folder.
        body = expand_plugin_text(body, Path(root))
    name = command_name(meta, path)
    description = str(meta.get("description") or "").strip()
    if not description:
        description = next((line.strip()[:240] for line in body.splitlines() if line.strip()), "")
    status, reason, detail = "available", "", ""
    if problem:
        status, reason, detail = "unsupported", "command_invalid", problem
    elif any(key in meta for key in SHELL_FIELDS):
        status, reason = "unsupported", "command_shell_unsupported"
        detail = "CLIO commands do not run local shell commands"
    qualifier = plugin or FILE_QUALIFIER.get(source, "")
    return {
        "kind": kind,
        "id": _slash(name),
        "name": _slash(name),
        "qualified_id": _slash(f"{qualifier}:{name}") if qualifier else _slash(name),
        "plugin": plugin,
        "source": source,
        "label": label,
        "title": str(meta.get("title") or _slash(name)).strip(),
        "description": description,
        "argument_hint": str(meta.get("argument-hint") or meta.get("argument_hint") or ""),
        "arguments": _arguments(meta),
        "aliases": [],
        "status": status,
        "enabled": status == "available",
        "disabled_reason": reason,
        "disabled_detail": detail,
        "unsupported_fields": [key for key in UNSUPPORTED_FIELDS if key in meta],
        "agent": str(meta.get("agent") or meta.get("agent_id") or "").strip(),
        "handler": str(meta.get("handler") or "").strip(),
        "path": path.as_posix(),
        "prompt_template": body,
        "root": root,
    }


def _builtin_rows(plugin_rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The core plugin's handler files: from the resolution, or read directly without one."""

    core = [row for row in plugin_rows if row.get("plugin") == "core" and row.get("handler")]
    if not core:
        records, _ = read_commands(CORE_COMMANDS, "core")
        core = [
            {
                "id": r.id,
                "plugin": "core",
                "path": r.path,
                "label": r.label,
                "handler": r.handler,
                "aliases": list(r.aliases),
            }
            for r in records
            if r.handler
        ]
    rows = []
    for record in core:
        row = file_row(
            Path(record["path"]), label="", source="builtin", kind="builtin", plugin="core"
        )
        row["aliases"] = [_slash(alias) for alias in record.get("aliases") or ()]
        rows.append(row)
    return rows


def _plugin_command_rows(plugin_rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for record in plugin_rows:
        if record.get("handler") and record.get("plugin") == "core":
            continue
        row = file_row(
            Path(record["path"]),
            label=str(record.get("label") or ""),
            source="plugin",
            plugin=str(record["plugin"]),
            root=str(record.get("root") or ""),
        )
        if row["handler"]:
            row.update(
                status="unsupported",
                enabled=False,
                disabled_reason="command_handler_not_builtin",
                disabled_detail="only built-in plugins may name a handler",
            )
        rows.append(row)
    return rows


def _file_command_rows(home: Path, cwd: Path) -> list[dict[str, Any]]:
    rows = []
    for root, source in file_roots(home, cwd):
        for path, label in command_files(root):
            rows.append(file_row(path, label=label, source=source))
    return rows


def _disable(row: dict[str, Any], reason: str, detail: str, **extra: Any) -> None:
    row.update(status="disabled", enabled=False, disabled_reason=reason, disabled_detail=detail)
    row.update(extra)


@dataclass
class CommandTable:
    """The rows of one session (or workspace), and the names that run them."""

    rows: list[dict[str, Any]]
    index: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: A bare name more than one source offers, and the qualified names that run each.
    ambiguous: dict[str, list[str]] = field(default_factory=dict)

    def lookup(self, typed: str) -> dict[str, Any] | None:
        return self.index.get(_slash(typed))

    def wire(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in row.items() if k not in _INTERNAL} for row in self.rows]


def place(rows: Iterable[dict[str, Any]]) -> CommandTable:
    """Decide reserved names, shadowing and ambiguity, and index the names that run."""

    rows = list(rows)
    builtins = [row for row in rows if row["kind"] == "builtin"]
    reserved = {name for row in builtins for name in (row["name"], *row["aliases"])}
    winners: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["kind"] == "builtin":
            continue
        if row["name"] in reserved:
            _disable(row, "command_name_reserved", f"{row['name']} is a built-in command")
            continue
        if row["kind"] != "command":
            continue
        # One owner per file source (the files in precedence order) or per plugin.
        owner = row["plugin"] or "files"
        winner = winners.get(f"{owner}{row['name']}")
        if winner is not None:
            _disable(
                row,
                "command_shadowed",
                f"shadowed by {winner['path']}",
                shadowed_by=winner["path"],
            )
            continue
        winners[f"{owner}{row['name']}"] = row
    table = CommandTable(rows=rows)
    live = [row for row in rows if row["disabled_reason"] not in _PLACED]
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in live:
        if row["kind"] != "builtin":
            by_name.setdefault(row["name"], []).append(row)
    for name, owners in by_name.items():
        if len(owners) > 1:
            table.ambiguous[name] = [row["qualified_id"] for row in owners]
            for row in owners:
                row["id"] = row["qualified_id"]
                row["ambiguous"] = True
    for row in live:
        table.index.setdefault(row["qualified_id"], row)
        if row["name"] not in table.ambiguous:
            table.index.setdefault(row["name"], row)
        for alias in row["aliases"]:
            table.index.setdefault(alias, row)
    return table


_PLACED = ("command_name_reserved", "command_shadowed")


def command_table(
    app: "FastAPI",
    session_id: str = "",
    workspace_id: str = "",
    *,
    home: Path | None = None,
) -> CommandTable:
    """Every command ``session_id`` can run (or, without a session, the workspace's files)."""

    cwd = command_cwd_for_request(app, session_id, workspace_id)
    plugin_rows: list[Mapping[str, Any]] = []
    if session_id:
        try:
            plugin_rows = list(session_resolution(app, session_id).commands)
        except SessionAgentUnavailable:
            plugin_rows = []
    rows = _builtin_rows(plugin_rows)
    # Without a workspace the server's own folder is the workspace, as for its tools.
    rows += _file_command_rows(home or Path.home(), cwd or Path.cwd())
    rows += _plugin_command_rows(plugin_rows)
    if session_id:
        from clio_agent.gact.mcp_prompt_commands import prompt_rows  # noqa: PLC0415 - owner
        from clio_agent.gact.skill_commands import skill_rows  # noqa: PLC0415 - owner module

        rows += skill_rows(app, session_id)
        rows += prompt_rows(app, session_id)
    return place(rows)


# ---- arguments ------------------------------------------------------------

_POSITIONAL = re.compile(r"\$(\d+)")


def split_words(text: str) -> list[str]:
    """The input's words, quoted words kept whole (as Claude Code splits ``$1``, ``$2``)."""

    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def render_command(template: str, user_input: str) -> str:
    """``template`` with ``$ARGUMENTS`` and the positional ``$1``, ``$2`` ... filled in."""

    if not template.strip():
        return user_input
    words = split_words(user_input)
    rendered = template.replace("$ARGUMENTS", user_input)

    def word(match: re.Match[str]) -> str:
        index = int(match.group(1)) - 1
        return words[index] if 0 <= index < len(words) else ""

    return _POSITIONAL.sub(word, rendered)


def required_arguments(row: Mapping[str, Any]) -> list[str]:
    """The names of a row's declared arguments marked ``required``."""

    names = []
    for spec in row.get("arguments") or []:
        if isinstance(spec, Mapping) and spec.get("required") in (True, "true", "yes"):
            name = str(spec.get("name") or spec.get("id") or "").strip()
            if name:
                names.append(name)
    return names


__all__ = [
    "CommandTable",
    "command_cwd_for_request",
    "command_table",
    "file_roots",
    "file_row",
    "place",
    "render_command",
    "required_arguments",
    "split_words",
]
