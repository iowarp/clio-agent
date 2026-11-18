"""Connectors derived from the clio-kit CLIO pins (LZ-D7 to LZ-D9).

Nothing is built at install any more (LZ-D1), so every MCP server of the pinned
clio-kit must be findable and enableable like a catalog connector. Hand-copying them
into ``catalogs/connectors/*.yaml`` would drift from the launcher, so they are derived
from the one source of truth: the catalogue the pinned clio-kit ships
(``clio_kit/_components.json``). Each server becomes an entry whose id is the server
name, whose description is its package's own, whose command is
``clio-kit mcp-server <name>``, and whose group is the clio-kit workflow package that
lists it (``catalogs/connectors/clio-kit/derive.yaml`` holds the readable names,
group labels and logos the catalogue cannot say). A ``logos`` value is one SVG beside
``derive.yaml``, or ``{light, dark}`` for a logo with a dark-theme variant; it is
validated and served like a hand-written entry's ``logo`` and ``logo_dark`` (LG-D2).

A server listed under ``withheld`` in ``derive.yaml`` is not offered as a connector:
CLIO reaches that service another way, and the entry gives the reason. Each one left
out is traced (:data:`KIT_SERVER_WITHHELD`), so the omission is never silent.

A hand-written entry with the same id wins (it has the logo and the needs); it only
takes the group from here. Each entry whose server is a clio-kit server records that
server's name as ``kit``, which the agent-load card uses to say a download comes.

A plugin whose one MCP server is a clio-kit server of a known name *follows* that
server's connector (:func:`kit_follow`, LZ-D8): it is shown once with the connector
(PL-D1), and its server is mounted only while the connector is on
(:mod:`.server_gate`). Its skills stay either way.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

DERIVE_DIR = "clio-kit"
DERIVE_FILE = "derive.yaml"
LAUNCHER = "clio-kit"
#: Traced for each clio-kit server ``derive.yaml`` withholds as a connector.
KIT_SERVER_WITHHELD = "kit_server_withheld"
_DESCRIPTION_MAX = 300
#: The group of an entry no workflow package lists.
OTHER_GROUP = ""


def components_path() -> Path | None:
    """The pinned clio-kit's shipped catalogue, or ``None`` when clio-kit is absent."""

    try:
        found = importlib.util.find_spec("clio_kit")
    except (ImportError, ValueError):
        return None
    if found is None or not found.submodule_search_locations:
        return None
    path = Path(next(iter(found.submodule_search_locations))) / "_components.json"
    return path if path.is_file() else None


@lru_cache(maxsize=4)
def _components(path: str) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def components() -> dict[str, Any]:
    path = components_path()
    return _components(str(path)) if path is not None else {}


def server_names() -> frozenset[str]:
    """Every server the pinned clio-kit ships."""

    return frozenset(str(name) for name in (components().get("servers") or {}))


def kit_server(spec: Mapping[str, Any]) -> str:
    """The clio-kit server ``spec`` runs (``clio-kit mcp-server <name>``), else ""."""

    if not isinstance(spec, Mapping) or spec.get("type") == "clio-connection":
        return ""
    command = str(spec.get("command") or "")
    name = Path(command.replace("\\", "/")).name.lower().removesuffix(".exe")
    args = [str(arg) for arg in spec.get("args") or ()]
    if name != LAUNCHER or len(args) < 2 or args[0] != "mcp-server" or args[1].startswith("-"):
        return ""
    return args[1].lower()


def kit_follow(mcp_servers: Mapping[str, Mapping[str, Any]]) -> str:
    """The connector a plugin follows because its one server is a known clio-kit server."""

    if len(mcp_servers) != 1:
        return ""
    (spec,) = mcp_servers.values()
    server = kit_server(spec)
    return server if server and server in server_names() else ""


def _derive_data(folder: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load((folder / DERIVE_DIR / DERIVE_FILE).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _words(server: str) -> str:
    text = server.replace("-", " ").replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def _groups(data: Mapping[str, Any], packages: Mapping[str, Any]) -> dict[str, str]:
    """Server name -> group label, from the workflow packages' dependencies."""

    out: dict[str, str] = {}
    for package, label in dict(data.get("groups") or {}).items():
        manifest = dict((packages.get(package) or {}).get("manifest") or {})
        for dependency in manifest.get("dependencies") or ():
            for server in (packages.get(dependency) or {}).get("servers", {}).values():
                name = kit_server(server)
                if name:
                    out.setdefault(name, str(label))
    return out


def _requires(record: Any) -> str:
    """The catalogue's prerequisite note for a server: what must be on the machine."""

    if not isinstance(record, Mapping):
        return ""
    prerequisites = record.get("prerequisites")
    note = prerequisites.get("note") if isinstance(prerequisites, Mapping) else None
    return " ".join(str(note).split())[:_DESCRIPTION_MAX] if note else ""


def _description(packages: Mapping[str, Any], server: str) -> str:
    for package in packages.values():
        servers = dict(package.get("servers") or {})
        if any(kit_server(spec) == server for spec in servers.values()):
            text = str((package.get("manifest") or {}).get("description") or "").strip()
            if text:
                return text[:_DESCRIPTION_MAX]
    return f"The {server} tools from clio-kit"


def _logo_fields(value: Any) -> dict[str, str]:
    """``logo`` (and ``logo_dark``) from one ``logos`` value of ``derive.yaml``."""

    if isinstance(value, Mapping):
        pairs = (("logo", value.get("light")), ("logo_dark", value.get("dark")))
        return {key: str(name) for key, name in pairs if name}  # validated as an entry
    return {"logo": str(value)} if value else {}


def derived_raw(folder: Path) -> list[dict[str, Any]]:
    """The derived entries as raw data (validated like any entry), in server order."""

    data = _derive_data(folder)
    catalogue = components()
    packages = dict(catalogue.get("packages") or {})
    names = dict(data.get("names") or {})
    logos = dict(data.get("logos") or {})
    groups = _groups(data, packages)
    servers = dict(catalogue.get("servers") or {})
    withheld = dict(data.get("withheld") or {})
    rows = []
    for server in sorted(servers):
        if server in withheld:
            from clio_agent.runtime import trace  # noqa: PLC0415

            trace.event(
                "CONNECTORS",
                "%s server=%s reason=%s",
                KIT_SERVER_WITHHELD,
                server,
                withheld[server],
            )
            continue
        row = {
            "id": server,
            "name": str(names.get(server) or _words(server)),
            "description": _description(packages, server),
            "server": {"command": LAUNCHER, "args": ["mcp-server", server]},
            "group": groups.get(server, OTHER_GROUP),
            **_logo_fields(logos.get(server)),
        }
        requires = _requires(servers[server])
        if requires:
            row["requires"] = requires
        rows.append(row)
    return rows


def stamp() -> tuple[Any, ...]:
    """What the derived entries depend on beyond the catalog folders."""

    path = components_path()
    if path is None:
        return ()
    try:
        return (str(path), path.stat().st_mtime_ns)
    except OSError:
        return ()
