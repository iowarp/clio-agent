"""Desktop-only stdio MCP launch adaptations."""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path


def desktop_mcp_log_file(namespace: str) -> Path | None:
    """Return an inheritable stderr sink for managed desktop MCP children."""

    if os.environ.get("CLIO_DESKTOP_BOOT_HEARTBEAT") != "1":
        return None

    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

    log_dir = paths.user_cache_dir() / "mcp-stdio"
    log_dir.mkdir(parents=True, exist_ok=True)
    safe_namespace = re.sub(r"[^A-Za-z0-9_.-]+", "-", namespace).strip("-.") or "server"
    return log_dir / f"{safe_namespace}.log"


def resolve_bundled_runtime_root() -> Path | None:
    """Resolve the desktop bundle's runtime root (the dir holding ``runtime.json``), if any.

    Walks up from ``sys.executable`` looking for the marker file the desktop installer writes
    beside the relocatable python interpreter + its bundled ``bin``/``python`` trees. Returns
    ``None`` outside the bundled desktop runtime (e.g. a dev checkout's venv, or a plain ``pip``
    install) -- callers must treat that as "not bundled", never raise. Shared by
    :func:`bundled_module_launcher` (the clio-kit MCP launcher) and
    :func:`clio_agent.runtime.sandbox_codex.detect_codex` (the bundled ``codex.exe`` lookup) so
    the ancestor walk exists in exactly one place.
    """

    executable = Path(sys.executable).resolve()
    return next(
        (parent for parent in executable.parents if (parent / "runtime.json").is_file()),
        None,
    )


def bundled_module_launcher(
    command: str, args: Sequence[str]
) -> tuple[str, list[str], dict[str, str]] | None:
    """Resolve bundled clio-kit through the relocatable runtime interpreter."""

    modules = {
        "clio-kit": ("clio_kit", "from clio_kit import cli; cli()"),
        # The desktop runtime pre-installs the Web Search MCP adapter during
        # packaging. Launch it directly: connecting must not download/build a
        # second Python environment on first use or depend on ambient `uvx`.
        "clio-web-search-mcp": ("web_mcp", "from web_mcp.server import main; main()"),
    }
    selected = modules.get(command)
    if selected is None or importlib.util.find_spec(selected[0]) is None:
        return None

    runtime_root = resolve_bundled_runtime_root()
    if runtime_root is None:
        return None

    env: dict[str, str] = {}
    bundled_bin = runtime_root / "bin"
    if bundled_bin.is_dir():
        ambient_path = os.environ.get("PATH", "")
        env["PATH"] = os.pathsep.join(part for part in (str(bundled_bin), ambient_path) if part)
    env["CLIO_KIT_CACHE_DIR"] = os.environ.get(
        "CLIO_KIT_CACHE_DIR", str(Path.home() / ".clio" / "mcp-runtime")
    )
    executable = Path(sys.executable).resolve()
    return str(executable), ["-c", selected[1], *args], env
