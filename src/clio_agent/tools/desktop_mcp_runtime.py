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


def bundled_module_launcher(
    command: str, args: Sequence[str]
) -> tuple[str, list[str], dict[str, str]] | None:
    """Resolve bundled clio-kit through the relocatable runtime interpreter."""

    if command != "clio-kit" or importlib.util.find_spec("clio_kit") is None:
        return None

    executable = Path(sys.executable).resolve()
    runtime_root = next(
        (parent for parent in executable.parents if (parent / "runtime.json").is_file()),
        None,
    )
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
    return str(executable), ["-c", "from clio_kit import cli; cli()", *args], env
