"""The files a shell call's command line names, as candidates for its ``used`` edges.

The used-edge scan (:func:`~clio_agent.gact.artifacts.transform_edges.detect_used_edges`)
reads path strings from a call's arguments. A shell call has one argument, the command
line (``cd runs/x && python3 fit.py measurements.csv``), which is not itself a path, so
the analysis a study cites recorded neither its script nor the exported file it read.

:func:`command_path_args` splits the command with :mod:`shlex`, follows its ``cd``
prefixes, and returns the tokens that look like paths, made absolute against the
directory each one is used in. They join the scan as one extra argument, so the scan's
own rules still decide: a candidate becomes an edge only when it is an existing file in
the workspace that the call did not write. A command that does not split (an unbalanced
quote) yields no candidates and a logged reason, never a guess.
"""

from __future__ import annotations

import logging
import re
import shlex
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The extra argument the scan reads the candidates from.
COMMAND_PATHS_ARG = "command_paths"
#: Short names of the shell tools whose ``command`` argument is a command line.
SHELL_TOOL_NAMES = frozenset({"shell_bash", "bash"})
_OPERATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")"})
_ASSIGNMENT = re.compile(r"^(?:--)?[A-Za-z_][\w-]*=")
_MAX_CANDIDATES = 64
_MAX_TOKEN = 1024


def _short(tool_name: str) -> str:
    return tool_name.rsplit(".", 1)[-1].rsplit("/", 1)[-1]


def _path_like(token: str) -> bool:
    return "/" in token or bool(Path(token).suffix)


def command_path_args(tool_name: str, args: dict[str, Any], base: Path) -> dict[str, list[str]]:
    """``{COMMAND_PATHS_ARG: [absolute candidates]}`` for a shell call, else ``{}``.

    ``base`` is the directory the command starts in when ``args`` names no ``cwd``
    (the session's node folder, else the workspace root).
    """

    command = args.get("command") if isinstance(args, dict) else None
    if _short(tool_name) not in SHELL_TOOL_NAMES or not isinstance(command, str):
        return {}
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError as exc:
        logger.info("shell command paths skipped reason=command_not_split detail=%s", exc)
        return {}
    cwd = str(args.get("cwd") or "").strip()
    here = (base / cwd) if cwd else base
    found: list[str] = []
    segment_start = True
    after_cd = False
    for token in tokens:
        if token in _OPERATORS or set(token) <= set("&|;()"):
            segment_start, after_cd = True, False
            continue
        if after_cd:
            here = here / token
            after_cd = False
            continue
        if segment_start and token == "cd":
            after_cd, segment_start = True, False
            continue
        segment_start = False
        value = _ASSIGNMENT.sub("", token, count=1)
        if not value or value.startswith("-") or len(value) > _MAX_TOKEN or "\n" in value:
            continue
        if _path_like(value):
            found.append(str((here / value).resolve(strict=False)))
            if len(found) >= _MAX_CANDIDATES:
                break
    return {COMMAND_PATHS_ARG: list(dict.fromkeys(found))} if found else {}


def shell_scan_args(
    app: Any, sid: str, tool_name: str, args: dict[str, Any], workspace_id: str
) -> dict[str, Any]:
    """``args`` plus a shell call's command-line paths, resolved where its shell starts."""

    if _short(tool_name) not in SHELL_TOOL_NAMES:
        return args
    from clio_agent.gact.node_folders import session_node_folder  # noqa: PLC0415

    base = session_node_folder(app, sid)
    if not base:
        store = getattr(getattr(app, "state", None), "workspaces", None)
        workspace = store.get(workspace_id) if store is not None and workspace_id else None
        base = str(getattr(workspace, "root_path", "") or "")
    if not base:
        return args
    return {**args, **command_path_args(tool_name, args, Path(base).expanduser())}


__all__ = ["COMMAND_PATHS_ARG", "SHELL_TOOL_NAMES", "command_path_args", "shell_scan_args"]
