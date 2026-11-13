"""Build the agents' clio-kit tool server environments during an install.

Split from :mod:`clio_agent.runtime.document_install`, whose ``--prepare-tool-servers``
step runs :func:`_prepare_tool_servers` beside the other package downloads.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
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
_SERVER_PYTHON_MAX_WAIT_S = 300.0
_SERVER_PYTHON_PIN = ".python-version"


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


def _catalogue_python_request(catalogue: dict[str, Any]) -> str | None:
    """The Python request every clio-kit Python server pins, if they all pin the same one.

    The catalogue records each server's files by digest, not content, so the shared
    ``.python-version`` is matched against the minor-version requests it can hold. A
    server without one, differing pins or any other request returns ``None``.
    """
    pins: set[tuple[str, int]] = set()
    for server in dict(catalogue.get("servers") or {}).values():
        if server.get("runtime", "python") != "python":
            continue
        artifact = dict(catalogue.get("artifacts") or {}).get(server.get("artifact"))
        pin = dict((artifact or {}).get("files") or {}).get(_SERVER_PYTHON_PIN)
        if not pin:
            return None
        pins.add((str(pin.get("sha256", "")), int(pin.get("size", -1))))
    if len(pins) != 1:
        return None
    ((digest, size),) = pins
    for minor in range(100):
        request = f"3.{minor}"
        for content in (request, f"{request}\n", f"{request}\r\n"):
            data = content.encode("ascii")
            if len(data) == size and hashlib.sha256(data).hexdigest() == digest:
                return request
    return None


def _prefetch_server_python(
    args: argparse.Namespace, emit: Callable[[str], None]
) -> dict[str, Any]:
    """Install the Python the tool servers pin while the marketplace that declares them installs.

    Each server's build (``uv venv`` in clio-kit) otherwise downloads and unpacks that
    Python itself once the marketplace is ready: about 3,400 files on the critical path.
    The same ``uv`` installs the same request into the same place, with the servers'
    environment, so the builds find it. It never installs a launcher or a registry
    entry, which a build's own download does not add either. Anything that could make
    the builds choose differently skips it, and a failure leaves them as before.
    """
    if not args.prepare_tool_servers:
        return {"status": "skipped", "reason": "not_requested"}
    if args.reuse_installed:
        return {"status": "skipped", "reason": "reuse_installed"}
    started = time.monotonic()
    from clio_agent.tools.desktop_mcp_runtime import bundled_module_launcher  # noqa: PLC0415

    # Only the bundled clio-kit is known before the installer's launcher is ready.
    launcher = bundled_module_launcher("clio-kit", ["mcp-server", "server"])
    found = importlib.util.find_spec("clio_kit")
    if launcher is None or found is None or not found.submodule_search_locations:
        return {"status": "skipped", "reason": "no_bundled_kit"}
    try:
        index = Path(next(iter(found.submodule_search_locations))) / "_components.json"
        request = _catalogue_python_request(json.loads(index.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, AttributeError):
        request = None
    if request is None:
        return {"status": "skipped", "reason": "no_shared_python_pin"}
    from clio_agent.tools.mcp_environment import stdio_environment  # noqa: PLC0415

    launcher_env = launcher[2]
    # As ``mcp_prepare.provision`` builds a declared clio-kit server's environment.
    env = stdio_environment({}, command="clio-kit", args=["mcp-server", "server"])
    env.update(launcher_env)
    # The builds' own uv settings (clio-kit's locked server runtime).
    env["UV_CACHE_DIR"] = str(Path(env["CLIO_KIT_CACHE_DIR"]).expanduser().resolve() / "uv-cache")
    env.pop("VIRTUAL_ENV", None)
    if (
        env.get("UV_MANAGED_PYTHON", "").lower() not in {"1", "true"}
        or env.get("UV_PYTHON")
        or env.get("UV_NO_MANAGED_PYTHON")
        or env.get("UV_PYTHON_PREFERENCE")
        or env.get("UV_PYTHON_DOWNLOADS", "automatic").lower() != "automatic"
    ):
        return {"status": "skipped", "reason": "python_choice_overridden"}
    uv = shutil.which("uv", path=env.get("PATH"))
    if not uv:
        return {"status": "skipped", "reason": "uv_missing"}
    emit(f"Installing Python {request} for the agents' data tools...")
    command = [uv, "python", "install", "--no-bin", "--no-registry", request]
    try:
        completed = subprocess.run(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_SERVER_PYTHON_MAX_WAIT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "status": "failed",
            "request": request,
            "error": str(error)[-300:],
            "seconds": round(time.monotonic() - started, 1),
        }
    seconds = round(time.monotonic() - started, 1)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        return {"status": "failed", "request": request, "error": detail[-300:], "seconds": seconds}
    emit(f"Python {request} for the agents' data tools is ready.")
    return {"status": "ready", "request": request, "seconds": seconds}


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
