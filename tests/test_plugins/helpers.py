"""Build Agent Plugins packages and marketplaces on disk for tests."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

from clio_agent.plugins.manifest import MCP_SCHEMA, PLUGIN_SCHEMA

NATIVE = frozenset(
    {"shell_bash", "fs_read_file", "query_tasks", "wait_tasks", "ask_user", "view_pdf"}
)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def plugin(
    root: Path,
    name: str,
    *,
    version: str = "1.0.0",
    ext: dict[str, Any] | None = None,
    files: dict[str, str] | None = None,
    servers: dict[str, Any] | None = None,
    manifest_extra: dict[str, Any] | None = None,
) -> Path:
    base = root / name
    manifest: dict[str, Any] = {"$schema": PLUGIN_SCHEMA, "name": name, "version": version}
    if ext is not None:
        manifest["extensions"] = {"ai.iowarp.clio": ext}
    manifest.update(manifest_extra or {})
    write(base / "plugin.json", json.dumps(manifest))
    if servers is not None:
        write(base / "mcp.json", json.dumps({"$schema": MCP_SCHEMA, "mcpServers": servers}))
    for relative, text in (files or {}).items():
        write(base / relative, text)
    return base


def fragment(plugin_id: str, name: str, section: str, body: str, when: str = "") -> str:
    when_line = f"when: {when}\n" if when else ""
    return f"---\nid: {plugin_id}.{name}\nsection: {section}\n{when_line}---\n{body}\n"


def tools_module(*names: str) -> str:
    lines = ["from clio_agent.sdk import native_tool"]
    lines += [f"{n} = native_tool({n!r})" for n in names]
    return "\n".join(lines) + "\n"
