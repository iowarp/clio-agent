"""Select bundled execution tools and expose prepared tools to workspace shells."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any

_ENVIRONMENTS: dict[Path, dict[str, str]] = {}
_LOCK = RLock()


def installed_github_cli() -> Path | None:
    """Resolve GitHub's installed binary only when a workspace needs its shell tools."""
    from clio_agent.runtime.github_cli import installed_github_cli as resolve

    return resolve()


def bundled_root() -> Path | None:
    """Find the desktop runtime independent of its installation directory."""
    configured = os.environ.get("GACT_BUNDLED_RUNTIME_DIR", "")
    candidates = [Path(configured)] if configured else []
    candidates.extend(Path(sys.executable).resolve().parents[:4])
    return next((root for root in candidates if (root / "runtime.json").is_file()), None)


def _wrapper(path: Path, argv: list[str]) -> None:
    if os.name == "nt":
        quoted = " ".join('"' + item.replace("%", "%%") + '"' for item in argv)
        path.with_suffix(".cmd").write_text(
            f"@echo off\r\nchcp 65001 >nul\r\n{quoted} %*\r\n", encoding="utf-8"
        )
    else:
        path.write_text(f'#!/bin/sh\nexec {shlex.join(argv)} "$@"\n', encoding="utf-8")
        path.chmod(0o755)


def publish_environment(workspace: Path, runtime: dict[str, Any], cache: Path) -> None:
    """Bind verified interpreter paths to future shell calls in this workspace."""
    from clio_agent.runtime.document_stack.process import scratch_root

    scratch = str(scratch_root(workspace))
    tools = cache / "shell-bin"
    tools.mkdir(exist_ok=True)
    _wrapper(tools / "python", runtime["python_argv"])
    _wrapper(tools / "python3", runtime["python_argv"])
    directories = [str(tools), str(Path(runtime["uv"]).parent)]
    javascript = runtime["javascript"]
    if javascript["status"] == "ready":
        _wrapper(tools / "pnpm", javascript["pnpm_argv"])
        directories.append(str(Path(runtime["node"]).parent))
    environment = {
        "PATH": os.pathsep.join([*directories, os.environ.get("PATH", "")]),
        "CLIO_EXECUTION_PATH": os.pathsep.join(directories),
        "CLIO_EXECUTION_PYTHON": runtime["python"],
        "CLIO_DOCUMENT_OFFICE_ROOT": runtime["office_root"],
        "CLIO_DOCUMENT_SCRATCH": scratch,
        "CLIO_DOCUMENT_SHORT_TEMP": tempfile.gettempdir(),
        # uv otherwise chooses an AppContainer-profile cache that the write
        # fence does not grant. Task dependencies belong to this workspace.
        "UV_CACHE_DIR": str(Path(scratch) / "uv-cache"),
        "TMPDIR": scratch,
        "TEMP": scratch,
        "TMP": scratch,
    }
    runtime["scratch_directory"] = scratch
    runtime["shell_environment"] = environment
    with _LOCK:
        _ENVIRONMENTS[workspace.resolve()] = environment


def shell_environment(workspace: Path, *, session_id: str | None = None) -> dict[str, str]:
    """Return only this process's verified workspace binding, never workspace input.

    ``TMPDIR``/``TEMP``/``TMP`` point at the session's own scratch folder (TD56) when a
    session owns the call (``session_id``, else the active one); ``UV_CACHE_DIR`` and
    ``CLIO_DOCUMENT_SCRATCH`` stay on the workspace scratch.
    """
    with _LOCK:
        environment = dict(_ENVIRONMENTS.get(workspace.resolve(), {}))
    environment.update(_session_temp(session_id))
    environment.update(_clio_tools(environment))
    environment.update(_without_command_line_tools_stubs(environment))
    path = environment.get("PATH", os.environ.get("PATH", ""))
    if shutil.which("gh", path=path):
        return environment
    executable = installed_github_cli()
    if executable is not None:
        directory = str(executable.parent)
        environment["PATH"] = os.pathsep.join([directory, path])
        prefix = environment.get("CLIO_EXECUTION_PATH", "")
        environment["CLIO_EXECUTION_PATH"] = os.pathsep.join(
            part for part in (directory, prefix) if part
        )
    return environment


