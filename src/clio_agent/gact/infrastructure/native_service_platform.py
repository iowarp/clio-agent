"""Standard-library platform operations shipped with the native service controller.

Windows workers hold a kill-on-close Job Object before spawning any child. The
controller checks creation time and job membership on one process handle before
terminating that job, so a reused PID never grants authority over another process.
"""

from __future__ import annotations

import csv
import ctypes
import errno
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

_JOB: Any = None
LOG_GENERATIONS = 3
WINDOWS_LOCK_WAIT_SECONDS = 60.0


def write_json(path: Path, value: dict[str, Any]) -> None:
    """Publish a private receipt atomically on its owning filesystem."""
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as writer:
        temporary = Path(writer.name)
        json.dump(value, writer)
        writer.flush()
        os.fsync(writer.fileno())
    temporary.replace(path)


def clio_tool(name: str, interpreter: str | None = None) -> str | None:
    """CLIO's own copy of a tool (``uv``, ``uvx``) on this host, the host's only after it.

    CLIO brings its tools, so a host needs none installed. Checked in order: the
    bundled desktop runtime (``GACT_BUNDLED_RUNTIME_DIR/bin``); beside the
    interpreter (``interpreter``, else this process's: a desktop runtime's
    ``python/bin`` -> ``bin``, a deployed install's ``<root>/clio-agent/.venv/bin``
    -> ``<root>/bin``); the
    launcher's ``CLIO_BIN_DIR``; then ``PATH`` and ``~/.local/bin``.
    """
    executable = f"{name}.exe" if os.name == "nt" else name
    # Not resolved: a venv's interpreter links into uv's Python store.
    python = Path(interpreter or sys.executable).absolute()
    bundled = os.environ.get("GACT_BUNDLED_RUNTIME_DIR", "").strip()
    candidates = [
        *([Path(bundled) / "bin" / executable] if bundled else []),
        *(parent / "bin" / executable for parent in python.parents[0:4]),
        *(
            [Path(os.environ["CLIO_BIN_DIR"]) / executable]
            if os.environ.get("CLIO_BIN_DIR")
            else []
        ),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(executable)
    if found:
        return found
    home = Path.home() / ".local" / "bin" / executable
    return str(home) if home.is_file() else None


def python_path(root: Path) -> Path:
    """The service environment's interpreter on the execution host."""
    return root / "environment/.venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def http_get(url: str, key: str = "") -> tuple[int, bytes]:
    """Status and body of a proxy-free loopback GET; 0 when nothing answered."""
    request = Request(url, headers={"Authorization": f"Bearer {key}"} if key else {})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, b""
    except (OSError, URLError, ValueError):
        return 0, b""


#: Unix socket paths are limited to 107 bytes; vLLM's tensor-parallel workers bind sockets
#: under TMPDIR, and on a deep service root they fail to start without logging why.
_SHORT_TMP_LIMIT = 64


def service_tmp(root: Path) -> Path:
    """The service's temp directory: ``root/tmp``, or a short private one when that is too deep."""
    tmp = root / "tmp"
    if len(str(tmp)) <= _SHORT_TMP_LIMIT:
        return tmp
    import hashlib  # noqa: PLC0415

    short = Path("/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir())
    short = short / ("clio-svc-" + hashlib.sha256(str(root).encode()).hexdigest()[:16])
    short.mkdir(mode=0o700, exist_ok=True)
    info = short.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError(f"The short service temp directory {short} is not private to this user")
    return short


def worker_environment(
    root: Path, manifest: dict[str, Any], base: dict[str, str]
) -> dict[str, str]:
    """Isolate interpreter and caches; expose the service's own console tools first."""
    env = {**base, **manifest.get("environment", {})}
    for name in ("UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"):
        env.pop(name, None)
    tmp = service_tmp(root)
    env.update(
        UV_CACHE_DIR=str(root / "cache/uv"),
        UV_PYTHON_INSTALL_DIR=str(root / "cache/python"),
        TMPDIR=str(tmp),
        TEMP=str(tmp),
        TMP=str(tmp),
        HF_HOME=str(root / "cache/huggingface"),
        PATH=os.pathsep.join(
            item for item in (str(python_path(root).parent), env.get("PATH", "")) if item
        ),
    )
    return env


def is_link(path: Path) -> bool:
    """Directory junctions grant no more ownership than symbolic links."""
    return path.is_symlink() or path.is_junction()


def validate_interpreter(root: Path) -> None:
    """Store Python's activation broker can escape the worker's job; require a managed venv."""
    if os.name == "nt":
        configuration = root / "environment/.venv/pyvenv.cfg"
        if configuration.is_file() and "windowsapps" in configuration.read_text().casefold():
            raise ValueError(
                "Reinstall this native service from scratch with uv-managed Python; Microsoft Store Python cannot provide owned process-tree supervision"
            )


@contextmanager
def locked(root: Path) -> Iterator[None]:
    """Serialize controllers and detached workers with a kernel-backed file lock."""
    # A Windows open file cannot be deleted. Keep its lifecycle lock outside the
    # owned directory so delete_data can remove that directory while serialized.
    path = root.parent / f".{root.name}.clio-service.lock" if os.name == "nt" else root / ".lock"
    if path.is_symlink():
        raise ValueError("Native service lock cannot be a symlink")
    with path.open("a+b") as stream:
        if sys.platform == "win32":
            import msvcrt

            stream.seek(0)
            # Lock one byte beyond EOF if this is a new file; Windows permits it.
            _acquire_windows_lock(stream.fileno(), path)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _acquire_windows_lock(descriptor: int, path: Path) -> None:
    """Wait through lifecycle handoff without the CRT's implicit ten-retry exit."""
    import msvcrt

    started = time.monotonic()
    announced = False
    while True:
        try:
            # Win32-only API; mypy checks this module against the host platform stubs.
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            break
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            elapsed = time.monotonic() - started
            if elapsed >= WINDOWS_LOCK_WAIT_SECONDS:
                raise TimeoutError(f"Native service lifecycle lock timed out: {path}") from exc
            if elapsed >= 1 and not announced:
                print(
                    json.dumps(
                        {"event": "lifecycle_lock_wait", "pid": os.getpid(), "path": str(path)}
                    ),
                    file=sys.stderr,
                    flush=True,
                )
                announced = True
            time.sleep(min(0.05, WINDOWS_LOCK_WAIT_SECONDS - elapsed))
    if announced:
        print(
            json.dumps(
                {
                    "event": "lifecycle_lock_acquired",
                    "pid": os.getpid(),
                    "seconds": round(time.monotonic() - started, 3),
                }
            ),
            file=sys.stderr,
            flush=True,
        )


def protect_root(root: Path) -> None:
    """On Windows restrict inherited service files and secrets to the execution user."""
    if os.name != "nt":
        root.chmod(0o700)
        return
    done = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        creationflags=0x08000000,
    )
    rows = list(csv.reader(io.StringIO(done.stdout)))
    sid = rows[0][1] if len(rows) == 1 and len(rows[0]) == 2 else ""
    if not sid.startswith("S-1-") or any(
        part and not part.isdigit() for part in sid[4:].split("-")
    ):
        raise RuntimeError("Could not resolve the native service owner's Windows SID")
    subprocess.run(
        ["icacls", str(root), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],
        capture_output=True,
        check=True,
        timeout=15,
        creationflags=0x08000000,
    )


def _kernel() -> Any:
    if sys.platform != "win32":
        raise RuntimeError("Windows process operations require Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures: dict[str, tuple[list[Any], Any]] = {
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "GetProcessTimes": (
            [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4,
            wintypes.BOOL,
        ),
        "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "CreateJobObjectW": ([wintypes.LPVOID, wintypes.LPCWSTR], wintypes.HANDLE),
        "OpenJobObjectW": ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": (
            [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD],
            wintypes.BOOL,
        ),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "IsProcessInJob": (
            [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)],
            wintypes.BOOL,
        ),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "QueryInformationJobObject": (
            [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID],
            wintypes.BOOL,
        ),
        "GetCurrentProcess": ([], wintypes.HANDLE),
        "GetLastError": ([], wintypes.DWORD),
        "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


def _identity(kernel: Any, handle: Any) -> str:
    code = wintypes.DWORD()
    if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
        return ""
    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
        return ""
    return f"windows:{times[0].dwHighDateTime}:{times[0].dwLowDateTime}"


def identity(pid: int) -> str:
    """A live Windows process's exact creation FILETIME, or empty when unavailable."""
    if pid <= 0:
        return ""
    kernel = _kernel()
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        return _identity(kernel, handle)
    finally:
        kernel.CloseHandle(handle)


class _Limits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD),
        ("minimum", ctypes.c_size_t),
        ("maximum", ctypes.c_size_t),
        ("processes", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _Limits),
        ("io", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t),
        ("peak_job", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("times", ctypes.c_int64 * 4),
        ("faults", wintypes.DWORD),
        ("total", wintypes.DWORD),
        ("active", wintypes.DWORD),
        ("terminated", wintypes.DWORD),
    ]


