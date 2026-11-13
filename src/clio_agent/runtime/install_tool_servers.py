"""Build the agents' clio-kit tool server environments during an install.

Split from :mod:`clio_agent.runtime.document_install`, whose ``--prepare-tool-servers``
step runs :func:`_prepare_tool_servers` beside the other package downloads.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

#: The file the Windows installer writes once its clio-kit launcher is installed:
#: ``{"status": "ready" | "failed", "path": [folders to put first on PATH], "env": {...}}``.
KIT_READY_ENV = "CLIO_INSTALL_KIT_READY"
_KIT_READY_MAX_WAIT_S = 600.0
_KIT_READY_POLL_S = 0.25


def _wait_for_installer_kit() -> dict[str, Any] | None:
    """Wait for the installer's clio-kit when it names a signal file, then adopt its PATH.

    Returns ``None`` when the tool servers can be prepared, else the typed outcome.
    """
    signal = os.environ.get(KIT_READY_ENV, "")
    if not signal:
        return None
    deadline = time.monotonic() + _KIT_READY_MAX_WAIT_S
    while True:
        try:
            ready = json.loads(Path(signal).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ready = None
        if isinstance(ready, dict) and ready.get("status"):
            break
        if time.monotonic() >= deadline:
            return {"status": "failed", "reason": "tool_servers_kit_wait_timeout"}
        time.sleep(_KIT_READY_POLL_S)
    if ready["status"] != "ready":
        return {"status": "skipped", "reason": "tool_servers_kit_unavailable"}
    for name, value in dict(ready.get("env") or {}).items():
        if value:
            os.environ[str(name)] = str(value)
    first = [str(folder) for folder in ready.get("path") or [] if folder]
    if first:
        os.environ["PATH"] = os.pathsep.join([*first, os.environ.get("PATH", "")])
    return None


def _carried_kit_servers() -> dict[str, Any]:
    """The clio-kit tool servers declared by plugins that an installed agent carries.

    Only installed plugins are read: the built-ins declare no MCP servers, and loading
    them imports the agent engine (about 15 s of a cold Windows install).
    """
    from clio_agent.plugins.service import PluginService  # noqa: PLC0415
    from clio_agent.runtime.mcp_launcher import discover_declared_mcp_servers  # noqa: PLC0415
    from clio_agent.tools.launcher_cache_lock import _launcher_name  # noqa: PLC0415

    service = PluginService()
    carried = {selection.ref.id for entry in service.agents() for selection in entry.spec.plugins}
    installed = service.store.installed_records()
    names = {name for record in installed if record.id in carried for name in record.mcp_servers}
    return {
        name: spec
        for name, spec in discover_declared_mcp_servers(records=installed).items()
        if name in names
        and _launcher_name(str(spec.command)) == "clio-kit"
        and [str(arg) for arg in spec.args][:1] == ["mcp-server"]
    }


def _prepare_tool_servers(
    args: argparse.Namespace, marketplace: Future[dict[str, Any]], emit: Callable[[str], None]
) -> dict[str, Any]:
    """Build the agents' clio-kit tool server environments during an install, reported typed.

    Each one is otherwise built by the first turn that uses it: about a minute of
    downloads and thousands of files on a cold cache. They are built in parallel, with
    the same step and environment the server's start uses (``mcp_prepare.provision``),
    which then finds them complete. A failure does not fail the install; the first use
    retries it.
    """
    if not args.prepare_tool_servers:
        return {"status": "skipped", "reason": "not_requested"}
    if args.reuse_installed:
        return {"status": "skipped", "reason": "reuse_installed"}
    if marketplace.result().get("status") != "ready":
        return {"status": "skipped", "reason": "tool_servers_marketplace_unavailable"}
    started = time.monotonic()
    from clio_agent.tools.desktop_mcp_runtime import bundled_module_launcher  # noqa: PLC0415

    # A bundled runtime launches its own clio-kit with its own uv, as the server does,
    # so only an install without one waits for the installer's launcher.
    bundled = bundled_module_launcher("clio-kit", []) is not None
    waited = None if bundled else _wait_for_installer_kit()
    if waited is not None:
        return {**waited, "seconds": round(time.monotonic() - started, 1)}
    from clio_agent.tools import mcp_prepare  # noqa: PLC0415

    kit_seconds = round(time.monotonic() - started, 1)
    servers = _carried_kit_servers()
    if not servers:
        return {"status": "skipped", "reason": "no_tool_servers", "seconds": kit_seconds}
    emit(f"Preparing the agents' data tools: {', '.join(sorted(servers))}...")

    def build(name: str) -> dict[str, Any]:
        began = time.monotonic()
        try:
            outcome: dict[str, Any] = {"status": mcp_prepare.provision(name, servers[name])}
        except mcp_prepare.PrepareError as error:
            outcome = {"status": "failed", "reason": error.reason, "error": str(error)[-300:]}
        outcome["seconds"] = round(time.monotonic() - began, 1)
        return outcome

    with ThreadPoolExecutor(max_workers=len(servers), thread_name_prefix="clio-tools") as pool:
        results = dict(zip(servers, pool.map(build, servers), strict=True))
    failed = sorted(name for name, outcome in results.items() if outcome["status"] == "failed")
    if failed:
        emit(f"Some data tools were not prepared; their first use will retry: {', '.join(failed)}")
    else:
        emit("The agents' data tools are ready.")
    return {
        "status": "failed" if failed else "ready",
        "kit": "bundled" if bundled else "installer",
        "kit_wait_seconds": kit_seconds,
        "seconds": round(time.monotonic() - started, 1),
        "servers": results,
    }
