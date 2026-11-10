"""Prepare a declared stdio MCP server separately from starting it.

Starting a plugin's MCP server used to do everything at once, inside the server spawn:
``uv run --project <plugin>`` resolved and synced the plugin's environment into
``<plugin>/.venv`` (writing into the installed plugin folder), and only then started the
server -- all of it measured against the server's start-up liveness, and repeated by a
second spawn when the session later connected. This module splits that work:

* **Provision** (:func:`provision`): build the server's environment with a separate,
  short-lived command, before the server starts.

  - A ``uv run --project <dir> <script>`` launcher syncs with ``uv sync --project <dir>``
    into a CLIO-owned environment (:func:`project_environment_dir`, under CLIO's cache)
    named by ``UV_PROJECT_ENVIRONMENT``, which every later spawn of the server also gets
    (:func:`uv_project_environment`), so nothing is written into the plugin folder. A
    marker in that environment records the inputs it was built from; while they match,
    provisioning is a file read.
  - A ``clio-kit mcp-server <name>`` launcher is prepared with
    ``clio-kit mcp-server --prepare <name>`` when the installed clio-kit offers that option
    (it is detected from ``clio-kit mcp-server --help``). Where builds run outside the
    fence (Windows, :func:`builds_outside_fence`), an older clio-kit is prepared by running
    its own build in the launcher's interpreter with the server's start left out
    (:data:`_KIT_BUILD_SHIM`). Otherwise an older clio-kit builds the environment when the
    server starts, as before; the provision step then records the typed reason
    :data:`PROVISION_UNSUPPORTED_LAUNCHER` and moves on.
  - Any other launcher has nothing to provision (:data:`PROVISION_NOT_APPLICABLE`).

  On Windows the provisioning command runs outside the sandbox: the AppContainer denies
  the executable writes a ``uv`` build makes (#1991). The server still starts confined.

* **Start**: the server is then started once; its tools are listed over that same
  connection (``gact.mcp_readiness``), not by a second, listing-only spawn.

Every namespace's preparation has one visible state in :data:`_STATES`
(``preparing`` / ``ready`` / ``failed``) with the phase it is in and, for a failure, a
typed reason from :data:`PREPARE_REASONS`. The MCP status surfaces read it
(:func:`preparation_state`) and callers can wait on it (:func:`wait_for_namespaces`).
A failure is never remembered as terminal: the next preparation starts fresh.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from clio_agent.runtime import trace

logger = logging.getLogger(__name__)

PREPARING = "preparing"
READY = "ready"
FAILED = "failed"

#: The phases a preparation passes through (the same words the session's
#: ``infrastructure.dependency.changed`` events use).
PHASE_PROVISION = "provision"
PHASE_LAUNCH = "launch"  # start the server and list its tools over that connection
PHASE_CONNECT = "connect"  # connect a server whose tools are already listed

#: Provision outcomes that are not failures (each still recorded, never silent).
PROVISIONED = "provisioned"
PROVISION_CACHED = "provision_cached"
PROVISION_NOT_APPLICABLE = "provision_not_applicable"
PROVISION_UNSUPPORTED_LAUNCHER = "mcp_prepare_provision_unsupported_launcher"

#: Typed failure reasons of a preparation (closed set).
LAUNCHER_MISSING = "mcp_prepare_launcher_missing"
PROVISION_FAILED = "mcp_prepare_provision_failed"
PROVISION_NO_PROGRESS = "mcp_prepare_provision_no_progress"
START_FAILED = "mcp_prepare_start_failed"
START_NO_PROGRESS = "mcp_prepare_start_no_progress"
CACHE_LOCK_TIMEOUT = "mcp_prepare_cache_lock_timeout"
PROVISION_ACCESS_DENIED = "mcp_prepare_provision_access_denied"
PROVISION_PATH_TOO_LONG = "mcp_prepare_provision_path_too_long"

PREPARE_REASONS: dict[str, str] = {
    LAUNCHER_MISSING: "The server's launcher command is not installed or not on PATH.",
    PROVISION_FAILED: "Building the server's environment failed (see detail).",
    PROVISION_ACCESS_DENIED: (
        "Building the server's environment was denied writing its executables (Windows "
        "'Failed to update Windows PE resources ... Access is denied'), as inside the "
        "AppContainer sandbox or under antivirus quarantine (see detail)."
    ),
    PROVISION_PATH_TOO_LONG: (
        "Building the server's environment hit the Windows path limit (WinError 206). "
        "Use a shorter CLIO_AGENT_HOME or CLIO_KIT_CACHE_DIR, or enable Windows long paths."
    ),
    PROVISION_NO_PROGRESS: (
        "Building the server's environment stopped making progress, or ran past "
        "tools.mcp.provision_max_wait_s."
    ),
    START_FAILED: "The server process did not start or closed its connection.",
    START_NO_PROGRESS: (
        "The server neither answered nor did any work for tools.mcp.no_progress_s, or "
        "was still starting at tools.mcp.max_wait_s."
    ),
    CACHE_LOCK_TIMEOUT: "Another start held the shared launcher cache past its ceiling.",
    PROVISION_UNSUPPORTED_LAUNCHER: (
        "The installed launcher cannot prepare a server separately (clio-kit without "
        "'mcp-server --prepare'); the environment is built when the server starts."
    ),
}

_ENV_VAR = "UV_PROJECT_ENVIRONMENT"
_MARKER = ".clio-provisioned.json"
_DEFAULT_PROVISION_MAX_WAIT_S = 900.0
_POLL_S = 0.25

#: ``uv run`` options that ``uv sync`` takes with the same meaning (value-taking).
_SYNC_VALUE_FLAGS = frozenset(
    {
        "--project",
        "--directory",
        "--package",
        "--python",
        "-p",
        "--extra",
        "--group",
        "--no-group",
        "--only-group",
        "--index",
        "--default-index",
        "--index-url",
        "--extra-index-url",
        "--find-links",
        "-f",
        "--prerelease",
        "--resolution",
        "--exclude-newer",
    }
)
#: ``uv run`` options that ``uv sync`` takes with the same meaning (standalone).
_SYNC_BOOL_FLAGS = frozenset(
    {
        "--frozen",
        "--locked",
        "--dev",
        "--no-dev",
        "--only-dev",
        "--all-extras",
        "--all-groups",
        "--no-default-groups",
        "--no-editable",
        "--no-install-project",
        "--all-packages",
        "--no-sources",
        "--offline",
    }
)


class PrepareError(RuntimeError):
    """A typed preparation failure: ``reason`` is a :data:`PREPARE_REASONS` key."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