def job_name(generation: str) -> str:
    """One named kernel job per unguessable, durable launch generation."""
    return "Local\\CLIO-native-" + generation


def bind_worker(generation: str, root: Path) -> None:
    """Bind every future descendant to this worker; fail before spawning on error."""
    global _JOB
    kernel = _kernel()
    job = kernel.CreateJobObjectW(None, job_name(generation))
    if not job:
        raise OSError("Could not create the native service Job Object")
    if kernel.GetLastError() == 183:
        kernel.CloseHandle(job)
        raise RuntimeError("Native service launch generation already has a Job Object")
    limits = _ExtendedLimits()
    limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; children may not break away.
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        kernel.CloseHandle(job)
        raise OSError("Could not configure native service process-tree cleanup")
    if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        kernel.CloseHandle(job)
        raise OSError("Could not bind the native service worker to its Job Object")
    _JOB = job  # The OS closes it on worker exit, including a hard kill.
    write_json(
        root / "evidence" / f"worker-{generation}.json",
        {
            "generation": generation,
            "pid": os.getpid(),
            "process_identity": identity(os.getpid()),
        },
    )


def await_worker(root: Path, generation: str) -> dict[str, Any]:
    """Record the actual worker, including when a Windows venv launcher has another PID."""
    deadline = time.monotonic() + 10
    ready = root / "evidence" / f"worker-{generation}.json"
    while time.monotonic() < deadline:
        if ready.is_file():
            record = json.loads(ready.read_text())
            if record.get("generation") != generation or not record.get("process_identity"):
                raise RuntimeError("The native worker published an invalid launch identity")
            if identity(record["pid"]) != record["process_identity"]:
                raise RuntimeError("The native worker exited before its launch committed")
            return record
        time.sleep(0.05)
    raise RuntimeError("Native worker did not establish its owned process tree")


