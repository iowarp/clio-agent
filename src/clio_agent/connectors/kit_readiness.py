"""Whether a clio-kit server's tools are downloaded already (LZ-D3).

The installer no longer builds the agents' clio-kit server environments (LZ-D1); a
server's first start downloads and builds its environment, which can take a minute.
The agent-load card says so before it happens: an item whose server is not prepared
reads "downloads its tools". This module answers the question from files only, never
by building or fetching:

* the cache root is the one CLIO's own launches give clio-kit
  (``CLIO_KIT_CACHE_DIR`` in :func:`clio_agent.tools.mcp_environment.stdio_environment`);
* the server's source is the component the pinned clio-kit catalogue names
  (``components/<sha256>/<server>``). Without it nothing was built;
* with it, the environment clio-kit would build is
  ``mcp-environments/<server>-<project sha256[:24]>``, ready when it holds a complete
  virtual environment (``pyvenv.cfg`` and its interpreter), as clio-kit's own
  ``environment_build.is_complete_environment`` checks.

A server whose runtime is not Python, or a catalogue this CLIO cannot read, answers
``None`` (unknown), with a typed trace; the card then says only that enabling turns it
on, never that nothing downloads.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from clio_agent.connectors import kit_entries
from clio_agent.runtime import trace


def cache_root() -> Path:
    """The clio-kit cache CLIO's launches of ``clio-kit mcp-server`` use."""

    from clio_agent.tools.mcp_environment import stdio_environment  # noqa: PLC0415

    env = stdio_environment({}, command=kit_entries.LAUNCHER, args=["mcp-server", "server"])
    configured = env.get("CLIO_KIT_CACHE_DIR") or os.environ.get("CLIO_KIT_CACHE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    base = env.get("XDG_CACHE_HOME") or os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache").expanduser().resolve() / "clio-kit"


@lru_cache(maxsize=64)
def _environment_name(component: str) -> str | None:
    """``<dir>-<project sha[:24]>`` for a server component folder (hashing its files)."""

    try:
        import clio_kit as kit  # noqa: PLC0415

        identity = kit.locked_server_project_identity(Path(component), "python")
    except Exception as exc:  # noqa: BLE001 - surfaced as the typed "unknown" answer
        trace.event("TOOLS", "kit_readiness_unknown component=%s error=%s", component, exc)
        return None
    return f"{Path(component).name}-{str(identity['project_sha256'])[:24]}"


def prepared(server: str) -> bool | None:
    """``True`` when ``server``'s environment is built, ``False`` when not, else ``None``."""

    catalogue = kit_entries.components()
    record = dict((catalogue.get("servers") or {}).get(server) or {})
    artifact = dict((catalogue.get("artifacts") or {}).get(str(record.get("artifact"))) or {})
    digest = str(artifact.get("sha256") or "")
    if not record or not digest:
        trace.event("TOOLS", "kit_readiness_unknown server=%s reason=not_in_catalogue", server)
        return None
    if str(record.get("runtime") or "python") != "python":
        trace.event("TOOLS", "kit_readiness_unknown server=%s reason=runtime", server)
        return None
    root = cache_root()
    name = str(record.get("artifact")).rsplit("/", 1)[-1]
    component = root / "components" / digest / name
    if not component.is_dir():
        return False
    environment_name = _environment_name(str(component))
    if environment_name is None:
        return None
    environment = root / "mcp-environments" / environment_name
    interpreter = (
        environment / "Scripts" / "python.exe"
        if os.name == "nt"
        else environment / "bin" / "python"
    )
    return (environment / "pyvenv.cfg").is_file() and interpreter.exists()
