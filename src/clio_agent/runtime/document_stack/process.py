"""Portable, cancellable subprocess and native document-tool discovery."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class DocumentError(RuntimeError):
    """A document operation failed without producing verified output."""


class DocumentCancelled(DocumentError):
    """The person or the turn stopped a document operation; its process tree was killed."""


#: A document job runs until it exits or someone stops it. While it runs, the
#: watcher is told how long it has been going: first after this many seconds,
#: then every ``HEARTBEAT_EVERY_S``.
HEARTBEAT_FIRST_S = 10.0
HEARTBEAT_EVERY_S = 15.0
#: A job whose process tree has used no CPU for this long is reported as stalled
#: (still running, never killed for it): the person sees it and can stop it.
STALL_IDLE_S = 30.0
#: CPU seconds of the whole tree below which a sample counts as no work.
_STALL_CPU_EPSILON_S = 0.05
_STALL_SAMPLE_S = 2.0
_POLL_S = 0.25

#: ``heartbeat(elapsed_s, stalled_s)``: ``stalled_s`` is how long the job has done no
#: work, or ``None`` while it is working (or its work cannot be measured).
Heartbeat = Callable[[float, float | None], None]

_WATCH: ContextVar[tuple[Callable[[], bool] | None, Heartbeat | None]] = ContextVar(
    "clio_document_watch", default=(None, None)
)


def stall_message(name: str, stalled_s: float) -> str:
    """Say plainly that a job is stuck, with the likely cause on this platform."""
    hint = ""
    if sys.platform == "darwin":
        hint = (
            " On macOS a sandbox denial can leave LibreOffice waiting on its pipe"
            " without ever exiting."
        )
    return (
        f"{name} has done no work for {stalled_s:.0f} s and may be stuck.{hint}"
        " Stop the turn to cancel it."
    )


class _WorkMeter:
    """Seconds since ``process``'s tree last used CPU; ``None`` when it cannot be measured.

    A tree containing a process this user may not inspect (a sandbox account's worker)
    is never called stalled on partial evidence.
    """

    def __init__(self, process: subprocess.Popen[Any]) -> None:
        self._pid = process.pid
        self._cpu: float | None = None
        self._busy_at = time.monotonic()
        self._sampled_at = 0.0
        self._unknown = False

    def stalled_s(self, now: float) -> float | None:
        if self._unknown:
            return None
        if now - self._sampled_at >= _STALL_SAMPLE_S:
            self._sampled_at = now
            cpu = self._tree_cpu()
            if cpu is None:
                self._unknown = True
                return None
            if self._cpu is None or cpu - self._cpu > _STALL_CPU_EPSILON_S:
                self._busy_at = now
                self._cpu = cpu
        idle = now - self._busy_at
        return idle if idle >= STALL_IDLE_S else None

    def _tree_cpu(self) -> float | None:
        try:
            import psutil
        except ImportError:
            return None
        try:
            root = psutil.Process(self._pid)
            tree = [root, *root.children(recursive=True)]
        except psutil.NoSuchProcess:
            return self._cpu
        except psutil.Error:
            return None
        total = 0.0
        for member in tree:
            try:
                times = member.cpu_times()
            except psutil.NoSuchProcess:
                continue
            except psutil.Error:
                return None
            total += times.user + times.system
        return total


@contextlib.contextmanager
def watched(
    cancelled: Callable[[], bool] | None = None,
    heartbeat: Heartbeat | None = None,
) -> Iterator[None]:
    """Scope how document processes started below learn of a stop and report that they run.

    ``cancelled`` is polled while a process runs; when it returns true the whole
    process tree is killed and :class:`DocumentCancelled` is raised. ``heartbeat``
    receives the elapsed seconds while a process is still running, and how long it
    has done no work once it looks stalled (see :data:`Heartbeat`).
    """
    token = _WATCH.set((cancelled, heartbeat))
    try:
        yield
    finally:
        _WATCH.reset(token)


def kill_tree(process: subprocess.Popen[Any]) -> None:
    """Kill ``process`` and every descendant, then reap it."""
    try:
        import psutil

        try:
            children = psutil.Process(process.pid).children(recursive=True)
        except psutil.Error:
            children = []
        for child in reversed(children):
            try:
                child.kill()
            except psutil.Error:
                pass
    except ImportError:
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.communicate(timeout=30)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass


def wait_process(
    process: subprocess.Popen[Any],
    *,
    name: str,
    timeout: float | None = None,
    cancelled: Callable[[], bool] | None = None,
    heartbeat: Heartbeat | None = None,
) -> tuple[Any, Any]:
    """Wait for ``process`` with no time limit unless ``timeout`` is given.

    A stop from ``cancelled`` (or the :func:`watched` scope) kills the process
    tree and raises :class:`DocumentCancelled`. ``timeout`` is for
    infrastructure steps only; an agent's document job never sets it.

    A job whose tree does no work for :data:`STALL_IDLE_S` (a sandboxed soffice
    blocked on a denied pipe, say) is not killed: it is logged and reported to
    ``heartbeat`` at once as stalled, and a stop then says it was stuck.
    """
    scoped_cancelled, scoped_heartbeat = _WATCH.get()
    cancelled = cancelled or scoped_cancelled
    heartbeat = heartbeat or scoped_heartbeat
    started = time.monotonic()
    next_beat = started + HEARTBEAT_FIRST_S
    meter = _WorkMeter(process) if timeout is None else None
    stalled: float | None = None
    while True:
        if cancelled is not None and cancelled():
            kill_tree(process)
            if stalled is not None:
                raise DocumentCancelled(
                    f"{name} was stopped after doing no work for {stalled:.0f} s (stuck); "
                    "its processes were ended"
                )
            raise DocumentCancelled(f"{name} was stopped; its processes were ended")
        now = time.monotonic()
        if timeout is not None and now - started >= timeout:
            kill_tree(process)
            raise DocumentError(f"{name} exceeded {timeout:g} seconds")
        wait = _POLL_S if timeout is None else max(0.0, min(_POLL_S, started + timeout - now))
        try:
            # Retrying communicate after TimeoutExpired loses no output.
            return process.communicate(timeout=wait)
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        was_stalled = stalled is not None
        stalled = meter.stalled_s(now) if meter is not None else None
        if stalled is not None and not was_stalled:
            logger.warning(
                "reason=document_process_stalled name=%s pid=%s idle_s=%.0f elapsed_s=%.0f",
                name,
                process.pid,
                stalled,
                now - started,
            )
            next_beat = now
        elif stalled is None and was_stalled:
            logger.info("reason=document_process_resumed name=%s pid=%s", name, process.pid)
            next_beat = now
        if heartbeat is not None and now >= next_beat:
            next_beat = now + HEARTBEAT_EVERY_S
            try:
                heartbeat(now - started, stalled)
            except Exception:  # noqa: BLE001 - a watcher never stops the job
                pass


# LibreOffice reads user settings from <UserInstallation>/user, not the profile root.
# OpenCL only accelerates interactive Calc; its per-machine device test costs a
# headless conversion several seconds and would tie a seeded profile to one GPU.
OFFICE_PROFILE_SETTINGS = (
    '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
    '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
    '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>'
    '<item oor:path="/org.openoffice.Office.Common/Misc">'
    '<prop oor:name="UseOpenCL" oor:op="fuse"><value>false</value></prop></item>'
    "</oor:items>"
)


def office_profile_key(executable: str | Path) -> str:
    """Name the seeded profile for one Office build; the profile itself is relocatable."""
    path = Path(executable).resolve()
    info = path.stat()
    return hashlib.sha256(f"{path.name}:{info.st_size}:{info.st_mtime_ns}".encode()).hexdigest()[
        :16
    ]


def office_profile_template(executable: str | Path, templates: Path | None = None) -> Path | None:
    """Return this build's seeded first-start profile, if prepare created one."""
    if templates is None and (configured := os.environ.get("CLIO_DOCUMENT_OFFICE_PROFILES")):
        templates = Path(configured)
    if templates is None:
        return None
    try:
        profile = templates / office_profile_key(executable) / "profile"
        return profile if (profile / "user" / "registrymodifications.xcu").is_file() else None
    except OSError:
        return None


def new_office_profile(profile: Path, template: Path | None = None) -> None:
    """Start from the seeded profile when readable; otherwise LibreOffice builds one."""
    if template is not None:
        try:
            shutil.copytree(template, profile)
            return
        except OSError:
            shutil.rmtree(profile, ignore_errors=True)
    (profile / "user").mkdir(parents=True)
    (profile / "user" / "registrymodifications.xcu").write_text(
        OFFICE_PROFILE_SETTINGS, encoding="utf-8"
    )


def _windows_dacl_protected(path: Path) -> bool:
    """Inspect one directory's ACL without walking its descendants."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL("advapi32", use_last_error=True)
    get_security = api.GetFileSecurityW
    get_security.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_security.restype = wintypes.BOOL
    needed = wintypes.DWORD()
    get_security(str(path), 4, None, 0, ctypes.byref(needed))
    if not needed.value:
        raise DocumentError(f"Cannot inspect document-directory permissions: {ctypes.WinError()}")
    descriptor = ctypes.create_string_buffer(needed.value)
    if not get_security(str(path), 4, descriptor, needed.value, ctypes.byref(needed)):
        raise DocumentError(f"Cannot read document-directory permissions: {ctypes.WinError()}")
    get_control = api.GetSecurityDescriptorControl
    get_control.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_control.restype = wintypes.BOOL
    control, revision = wintypes.WORD(), wintypes.DWORD()
    if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
        raise DocumentError(f"Cannot inspect document ACL inheritance: {ctypes.WinError()}")
    return bool(control.value & 0x1000)  # SE_DACL_PROTECTED


def inherit_parent_access(path: Path) -> None:
    """Keep published document directories accessible under their parent's existing grants.

    Windows Python 3.13+ creates private temporary directories with a protected
    ACL. That is appropriate for an Office profile, but excludes the host or
    sandbox user from shared output/cache directories. Only protected roots need
    repair; ordinary calls never recursively reapply permissions.
    """
    if _windows_dacl_protected(path):
        run(["icacls", str(path), "/inheritancelevel:e", "/Q"], cwd=path, timeout=30)


#: Written into CLIO-owned workspace machinery directories. ripgrep (the agent's
#: fs_search/fs_glob, and ``rg`` in a shell) honours ``.ignore`` files, so their
#: contents stay out of searches for the user's files.
SEARCH_IGNORE_MARKER = "# Written by CLIO: generated document machinery, not user files.\n# Keeps it out of searches.\n*\n"
#: The same for git: the document toolchain leaves tens of MB of node_modules in
#: the workspace, which ``git add -A`` in the person's project would otherwise stage.
GIT_IGNORE_MARKER = "# Written by CLIO: generated document machinery, not user files.\n# Keeps it out of version control.\n*\n"


def mark_machinery_ignored(directory: Path) -> None:
    """Hide ``directory``'s contents from ripgrep and git, writing each marker if absent."""
    for name, text in ((".ignore", SEARCH_IGNORE_MARKER), (".gitignore", GIT_IGNORE_MARKER)):
        marker = directory / name
        try:
            if not marker.exists():
                marker.write_text(text, encoding="utf-8")
        except OSError:
            pass


def scratch_root(workspace: Path, *, create: bool = True) -> Path:
    """Create workspace-owned scratch storage without following an escaping link."""
    workspace = workspace.resolve()
    root = (workspace / ".tmp" / "clio-documents").resolve()
    if not root.is_relative_to(workspace):
        raise DocumentError("Document scratch storage escapes the active workspace")
    if create:
        root.mkdir(parents=True, exist_ok=True)
        mark_machinery_ignored(root)
    return root


def run(
    command: list[str],
    *,
    cwd: Path,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    windows_command_line: str | None = None,
    cancelled: Callable[[], bool] | None = None,
    heartbeat: Heartbeat | None = None,
) -> str:
    """Run without a shell, allowing explicit Windows quoting for non-CRT parsers.

    The command runs until it exits or is stopped (see :func:`wait_process`);
    only infrastructure steps pass a ``timeout``.
    Children never write bytecode: LibreOffice's embedded Python would otherwise
    write ``.pyc`` into its application tree and break macOS's code-signature seal.
    """
    if windows_command_line is not None and os.name != "nt":
        raise DocumentError("An explicit Windows command line requires Windows")
    env = {**(os.environ if env is None else env), "PYTHONDONTWRITEBYTECODE": "1"}
    flags: dict[str, Any] = {}
    if os.name == "nt":
        flags["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        process = subprocess.Popen(
            windows_command_line if windows_command_line is not None else command,
            executable=command[0] if windows_command_line is not None else None,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **flags,
        )
    except OSError as exc:
        raise DocumentError(f"Could not run {Path(command[0]).name}: {exc}") from exc
    stdout, stderr = wait_process(
        process,
        name=Path(command[0]).name,
        timeout=timeout,
        cancelled=cancelled,
        heartbeat=heartbeat,
    )
    if process.returncode:
        detail = (stderr or stdout).strip()[-4000:]
        raise DocumentError(f"{Path(command[0]).name} exited {process.returncode}: {detail}")
    return stdout or stderr


def find_native(name: str) -> str | None:
    """Find a configured native tool, including standard desktop install paths."""
    override = os.environ.get(f"CLIO_DOCUMENT_{name.upper()}", "").strip()
    if override:
        candidate = Path(override).expanduser().resolve()
        if not candidate.is_file():
            raise DocumentError(f"CLIO_DOCUMENT_{name.upper()} is not an executable file")
        return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    if name == "soffice":
        from importlib import import_module

        office = import_module(f"{__package__}.office" if __package__ else "office")
        roots = (
            [Path(os.environ["CLIO_DOCUMENT_OFFICE_ROOT"])]
            if os.environ.get("CLIO_DOCUMENT_OFFICE_ROOT")
            else []
        )
        if bundled := os.environ.get("GACT_BUNDLED_RUNTIME_DIR"):
            roots.append(Path(bundled) / "documents" / "office")
        roots.extend(
            parent / "documents" / "office" for parent in Path(sys.executable).resolve().parents[:4]
        )
        for root in roots:
            if private_candidate := office.executable(root):
                return str(private_candidate)
        candidates = [Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")]
        if sys.platform == "win32":
            candidates.extend(
                Path(install_root) / "LibreOffice" / "program" / "soffice.com"
                for key in ("ProgramFiles", "ProgramFiles(x86)")
                if (install_root := os.environ.get(key))
            )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return shutil.which("libreoffice")
    return None


def office_convert(
    source: Path,
    output_dir: Path,
    target: str,
    *,
    execute: Callable[[list[str], Path], None] | None = None,
    executable_path: str | None = None,
    workspace: Path | None = None,
    profile_templates: Path | None = None,
) -> Path:
    """Convert a copy using a fresh LibreOffice profile; never overwrite the source.

    The fresh profile is a copy of the one seeded during prepare, so each call
    skips LibreOffice's first-start profile build.
    """
    executable = executable_path or find_native("soffice")
    if not executable:
        if os.environ.get("CLIO_DOCUMENT_CONFINED") == "1":
            raise DocumentError(
                "The prepared Office renderer is unavailable to the confined worker; "
                "repair renderer access on the CLIO host before retrying. "
                "Renderer installation cannot run inside document execution."
            )
        root = os.environ.get("CLIO_DOCUMENT_OFFICE_ROOT", "")
        if not root:
            raise DocumentError(
                "LibreOffice is unavailable; prepare Clio's execution runtime first"
            )
        from importlib import import_module

        office = import_module(f"{__package__}.office" if __package__ else "office")
        executable = office.ensure_office(Path(root))
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{source.stem}.{target}"
    if output.exists() or output.resolve() == source.resolve():
        raise DocumentError(f"Refusing to overwrite {output}")
    profile_root = scratch_root(workspace) if workspace is not None else None
    if profile_root is None and (configured := os.environ.get("CLIO_DOCUMENT_SCRATCH")):
        profile_root = Path(configured).resolve()
    # Native Windows profile APIs still have MAX_PATH limits. Only the disposable
    # profile may need a short allowed TMP path; document staging stays in .tmp.
    if profile_root is not None and os.name == "nt" and len(str(profile_root)) > 80:
        profile_root = Path(os.environ.get("CLIO_DOCUMENT_SHORT_TEMP", tempfile.gettempdir()))
    temporary = tempfile.TemporaryDirectory(prefix="lo-", dir=profile_root)
    try:
        if execute is not None:
            # A host-created profile must be reachable by the confined executor.
            # Profiles created inside the worker keep their private ACL instead.
            inherit_parent_access(Path(temporary.name))
        profile = Path(temporary.name).resolve() / "profile"
        new_office_profile(profile, office_profile_template(executable, profile_templates))
        command = [
            executable,
            f"-env:UserInstallation={profile.as_uri()}",
            "--headless",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            "--convert-to",
            target,
            "--outdir",
            str(output_dir),
            str(source),
        ]
        # CLIO's macOS Office fence grants the single-instance pipe only this directory.
        if pipe_dir := os.environ.get("OSL_SOCKET_PATH"):
            command.insert(1, f"-env:OSL_SOCKET_PATH={pipe_dir}")
        if execute is None:
            run(command, cwd=output_dir)
        else:
            execute(command, output_dir)
    finally:
        _cleanup_profile(temporary)
    if not output.is_file() or not output.stat().st_size:
        raise DocumentError("LibreOffice returned without producing a nonempty output file")
    return output


def _cleanup_profile(temporary: tempfile.TemporaryDirectory[str]) -> None:
    # Windows can release Office's profile handles just after the console exits.
    for attempt in range(20):
        try:
            if os.name == "nt":
                root = str(Path(temporary.name).resolve())
                extended = (
                    "\\\\?\\UNC\\" + root[2:] if root.startswith("\\\\") else "\\\\?\\" + root
                )
                try:
                    shutil.rmtree(extended)
                except FileNotFoundError:
                    pass
            temporary.cleanup()
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in {5, 32, 145} or attempt == 19:
                raise DocumentError(f"Could not clean the private Office profile: {exc}") from exc
            time.sleep(0.1)