def stop(receipt: dict[str, Any]) -> None:
    """Terminate only the receipt's exact worker and its verified job, then await all children."""
    kernel = _kernel()
    process = kernel.OpenProcess(0x1000, False, receipt["pid"])
    if not process:
        return
    job = None
    try:
        if _identity(kernel, process) != receipt["process_identity"]:
            return
        job = kernel.OpenJobObjectW(0x0004 | 0x0008, False, job_name(receipt["generation"]))
        member = wintypes.BOOL()
        if (
            not job
            or not kernel.IsProcessInJob(process, job, ctypes.byref(member))
            or not member.value
        ):
            raise RuntimeError(
                "Native service worker has no matching owned Job Object; data retained"
            )
        if not kernel.TerminateJobObject(job, 1):
            raise OSError("Could not stop the native service process tree")
        deadline = time.monotonic() + 10
        while True:
            accounting = _Accounting()
            if not kernel.QueryInformationJobObject(
                job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
            ):
                raise OSError("Could not confirm native service process-tree termination")
            if not accounting.active:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("Native service children did not stop; data retained")
            time.sleep(0.05)
    finally:
        if job:
            kernel.CloseHandle(job)
        kernel.CloseHandle(process)


def wait_launcher_exit(receipt: dict[str, Any]) -> None:
    """Await the venv broker outside the job before deleting its mapped executable."""
    pid = receipt.get("launcher_pid", 0)
    expected = receipt.get("launcher_identity", "")
    if not pid or not expected:
        return
    deadline = time.monotonic() + 10
    while identity(pid) == expected:
        if time.monotonic() >= deadline:
            raise RuntimeError("Native Python launcher did not exit; runtime files retained")
        time.sleep(0.02)


