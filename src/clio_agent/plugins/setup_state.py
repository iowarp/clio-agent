"""Whether the person has set up an installed plugin, read from the plugin's own state.

A plugin declares in its ``ai.iowarp.clio`` extension the files under its ``PLUGIN_DATA``
folder that hold what the person configured through it (``setup.files``), such as the
endpoints they added on its page. The plugin is set up when any of those files holds a
non-empty value: a JSON or YAML file that parses to a non-empty list or scalar, or to a
mapping with at least one non-empty value, or any other file with text in it. A missing,
empty or unreadable file, or one that parses to nothing (``[]``, ``{}``, ``null``, or a
mapping whose values are all empty such as ``sites: {}``), does not count.

A plugin the person has set up joins new conversations by default
(:mod:`clio_agent.gact.plugin_join`). Nothing here names a particular plugin.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

#: Larger files are read as set up without being parsed (a person's state, not a dump).
_PARSE_LIMIT = 1 << 20


def setup_files(record: Any) -> list[str]:
    """The ``setup.files`` ``record`` declares (``[]`` for none)."""

    setup = (getattr(record, "host", None) or {}).get("setup") or {}
    return [str(path) for path in setup.get("files") or ()]


def _holds_value(path: Path) -> bool:
    try:
        size = path.stat().st_size
        if size == 0:
            return False
        if size > _PARSE_LIMIT:
            return True
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if not text.strip():
        return False
    suffix = path.suffix.casefold()
    try:
        if suffix == ".json":
            value: Any = json.loads(text)
        elif suffix in (".yaml", ".yml"):
            value = yaml.safe_load(text)
        else:
            return True
    except (ValueError, yaml.YAMLError):
        return False
    if isinstance(value, dict):
        return any(not _empty(item) for item in value.values())
    return not _empty(value)


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, (str, list, dict)) and not value)


def is_set_up(record: Any, data_dir: Path | None = None) -> bool:
    """Whether a declared state file of ``record`` holds a value (see the module).

    ``data_dir`` is the plugin's ``PLUGIN_DATA`` (its store folder when not given). A
    path that leaves it, through a link, never counts.
    """

    files = setup_files(record)
    if not files:
        return False
    if data_dir is None:
        from clio_agent.plugins.servers import plugin_data_dir  # noqa: PLC0415

        data_dir = plugin_data_dir(str(record.id))
    root = data_dir.resolve()
    for relative in files:
        path = (root / relative).resolve()
        if path.is_relative_to(root) and path.is_file() and _holds_value(path):
            return True
    return False


__all__ = ["is_set_up", "setup_files"]
