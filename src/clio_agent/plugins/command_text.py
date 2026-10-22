"""Expand a plugin's command or skill text the way its authoring client does.

Claude Code commands and skills name files of their own plugin through
``${CLAUDE_PLUGIN_ROOT}`` and pull a file's text in with an ``@<path>`` line.
Read raw, the model would see a variable it cannot resolve and a path it was
never shown. Here the variable becomes the installed plugin folder and an
``@`` line naming a file inside that folder becomes the file's text, so a
command reaches the model as its author wrote it for. An ``@`` line that names
something outside the plugin, or a file that is missing or too large, stays as
it is with a note saying why: never a silent drop.
"""

from __future__ import annotations

import re
from pathlib import Path

from clio_agent.plugins.manifest import within

ROOT_VARIABLES = ("${CLAUDE_PLUGIN_ROOT}", "${PLUGIN_ROOT}")
#: The largest file an ``@`` line inlines; a larger one is left for the model to read.
MAX_INCLUDE_BYTES = 64 * 1024
_INCLUDE = re.compile(r"^@(\S+)\s*$")


def _include(line: str, root: Path) -> str:
    match = _INCLUDE.match(line.strip())
    if match is None:
        return line
    target = Path(match.group(1))
    target = target if target.is_absolute() else root / target
    if not within(root, target):
        return f"{line}\n(not inlined: outside the plugin folder)"
    if not target.is_file():
        return f"{line}\n(not inlined: no such file in the plugin)"
    if target.stat().st_size > MAX_INCLUDE_BYTES:
        return f"{line}\n(not inlined: larger than {MAX_INCLUDE_BYTES // 1024} KiB; read the file)"
    text = target.read_text(encoding="utf-8", errors="replace").rstrip()
    return f'<file path="{target}">\n{text}\n</file>'


def expand_plugin_text(text: str, root: Path) -> str:
    """``text`` with the plugin-root variables resolved and its ``@`` file lines inlined."""

    root = root.resolve()
    for variable in ROOT_VARIABLES:
        text = text.replace(variable, str(root))
    return "\n".join(_include(line, root) for line in text.split("\n"))


__all__ = ["MAX_INCLUDE_BYTES", "ROOT_VARIABLES", "expand_plugin_text"]