# --------------------------------------------------------------------------- #
# Where a ``uv run --project`` server's environment lives                     #
# --------------------------------------------------------------------------- #


def project_environment_dir(project_dir: str) -> Path:
    """CLIO's environment for one ``uv`` project: ``<cache>/mcp-project-envs/<name>-<hash>``."""

    from clio_agent import paths  # noqa: PLC0415 - paths reads the environment late

    resolved = Path(project_dir).expanduser()
    try:
        resolved = resolved.resolve()
    except OSError:
        pass
    digest = hashlib.sha256(os.path.normcase(str(resolved)).encode("utf-8")).hexdigest()[:16]
    name = resolved.parent.name if resolved.name in ("impl", "src", "") else resolved.name
    return paths.mcp_build_cache_dir() / "mcp-project-envs" / f"{name or 'project'}-{digest}"


def _uv_run(command: str, args: Sequence[str]) -> Any:
    from clio_agent.tools.uv_launcher import parse_uv_run_launcher  # noqa: PLC0415

    return parse_uv_run_launcher(command, [str(arg) for arg in args])


def uv_project_environment(
    command: str, args: Sequence[str], declared_env: Mapping[str, str]
) -> dict[str, str]:
    """``{UV_PROJECT_ENVIRONMENT: ...}`` for a ``uv run --project`` server, else ``{}``.

    A declaration that names its own environment keeps it, and a ``--no-sync`` launcher
    runs the environment its project already has (it declared that environment binding).
    """

    if _ENV_VAR in declared_env:
        return {}
    launcher = _uv_run(command, args)
    if launcher is None or launcher.sync_disabled:
        return {}
    return {_ENV_VAR: str(project_environment_dir(launcher.project_dir))}