#: The variables naming CLIO's own interpreter and uv in every shell and kernel.
CLIO_TOOL_VARIABLES = ("CLIO_PYTHON", "CLIO_UV", "UV_CACHE_DIR")


def _clio_tools(environment: dict[str, str]) -> dict[str, str]:
    """CLIO's own interpreter and uv, so a command can use them on a host with neither.

    ``CLIO_PYTHON`` is the interpreter ``run_code`` runs on (the prepared execution
    Python, else CLIO's own); ``CLIO_UV`` is CLIO's uv (bundled first, see
    ``clio_tool``). uv's cache stays CLIO's: the prepared workspace scratch, else the
    session's scratch folder, never the person's own uv cache.
    """
    from clio_agent.gact.infrastructure.native_service_platform import clio_tool  # noqa: PLC0415

    python = environment.get("CLIO_EXECUTION_PYTHON") or sys.executable
    tools = {"CLIO_PYTHON": python}
    uv = clio_tool("uv", python)
    if uv:
        tools["CLIO_UV"] = uv
    if "UV_CACHE_DIR" not in environment and environment.get("TMPDIR"):
        tools["UV_CACHE_DIR"] = str(Path(environment["TMPDIR"]) / "uv-cache")
    return tools


def _without_command_line_tools_stubs(environment: dict[str, str]) -> dict[str, str]:
    """On a Mac without the Command Line Tools, keep shells away from their stubs.

    ``python3`` there is ``/usr/bin/python3``, a stub that opens the "Install Command
    Line Developer Tools" dialog; it answers with CLIO's Python instead. uv patches
    each Python it downloads with ``install_name_tool``, another stub; the stand-in
    fails, so uv warns and keeps the interpreter unpatched.
    """
    from clio_agent.runtime.macos_clt import is_command_line_tools_stub  # noqa: PLC0415

    path = environment.get("PATH", os.environ.get("PATH", ""))
    found = shutil.which("python3", path=path)
    if found is None or not is_command_line_tools_stub(found):
        return {}
    from clio_agent import paths  # noqa: PLC0415

    python = environment.get("CLIO_PYTHON") or sys.executable
    digest = hashlib.sha256(python.encode("utf-8")).hexdigest()[:16]
    tools = paths.user_cache_dir() / "clt-free-bin" / digest
    if not (tools / "install_name_tool").is_file():
        tools.mkdir(parents=True, exist_ok=True)
        _wrapper(tools / "python3", [python])
        (tools / "install_name_tool").write_text(
            "#!/bin/sh\necho 'clio: install_name_tool needs the Command Line Tools;"
            " the library is left as it is' >&2\nexit 1\n",
            encoding="utf-8",
        )
        (tools / "install_name_tool").chmod(0o755)
    prefix = environment.get("CLIO_EXECUTION_PATH", "")
    return {
        "PATH": os.pathsep.join(item for item in (str(tools), path) if item),
        "CLIO_EXECUTION_PATH": os.pathsep.join(item for item in (str(tools), prefix) if item),
    }


def _session_temp(session_id: str | None) -> dict[str, str]:
    """The temp-dir variables for the session's scratch folder, empty off-session."""
    from clio_agent.runtime import session_scratch  # noqa: PLC0415

    sid = session_scratch.active_session() if session_id is None else session_id
    folder = session_scratch.session_scratch_dir(sid) if sid else None
    if folder is None:
        return {}
    return {key: str(folder) for key in ("TMPDIR", "TEMP", "TMP")}


def posix_command(command: str, environment: dict[str, str]) -> str:
    """Restore managed tools after login profiles initialize the host's PATH."""
    prefix = environment.get("CLIO_EXECUTION_PATH", "")
    return f'export PATH={shlex.quote(prefix)}:"$PATH"\n{command}' if prefix else command