def release_children() -> None:
    """Reap descendants after the server exits, before declaring its worker stopped."""
    kernel = _kernel()
    deadline = time.monotonic() + 10
    capacity = 256
    while True:
        buffer = ctypes.create_string_buffer(8 + capacity * ctypes.sizeof(ctypes.c_size_t))
        if not kernel.QueryInformationJobObject(_JOB, 3, buffer, len(buffer), None):
            if kernel.GetLastError() == 234 and capacity < 65536:
                capacity *= 2
                continue
            raise OSError("Could not inspect the native worker's owned descendants")
        count = ctypes.c_uint32.from_buffer(buffer, 4).value
        pids = (ctypes.c_size_t * count).from_buffer(buffer, 8)
        children = [pid for pid in pids if pid != os.getpid()]
        if not children:
            return
        for pid in children:
            process = kernel.OpenProcess(0x1000 | 0x0001, False, pid)
            if not process:
                continue
            try:
                member = wintypes.BOOL()
                if not kernel.IsProcessInJob(process, _JOB, ctypes.byref(member)):
                    raise OSError("Could not verify native descendant ownership")
                if (
                    member.value
                    and _identity(kernel, process)
                    and not kernel.TerminateProcess(process, 1)
                ):
                    raise OSError("Could not reap the native worker's descendant")
            finally:
                kernel.CloseHandle(process)
        if time.monotonic() >= deadline:
            raise RuntimeError("Native service descendants did not stop")
        time.sleep(0.05)


def with_host_setup(setup: str, command: list[str]) -> list[str]:
    """``command`` after the host setup lines (``module load ...``) in a login shell; fails on error."""
    if not setup.strip():
        return command
    return ["bash", "--login", "-c", f'set -e\n{setup}\nexec "$@"', "bash", *command]


def copy_output(stream: Any, output: Any, secrets: list[str]) -> None:
    """Copy the server's merged output into its log with credentials redacted."""
    for line in stream:
        for value in secrets:
            line = line.replace(value, "[redacted]")
        output.write(line)
        output.flush()


def rotate_log(log: Path, keep: int = LOG_GENERATIONS) -> None:
    """Shift a non-empty log to ``.1`` (up to ``.keep``): a failed start's log is evidence (F008)."""
    if not log.is_file() or log.stat().st_size == 0:
        return
    log.with_name(f"{log.name}.{keep}").unlink(missing_ok=True)
    for index in range(keep - 1, 0, -1):
        older = log.with_name(f"{log.name}.{index}")
        if older.is_file():
            older.replace(log.with_name(f"{log.name}.{index + 1}"))
    log.replace(log.with_name(f"{log.name}.1"))


def stop_posix(receipt: dict[str, Any], alive: Callable[[dict[str, Any]], bool]) -> None:
    """Stop the exact POSIX process group and wait for its worker."""
    if sys.platform == "win32":
        raise RuntimeError("POSIX process groups require a POSIX host")
    pid = receipt["pid"]
    if os.getpgid(pid) != pid:
        raise ValueError("Recorded native service is not its own process group")
    os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while alive(receipt) and time.monotonic() < deadline:
        time.sleep(0.1)
    if alive(receipt):
        os.killpg(pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while alive(receipt) and time.monotonic() < deadline:
            time.sleep(0.1)
    if alive(receipt):
        raise RuntimeError("Native service did not stop; its data was retained")