# --------------------------------------------------------------------------- #
# Provision                                                                   #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProvisionPlan:
    """One provisioning command: ``argv`` with the extra ``env`` it runs under."""

    argv: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    marker_dir: str = ""
    fingerprint: str = ""
    #: The command's exit status meaning "this launcher cannot provision" (0: none).
    unsupported_exit: int = 0


def _sync_argv(command: str, args: Sequence[str]) -> list[str]:
    """The ``uv sync`` argv for a ``uv run`` launcher: its project flags, nothing else.

    Only called for an argv :func:`~clio_agent.tools.uv_launcher.parse_uv_run_launcher`
    accepted, so every option before the entry point is one it models.
    """

    from clio_agent.tools.uv_launcher import _VALUE_FLAGS  # noqa: PLC0415

    argv = [command, "sync"]
    tokens = [str(arg) for arg in args][1:]  # drop "run"
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--" or not token.startswith("-") or token == "-":
            break
        flag, _, inline = token.partition("=")
        takes_value = flag in _VALUE_FLAGS and not inline
        if flag in _SYNC_VALUE_FLAGS or flag in _SYNC_BOOL_FLAGS:
            argv.extend(tokens[index : index + (2 if takes_value else 1)])
        index += 2 if takes_value else 1
    return argv


def _project_fingerprint(project_dir: str, argv: Sequence[str]) -> str:
    digest = hashlib.sha256(json.dumps(list(argv[1:])).encode("utf-8"))
    for name in ("pyproject.toml", "uv.lock", ".python-version"):
        try:
            digest.update(name.encode() + b"\0" + (Path(project_dir) / name).read_bytes())
        except OSError:
            digest.update(name.encode() + b"\0-")
    return digest.hexdigest()


_PREPARE_OPTION: dict[tuple[str, ...], bool] = {}
_PREPARE_OPTION_LOCK = threading.Lock()


