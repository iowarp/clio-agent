"""Write the ``clio-eval`` marketplace: plugins that launch the synthetic servers.

Layout (an Agent Plugins marketplace, ``docs/design/sdk/plugins-and-composition.md``)::

    marketplace/ai.iowarp.clio/marketplace.json
    marketplace/plugins/eval-home/{plugin.json, mcp.json, server/*.py}
    marketplace/plugins/eval-records-20/...      (and -200, -2000)

``eval-home`` declares ``synthetic-home`` plus the two distractor servers;
each ``eval-records-N`` declares one ``records`` server over N records. Server
names use ``[a-z0-9-]`` because CLIO derives a tool's namespace by splitting
its name on the first ``_``. The plugins add no agent: a session runs the
standard agent with a plugin delta (``tasks.py``).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from scripts.eval.tool_calling.fixtures.common import write_text
from scripts.eval.tool_calling.paths import MARKETPLACE_NAME

PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
SERVERS_SOURCE = Path(__file__).resolve().parents[1] / "servers"
SERVER_FILES = (
    "toolkit.py",
    "home_catalog.py",
    "home.py",
    "distractors.py",
    "records.py",
    "run.py",
)
RECORD_SIZES = (20, 200, 2000)
#: Exported to the CLIO server to collect every synthetic call in one file.
CALL_LOG_ENV = "CLIO_EVAL_CALL_LOG"


def _json(path: Path, data: Any) -> None:
    write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def _server(name: str, python: str, env: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "stdio",
        "command": python,
        "args": ["${PLUGIN_ROOT}/server/run.py", name],
        "env": {"EVAL_CALL_LOG": f"${{{CALL_LOG_ENV}:-${{PLUGIN_DATA}}/calls.jsonl}}", **env},
    }


def _plugin(root: Path, name: str, description: str, servers: dict[str, Any]) -> None:
    folder = root / "plugins" / name
    _json(
        folder / "plugin.json",
        {
            "$schema": PLUGIN_SCHEMA,
            "name": name,
            "version": "1.0.0",
            "description": description,
            "author": {"name": "CLIO evaluation harness"},
            "license": "BSD-3-Clause",
            "keywords": ["evaluation", "synthetic"],
        },
    )
    _json(folder / "mcp.json", {"$schema": MCP_SCHEMA, "mcpServers": servers})
    for file in SERVER_FILES:
        target = folder / "server" / file
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SERVERS_SOURCE / file, target)


def write_marketplace(root: Path, *, seed: int, python: str = "python") -> dict[str, Any]:
    """Write the marketplace under ``root``; return what the manifest records."""
    if "/" in python or " " in python:
        raise ValueError("python must be a bare command name (plugin rule)")
    _json(
        root / "ai.iowarp.clio" / "marketplace.json",
        {
            "name": MARKETPLACE_NAME,
            "description": "Synthetic MCP servers for the tool-calling evaluation.",
        },
    )
    seed_env = {"EVAL_SEED": str(seed)}
    _plugin(
        root,
        "eval-home",
        "A simulated smart home (about 90 tools) with a calendar and a parts inventory "
        "beside it, for tool-disclosure evaluation.",
        {
            name: _server(name, python, seed_env)
            for name in ("synthetic-home", "synthetic-calendar", "synthetic-inventory")
        },
    )
    plugins = {"eval-home": ["synthetic-home", "synthetic-calendar", "synthetic-inventory"]}
    for size in RECORD_SIZES:
        name = f"eval-records-{size}"
        _plugin(
            root,
            name,
            f"Stored station records ({size}), readable only through list_records and get_record.",
            {"records": _server("records", python, {**seed_env, "EVAL_RECORDS_COUNT": str(size)})},
        )
        plugins[name] = ["records"]
    return {"name": MARKETPLACE_NAME, "plugins": plugins, "python": python, "seed": seed}
