"""The command files a plugin ships, and the rules ``plugin check`` holds them to.

A plugin's ``commands/`` folder is read recursively, as Claude Code reads it: a
file's name is its stem (or its ``name`` field), and the subfolder it sits in is
a label shown beside it (``frontend`` for ``commands/frontend/component.md``).
The built-in commands are files of the core plugin naming a ``handler:``; their
names and aliases are reserved, so no other plugin may use them.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from clio_agent.plugins.frontmatter import FrontmatterError, split_frontmatter
from clio_agent.plugins.records import CommandRecord, Diagnostic

#: Where the plugins that ship inside clio-agent live.
BUILTIN_ROOT = Path(__file__).resolve().parent / "builtin"
#: The core plugin's command folder: the built-in commands and the reserved names.
CORE_COMMANDS = BUILTIN_ROOT / "core" / "ai.iowarp.clio" / "commands"


def _names(value: Any) -> tuple[str, ...]:
    values = value if isinstance(value, list) else [value] if value else []
    return tuple(str(item).strip().lstrip("/") for item in values if str(item).strip())


def command_name(meta: dict[str, Any], path: Path) -> str:
    """A command file's name: its ``name`` (or ``slash_id``) field, else its stem."""

    raw = meta.get("slash_id") or meta.get("slash-id") or meta.get("name") or ""
    return str(raw).strip().lstrip("/") or path.stem


def read_command_meta(path: Path) -> tuple[dict[str, Any], str, str]:
    """``(frontmatter, body, problem)`` of one command file; ``problem`` says why it is unreadable."""

    try:
        meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return {}, "", f"unreadable: {exc}"
    except FrontmatterError as exc:
        return {}, "", str(exc)
    return dict(meta or {}), body, ""


def command_files(base: Path) -> list[tuple[Path, str]]:
    """Every ``*.md`` under ``base`` with its subfolder label, in a stable order."""

    if not base.is_dir():
        return []
    found = []
    for path in sorted(base.rglob("*.md"), key=lambda p: p.relative_to(base).as_posix().lower()):
        if path.is_file():
            parent = path.parent.relative_to(base).as_posix()
            found.append((path, "" if parent == "." else parent))
    return found


def read_commands(base: Path, plugin: str) -> tuple[list[CommandRecord], list[Diagnostic]]:
    """The command records under ``base`` and a diagnostic for each file that cannot be read."""

    records: list[CommandRecord] = []
    problems: list[Diagnostic] = []
    for path, label in command_files(base):
        meta, _body, problem = read_command_meta(path)
        if problem:
            problems.append(
                Diagnostic("warning", "command_invalid", f"{path.name}: {problem}", plugin)
            )
        records.append(
            CommandRecord(
                id=command_name(meta, path),
                description=str(meta.get("description") or ""),
                path=str(path),
                label=label,
                handler=str(meta.get("handler") or "").strip(),
                aliases=_names(meta.get("aliases")),
            )
        )
    return records, problems


def builtin_command_names() -> frozenset[str]:
    """The core plugin's command names and aliases, which no other source may use."""

    records, _ = read_commands(CORE_COMMANDS, "core")
    return frozenset(name for record in records for name in (record.id, *record.aliases))


def command_diagnostics(
    plugin: str,
    commands: Iterable[CommandRecord],
    skills: Iterable[str],
    *,
    builtin: bool,
) -> list[Diagnostic]:
    """``plugin check`` rules for one plugin's commands and skills.

    These are check errors only: at run time the command table lists such a row as
    disabled with the same reason, so one bad file never stops a plugin loading.

    A ``handler:`` is for built-in plugins only; a core name may not be reused; a
    command and a skill of one plugin may not share a name; two files of one plugin
    may not share a name.
    """

    commands = list(commands)
    skills = set(skills)
    reserved = set() if builtin else builtin_command_names()
    found: list[Diagnostic] = []
    seen: dict[str, str] = {}
    for command in commands:
        where = Path(command.path).name
        if command.handler and not builtin:
            found.append(
                Diagnostic(
                    "error",
                    "command_handler_not_builtin",
                    f"{where} names handler {command.handler!r}; only built-in plugins may",
                    plugin,
                )
            )
        if command.id in reserved:
            found.append(
                Diagnostic(
                    "error",
                    "command_name_reserved",
                    f"{where}: /{command.id} is a built-in command",
                    plugin,
                )
            )
        if command.id in skills:
            found.append(
                Diagnostic(
                    "error",
                    "command_skill_clash",
                    f"{where}: /{command.id} is both a command and a skill of this plugin",
                    plugin,
                )
            )
        if command.id in seen:
            found.append(
                Diagnostic(
                    "error",
                    "command_duplicate",
                    f"{where}: /{command.id} is also {seen[command.id]}",
                    plugin,
                )
            )
        seen.setdefault(command.id, command.path)
    for skill in sorted(skills & reserved):
        found.append(
            Diagnostic(
                "error",
                "command_name_reserved",
                f"skill {skill}: /{skill} is a built-in command",
                plugin,
            )
        )
    return found


__all__ = [
    "BUILTIN_ROOT",
    "CORE_COMMANDS",
    "builtin_command_names",
    "command_diagnostics",
    "command_files",
    "command_name",
    "read_command_meta",
    "read_commands",
]
