"""The last instructions each installed plugin's MCP servers sent, kept across restarts.

A server's ``instructions`` say what it reaches (its sites, datasets, packages), and
those words are how the agent finds a plugin it does not have in a conversation: a
request naming one of them should lead to an offer of that plugin. A plugin's servers
run only while something uses them, so every live text a plugin server sends is kept
here by plugin id (:func:`remember`, called from
:func:`clio_agent.tools.mcp_server_instructions.record_instructions`) and read when no
server of the plugin runs (:func:`plugin_instructions`). The file is
``<plugin store>/server_instructions.json``. Nothing here names a particular plugin.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from clio_agent.runtime import trace

_BASENAME = "server_instructions.json"
_SCHEMA = 1
_LOCK = threading.Lock()
#: ``(path, {plugin: {server: text}})`` as last read or written by this process.
_LOADED: tuple[Path, dict[str, dict[str, str]]] | None = None


def _path() -> Path:
    from clio_agent.plugins.service import plugin_store_root  # noqa: PLC0415

    return plugin_store_root() / _BASENAME


def _read(path: Path) -> dict[str, dict[str, str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        trace.event("TOOLS", "plugin_instructions_unreadable (%s)", type(exc).__name__)
        return {}
    plugins = raw.get("plugins") if isinstance(raw, dict) and raw.get("schema") == _SCHEMA else {}
    out: dict[str, dict[str, str]] = {}
    for plugin, servers in (plugins or {}).items():
        if isinstance(servers, dict):
            texts = {
                str(name): str(row.get("text") or "")
                for name, row in servers.items()
                if isinstance(row, dict) and row.get("text")
            }
            if texts:
                out[str(plugin)] = texts
    return out


def _current() -> dict[str, dict[str, str]]:
    """The kept texts (under :data:`_LOCK`)."""

    global _LOADED
    path = _path()
    if _LOADED is None or _LOADED[0] != path:
        _LOADED = (path, _read(path))
    return _LOADED[1]


def _write(path: Path, texts: dict[str, dict[str, str]]) -> None:
    now = time.time()
    document: dict[str, Any] = {
        "schema": _SCHEMA,
        "plugins": {
            plugin: {name: {"text": text, "recorded_at": now} for name, text in servers.items()}
            for plugin, servers in sorted(texts.items())
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(document, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def remember(plugin: str, server: str, text: str) -> None:
    """Keep ``plugin``'s server ``server``'s live instructions (none forgets them).

    Writes only when the text changed. Never raises: a store that cannot be written
    keeps the text for this process and traces why.
    """

    text = text.strip()
    with _LOCK:
        texts = _current()
        if texts.get(plugin, {}).get(server, "") == text:
            return
        if text:
            texts.setdefault(plugin, {})[server] = text
        else:
            texts.get(plugin, {}).pop(server, None)
            if not texts.get(plugin):
                texts.pop(plugin, None)
        try:
            _write(_path(), texts)
        except OSError as exc:
            trace.event(
                "TOOLS", "plugin_instructions_unsaved plugin=%s (%s)", plugin, type(exc).__name__
            )


def plugin_instructions(plugin: str) -> str:
    """The last instructions ``plugin``'s servers sent, each server's on its own lines."""

    with _LOCK:
        servers = dict(_current().get(plugin) or {})
    return "\n".join(servers[name] for name in sorted(servers))


def forget_loaded() -> None:
    """Read the file again at next use (tests, a changed store root)."""

    global _LOADED
    with _LOCK:
        _LOADED = None


__all__ = ["forget_loaded", "plugin_instructions", "remember"]