def launcher_supports_prepare(command: str | Sequence[str]) -> bool:
    """Whether this clio-kit offers ``mcp-server --prepare`` (read once from its help).

    ``command`` is the launcher executable, or the full launcher prefix when clio-kit
    runs through the desktop runtime's interpreter (``python -c <entry>``).
    """

    prefix = (command,) if isinstance(command, str) else tuple(command)
    with _PREPARE_OPTION_LOCK:
        known = _PREPARE_OPTION.get(prefix)
    if known is not None:
        return known
    try:
        done = subprocess.run(  # noqa: S603 - the declared launcher, fixed arguments
            [*prefix, "mcp-server", "--help"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        supported = done.returncode == 0 and "--prepare" in done.stdout
    except (OSError, subprocess.TimeoutExpired):
        supported = False
    with _PREPARE_OPTION_LOCK:
        _PREPARE_OPTION[prefix] = supported
    return supported


#: Exit status of :data:`_KIT_BUILD_SHIM` when this clio-kit lacks what it drives.
_KIT_SHIM_UNSUPPORTED = 97

#: Builds a clio-kit server's environment without starting the server, for a clio-kit
#: without ``mcp-server --prepare``. It runs ``clio-kit mcp-server <args>`` in the
#: launcher's own interpreter with the final launch replaced by a no-op, so the build
#: (materialize, ``uv sync --frozen``, cache upkeep) is exactly the one that the server's
#: start would do; that start then finds the environment built.
_KIT_BUILD_SHIM = f"""
import sys
try:
    import clio_kit as kit
    group, build = kit.main, kit._build_locked_environment
    kit._run_child_command
except (ImportError, AttributeError) as exc:
    sys.stderr.write(f'clio-kit cannot build without starting: {{exc!r}}\\n')
    sys.exit({_KIT_SHIM_UNSUPPORTED})
built = []
def _build(*args, **kwargs):
    ok = build(*args, **kwargs)
    built.append(bool(ok))
    return ok
kit._build_locked_environment = _build
kit._run_child_command = lambda *args, **kwargs: None
try:
    group.main(args=['mcp-server', *sys.argv[1:]], prog_name='clio-kit', standalone_mode=False)
except SystemExit as exc:
    if exc.code not in (0, None):
        raise
except Exception as exc:
    sys.stderr.write(f'{{exc}}\\n')
    sys.exit(1)
if False in built:
    sys.stderr.write('clio-kit failed to build the server environment\\n')
    sys.exit(3)
"""


def builds_outside_fence() -> bool:
    """Whether MCP environments are built unconfined, before the confined server starts.

    On Windows the fence (AppContainer, or the offline sandbox accounts) cannot host a
    ``uv`` build: writing a console script's PE resources is denied (#1991). The build
    is CLIO's own step, like installing the plugin, so it runs outside; the server it
    builds for still starts confined. Elsewhere the fence hosts the build unchanged.
    """

    return os.name == "nt"


def _uv_tool_python(launcher: Path) -> Path | None:
    """The interpreter of a ``uv tool install`` console script in uv's tool bin folder."""

    bin_dirs = [os.environ.get("UV_TOOL_BIN_DIR", ""), str(Path.home() / ".local" / "bin")]
    parent = os.path.normcase(str(launcher.parent))
    if not any(value and os.path.normcase(str(Path(value))) == parent for value in bin_dirs):
        return None
    tools = os.environ.get("UV_TOOL_DIR") or ""
    if not tools:
        roaming = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        tools = str(Path(roaming) / "uv" / "tools") if os.name == "nt" else ""
    if not tools:
        data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        tools = str(Path(data) / "uv" / "tools")
    scripts = "Scripts" if os.name == "nt" else "bin"
    python = Path(tools) / launcher.stem / scripts / ("python.exe" if os.name == "nt" else "python")
    return python if python.is_file() else None


def launcher_interpreter(resolved_command: str) -> str | None:
    """The Python interpreter that runs the ``clio-kit`` console script ``resolved_command``.

    A script in a virtual environment sits beside its interpreter; a ``uv tool``
    script runs its tool environment's interpreter. Anything else is unknown (``None``).
    """

    launcher = Path(resolved_command)
    sibling = launcher.with_name("python.exe" if os.name == "nt" else "python")
    if sibling.is_file() and (launcher.parent.parent / "pyvenv.cfg").is_file():
        return str(sibling)
    tool = _uv_tool_python(launcher)
    return str(tool) if tool is not None else None


def _kit_build_plan(interpreter: str, args: Sequence[str], env: Mapping[str, str]) -> ProvisionPlan:
    return ProvisionPlan(
        argv=(interpreter, "-c", _KIT_BUILD_SHIM, *[str(arg) for arg in args[1:]]),
        env=dict(env),
        unsupported_exit=_KIT_SHIM_UNSUPPORTED,
    )


def _clio_kit_plan(
    prefix: tuple[str, ...], interpreter: str | None, args: Sequence[str], env: Mapping[str, str]
) -> ProvisionPlan | str:
    """``mcp-server --prepare`` when offered, else the build shim where builds run outside."""

    if launcher_supports_prepare(prefix if len(prefix) > 1 else prefix[0]):
        return ProvisionPlan(argv=(*prefix, "mcp-server", "--prepare", args[1]), env=dict(env))
    if builds_outside_fence() and interpreter:
        return _kit_build_plan(interpreter, args, env)
    return PROVISION_UNSUPPORTED_LAUNCHER


def provision_plan(spec: Any, resolved_command: str) -> ProvisionPlan | str:
    """The provisioning command for ``spec``, or the typed outcome when there is none."""

    from clio_agent.tools.launcher_cache_lock import _launcher_name  # noqa: PLC0415

    command, args = str(spec.command), [str(arg) for arg in spec.args]
    launcher = _uv_run(command, args)
    if launcher is not None:
        if launcher.sync_disabled:
            return PROVISION_NOT_APPLICABLE
        argv = _sync_argv(resolved_command, args)
        redirect = uv_project_environment(command, args, spec.env)
        environment = redirect.get(_ENV_VAR) or str(spec.env.get(_ENV_VAR, ""))
        return ProvisionPlan(
            argv=tuple(argv),
            env=redirect,
            marker_dir=environment,
            fingerprint=_project_fingerprint(launcher.project_dir, argv),
        )
    if _launcher_name(command) == "clio-kit" and len(args) >= 2 and args[0] == "mcp-server":
        interpreter = launcher_interpreter(resolved_command) if builds_outside_fence() else None
        return _clio_kit_plan((resolved_command,), interpreter, args, {})
    return PROVISION_NOT_APPLICABLE


def bundled_provision_plan(
    launcher: tuple[str, Sequence[str], Mapping[str, str]],
) -> ProvisionPlan | str:
    """The provisioning plan for a server the desktop runtime starts with its own interpreter.

    ``launcher`` is :func:`~clio_agent.tools.desktop_mcp_runtime.bundled_module_launcher`'s
    result. A preinstalled adapter (the Web Search MCP) has nothing to build; a bundled
    clio-kit server is prepared through the same interpreter, with the same environment
    (bundled ``uv`` on ``PATH``, ``CLIO_KIT_CACHE_DIR``) as the server's start.
    """

    from clio_agent.tools.desktop_mcp_runtime import CLIO_KIT_ENTRY  # noqa: PLC0415

    executable, argv, env = launcher
    argv = [str(value) for value in argv]
    if argv[:2] != ["-c", CLIO_KIT_ENTRY]:
        return PROVISION_NOT_APPLICABLE
    args = argv[2:]
    if len(args) < 2 or args[0] != "mcp-server":
        return PROVISION_NOT_APPLICABLE
    prefix = (str(executable), "-c", CLIO_KIT_ENTRY)
    return _clio_kit_plan(prefix, str(executable), args, env)


def provision_max_wait_s() -> float:
    """``tools.mcp.provision_max_wait_s`` (900 s): ceiling on building one environment."""

    from clio_agent import conf  # noqa: PLC0415

    value = conf.resolve(
        "tools.mcp.provision_max_wait_s",
        env="CLIO_MCP_PROVISION_MAX_WAIT_S",
        default=_DEFAULT_PROVISION_MAX_WAIT_S,
        cast=conf.as_float,
    )
    return value if value > 0 else _DEFAULT_PROVISION_MAX_WAIT_S


def _marker_matches(plan: ProvisionPlan) -> bool:
    if not plan.marker_dir or not plan.fingerprint:
        return False
    try:
        recorded = json.loads((Path(plan.marker_dir) / _MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(recorded, dict) and recorded.get("fingerprint") == plan.fingerprint


def _write_marker(plan: ProvisionPlan) -> None:
    if not plan.marker_dir or not plan.fingerprint:
        return
    marker = Path(plan.marker_dir) / _MARKER
    try:
        marker.write_text(
            json.dumps({"fingerprint": plan.fingerprint, "argv": list(plan.argv[1:])}),
            encoding="utf-8",
        )
    except OSError as exc:  # the environment is built; only the shortcut is lost
        trace.event("TOOLS", "mcp_prepare_marker_unwritable path=%s error=%r", marker, exc)


def _plan_label(plan: ProvisionPlan) -> str:
    """A short name for ``plan``'s command in failures (never the build shim's source)."""

    if plan.unsupported_exit:
        return f"clio-kit mcp-server {' '.join(plan.argv[3:])} (build only)"
    return " ".join(plan.argv[:2])


def _failure_reason(output: str) -> str:
    """The typed reason a failed build's ``output`` names (Windows-specific causes)."""

    lowered = output.lower()
    if "windows pe resources" in lowered:
        return PROVISION_ACCESS_DENIED
    if any(
        marker in lowered
        for marker in ("os error 206", "winerror 206", "filename or extension is too long")
    ):
        return PROVISION_PATH_TOO_LONG
    return PROVISION_FAILED


class _UnsupportedLauncher(Exception):
    """The provisioning command reported :attr:`ProvisionPlan.unsupported_exit`."""


def _spawn(plan: ProvisionPlan, cwd: str) -> Any:
    """``plan``'s spawn: unconfined where builds run outside the fence, else confined."""

    from clio_agent.runtime import sandbox  # noqa: PLC0415 - avoid import cycle

    if builds_outside_fence():
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        return _Unconfined(
            command=plan.argv[0],
            args=list(plan.argv[1:]),
            popen_kwargs={"creationflags": flags} if flags else {},
        )
    return sandbox.wrap_confined(
        plan.argv[0],
        list(plan.argv[1:]),
        write_roots=sandbox.effective_write_roots(sandbox.PROFILE_FLEET, workspace_root=None),
        net_policy=sandbox.NET_ALLOW_RECORD,
        profile=sandbox.PROFILE_FLEET,
        pdeathsig=True,
        cwd=cwd,
    )


@dataclass
class _Unconfined:
    """An unconfined spawn with :class:`~clio_agent.runtime.sandbox.ConfinedSpawn`'s shape."""

    command: str
    args: list[str]
    env_overlay: dict[str, str] = field(default_factory=dict)
    popen_kwargs: dict[str, Any] = field(default_factory=dict)

    def release(self) -> None:
        """Nothing to release: no egress channel was opened."""


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    import psutil  # noqa: PLC0415

    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except psutil.Error:
        children = []
    for child in children:
        try:
            child.kill()
        except psutil.Error:
            continue
    process.kill()
    process.wait()


def _run_provision(namespace: str, plan: ProvisionPlan, env: dict[str, str], cwd: str) -> None:
    """Run ``plan`` while its process tree works (typed failure otherwise)."""

    from clio_agent.runtime.progress import ProcessTreeWork, progressed  # noqa: PLC0415
    from clio_agent.tools.mcp_server_progress import mcp_no_progress_s  # noqa: PLC0415

    confined = _spawn(plan, cwd)
    label = _plan_label(plan)
    trace.event(
        "TOOLS",
        "mcp_prepare_provision_start namespace=%s command=%s fence=%s",
        namespace,
        label,
        "outside" if isinstance(confined, _Unconfined) else "confined",
    )
    try:
        process = subprocess.Popen(  # noqa: S603 - declared launcher, composed argv
            [confined.command, *confined.args],
            env={**env, **confined.env_overlay},
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **confined.popen_kwargs,
        )
    except OSError as exc:
        confined.release()
        raise PrepareError(LAUNCHER_MISSING, f"{plan.argv[0]}: {exc}") from exc
    output: list[bytes] = []
    reader = threading.Thread(
        target=lambda: output.extend(iter(lambda: process.stdout.read(4096), b"")),  # type: ignore[union-attr]
        name=f"clio-provision-{namespace}",
        daemon=True,
    )
    reader.start()
    tree = ProcessTreeWork(process.pid)
    window, ceiling = mcp_no_progress_s(), provision_max_wait_s()
    started = last_check = time.monotonic()
    last = tree.sample()
    try:
        while process.poll() is None:
            time.sleep(_POLL_S)
            now = time.monotonic()
            if now - last_check < window:
                continue
            work = tree.sample()
            if not progressed(work, last) or now - started >= ceiling:
                _kill_tree(process)
                why = "no progress" if now - started < ceiling else "ceiling reached"
                raise PrepareError(PROVISION_NO_PROGRESS, f"{label} {why} after {now - started:.0f}s")
            last, last_check = work, now
    finally:
        reader.join(timeout=5)
        confined.release()
    if process.returncode != 0:
        text = b"".join(output).decode("utf-8", errors="replace").strip()
        if plan.unsupported_exit and process.returncode == plan.unsupported_exit:
            raise _UnsupportedLauncher(text[-500:])
        raise PrepareError(
            _failure_reason(text), f"{label} exited {process.returncode}: {text[-1500:]}"
        )


def provision(namespace: str, spec: Any, *, cwd: str | None = None) -> str:
    """Build ``spec``'s environment before its server starts; return the typed outcome.

    Returns :data:`PROVISIONED`, :data:`PROVISION_CACHED`, :data:`PROVISION_NOT_APPLICABLE`
    or :data:`PROVISION_UNSUPPORTED_LAUNCHER`.

    Raises:
        PrepareError: The launcher is missing, or the provisioning command failed or
            stopped making progress.
    """

    from clio_agent.tools.desktop_mcp_runtime import bundled_module_launcher  # noqa: PLC0415
    from clio_agent.tools.launcher_cache_lock import (  # noqa: PLC0415
        acquire_launcher_cache_lock,
        uses_shared_launcher_cache,
    )
    from clio_agent.tools.mcp_cache import _mcp_uv_cache_dir  # noqa: PLC0415
    from clio_agent.tools.mcp_environment import stdio_environment  # noqa: PLC0415

    if getattr(spec, "transport", "") != "stdio" or not getattr(spec, "command", ""):
        return PROVISION_NOT_APPLICABLE
    # The desktop runtime ships clio-kit and the Web Search adapter as modules of its
    # interpreter, not as console scripts on PATH; the server's start
    # (``mcp_config.transport_for``) prefers them, so provisioning must too.
    bundled = bundled_module_launcher(str(spec.command), [str(arg) for arg in spec.args])
    if bundled is not None:
        plan = bundled_provision_plan(bundled)
    else:
        from clio_agent.runtime.macos_clt import real_which  # noqa: PLC0415

        resolved = real_which(str(spec.command))
        if not resolved:
            raise PrepareError(LAUNCHER_MISSING, f"{spec.command!r} is not on PATH")
        plan = provision_plan(spec, resolved)
    if isinstance(plan, str):
        if plan == PROVISION_UNSUPPORTED_LAUNCHER:
            trace.event("TOOLS", "%s namespace=%s launcher=%s", plan, namespace, spec.command)
        return plan
    if _marker_matches(plan):
        return PROVISION_CACHED
    env = stdio_environment(spec.env, command=str(spec.command), args=spec.args)
    env.update(plan.env)
    if "UV_CACHE_DIR" not in spec.env:
        uv_cache = _mcp_uv_cache_dir()
        uv_cache.mkdir(parents=True, exist_ok=True)
        env["UV_CACHE_DIR"] = str(uv_cache)
    work_dir = cwd or os.getcwd()
    started = time.monotonic()
    try:
        if uses_shared_launcher_cache(spec):
            with acquire_launcher_cache_lock(namespace):
                _run_provision(namespace, plan, env, work_dir)
        else:
            _run_provision(namespace, plan, env, work_dir)
    except _UnsupportedLauncher as exc:
        trace.event(
            "TOOLS",
            "%s namespace=%s launcher=%s detail=%s",
            PROVISION_UNSUPPORTED_LAUNCHER,
            namespace,
            spec.command,
            str(exc)[-300:],
        )
        return PROVISION_UNSUPPORTED_LAUNCHER
    _write_marker(plan)
    trace.event(
        "TOOLS",
        "mcp_prepare_provisioned namespace=%s seconds=%.1f argv=%s",
        namespace,
        time.monotonic() - started,
        _plan_label(plan),
    )
    return PROVISIONED


def classify(exc: BaseException, phase: str) -> str:
    """The typed reason for a preparation failure in ``phase``."""

    from clio_agent.tools.launcher_cache_lock import LauncherCacheLockTimeoutError  # noqa: PLC0415
    from clio_agent.tools.mcp_config import MCPSpawnError  # noqa: PLC0415
    from clio_agent.tools.mcp_server_progress import NoProgressTimeout  # noqa: PLC0415

    if isinstance(exc, PrepareError):
        return exc.reason
    if isinstance(exc, LauncherCacheLockTimeoutError):
        return CACHE_LOCK_TIMEOUT
    if isinstance(exc, (MCPSpawnError, FileNotFoundError)):
        return LAUNCHER_MISSING
    if isinstance(exc, NoProgressTimeout):
        return PROVISION_NO_PROGRESS if phase == PHASE_PROVISION else START_NO_PROGRESS
    return PROVISION_FAILED if phase == PHASE_PROVISION else START_FAILED


# --------------------------------------------------------------------------- #
# Visible state                                                               #
# --------------------------------------------------------------------------- #


@dataclass
class Preparation:
    """One namespace's current preparation (what the status surfaces show)."""

    namespace: str
    state: str
    phase: str
    reason: str = ""
    detail: str = ""
    provision: str = ""
    attempt: int = 1
    tool_count: int | None = None
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def as_row(self) -> dict[str, Any]:
        """The JSON shape of this preparation."""

        row = asdict(self)
        row["elapsed_s"] = round(self.updated_at - self.started_at, 2)
        return {key: value for key, value in row.items() if value not in ("", None)}


_STATES: dict[str, Preparation] = {}
_CHANGED = threading.Condition()


def record(namespace: str, state: str, phase: str, **fields: Any) -> Preparation:
    """Record ``namespace``'s preparation state and wake every waiter.

    A new ``preparing`` record after a finished one starts a new preparation (its clock
    restarts); ``reason``/``detail`` are kept only on the record they were given to.
    """

    with _CHANGED:
        current = _STATES.get(namespace)
        now = time.time()
        if current is None or (state == PREPARING and current.state != PREPARING):
            current = Preparation(namespace=namespace, state=state, phase=phase)
        current.state, current.phase, current.updated_at = state, phase, now
        current.reason = str(fields.pop("reason", "") or "")
        current.detail = str(fields.pop("detail", "") or "")[:2000]
        for key, value in fields.items():
            setattr(current, key, value)
        _STATES[namespace] = current
        _CHANGED.notify_all()
    if state == FAILED:
        trace.event(
            "TOOLS",
            "mcp_prepare_failed namespace=%s phase=%s reason=%s detail=%s",
            namespace,
            phase,
            current.reason,
            current.detail[:300],
        )
    return current


def forget(namespace: str) -> None:
    """Drop ``namespace``'s record (its server set was reset; a reconnect follows)."""

    with _CHANGED:
        _STATES.pop(namespace, None)
        _CHANGED.notify_all()


def preparation_state(namespace: str) -> dict[str, Any] | None:
    """``namespace``'s current preparation row, or ``None`` when none was recorded."""

    with _CHANGED:
        current = _STATES.get(namespace)
        return current.as_row() if current is not None else None


def is_preparing(namespace: str) -> bool:
    """Whether a preparation of ``namespace`` is in progress."""

    with _CHANGED:
        current = _STATES.get(namespace)
        return current is not None and current.state == PREPARING


def wait_for_namespaces(
    namespaces: Iterable[str], timeout_s: float
) -> dict[str, dict[str, Any] | None]:
    """Wait until none of ``namespaces`` is ``preparing`` (or ``timeout_s`` passes).

    Returns each namespace's state row at return time (``None`` for one never recorded).
    """

    names = sorted(set(namespaces))
    deadline = time.monotonic() + max(0.0, timeout_s)
    with _CHANGED:
        while True:
            pending = [name for name in names if (s := _STATES.get(name)) and s.state == PREPARING]
            remaining = deadline - time.monotonic()
            if not pending or remaining <= 0:
                break
            _CHANGED.wait(min(remaining, 1.0))
        return {name: (_STATES[name].as_row() if name in _STATES else None) for name in names}


_OBSERVERS: dict[str, Any] = {}


def observe_listings(key: str, callback: Any) -> None:
    """Call ``callback(namespace, tools)`` after each live listing (``key`` replaces).

    The default gateway's tool catalog uses it to learn a server's tools when an agent
    first prepares that server, instead of starting every server at boot.
    """

    with _CHANGED:
        _OBSERVERS[key] = callback


def notify_listed(namespace: str, tools: Mapping[str, Any]) -> None:
    """Hand a fresh live listing of ``namespace`` to every observer.

    An observer records its own failures (the agent's catalog merge does, typed): one
    that raises fails this preparation's listing step, never silently.
    """

    with _CHANGED:
        observers = list(_OBSERVERS.values())
    for callback in observers:
        callback(namespace, dict(tools))


def reason_catalog() -> dict[str, str]:
    """The typed preparation reasons and what each means."""

    return dict(PREPARE_REASONS)


__all__ = [
    "FAILED",
    "PHASE_CONNECT",
    "PHASE_LAUNCH",
    "PHASE_PROVISION",
    "PREPARE_REASONS",
    "PREPARING",
    "PROVISIONED",
    "PROVISION_ACCESS_DENIED",
    "PROVISION_CACHED",
    "PROVISION_NOT_APPLICABLE",
    "PROVISION_PATH_TOO_LONG",
    "PROVISION_UNSUPPORTED_LAUNCHER",
    "READY",
    "PrepareError",
    "Preparation",
    "ProvisionPlan",
    "builds_outside_fence",
    "classify",
    "forget",
    "is_preparing",
    "launcher_interpreter",
    "launcher_supports_prepare",
    "notify_listed",
    "observe_listings",
    "preparation_state",
    "project_environment_dir",
    "provision",
    "provision_max_wait_s",
    "provision_plan",
    "reason_catalog",
    "record",
    "uv_project_environment",
    "wait_for_namespaces",
]
