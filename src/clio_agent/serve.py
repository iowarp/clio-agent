"""Connect-or-spawn primitive for the GACT server (the *one front door*, #799 phase 2a).

The shell launchers (``install/clio`` / ``install/clio.ps1``) today own the only
implementation of "talk to the running server, or start one if none is up". This module
ports those semantics into Python so the CLI (Part 3) can call a single function instead
of shelling out to a platform-specific launcher.

Two entry points:

* :func:`ensure_server` — probe ``GET /v1/health``; **attach** (return the base URL) if a
  server already answers, otherwise **spawn** ``clio-agent-gact --port <port>`` detached,
  wait for health, and return the base URL. Only servers *we* spawn are recorded as
  managed-by-us in the pidfile.
* :func:`stop_server` — terminate the process tree of a server *we* spawned (never an
  attached external one) and clean up the pidfile. Idempotent.

Every outcome (attach / spawn / already-running / timeout / stop) is reported through a
closed, typed reason catalog (:data:`_SERVE_REASON_DEFINITIONS`) — mirroring the
``stream_fallback`` reason-catalog house style in :mod:`clio_agent.gact.streaming`. There
is no silent fallback: a spawn that never turns healthy raises :class:`ServerStartTimeout`
with its structured reason attached, and the partially-started process is torn down first.

The pidfile lives under the canonical per-user data dir
(``clio_agent.paths.user_data_dir()`` — e.g. ``%LOCALAPPDATA%\\clio-agent`` on Windows,
``~/.local/share/clio-agent`` on Linux), named ``gact-server-<port>.pid``, so distinct
ports never clobber each other's record. It is a small JSON blob guarded by a sibling
``filelock`` lock, carrying the PID, the process creation-time (a PID-reuse guard), the
host/port, and a ``spawned_by_us`` marker.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from filelock import FileLock

from clio_agent.paths import user_data_dir

logger = logging.getLogger(__name__)

_HEALTH_PATH = "/v1/health"
_SERVER_BIN = "clio-agent-gact"
#: HTTP status codes that mean "the server process is answering" (up), even when it
#: reports a degraded/unavailable subsystem. ``/v1/health`` returns 200 (ready/degraded)
#: or 503 (unavailable) — both prove the process is alive and serving.
_HEALTHY_STATUS = frozenset({200, 503})


# --- structured reason catalog (mirrors gact.streaming stream_fallback) -------
#
# A closed set of outcomes. Every public call resolves to exactly one of these; the
# payload is logged and (for the terminal case) carried on the raised error. Adding a new
# outcome means adding an entry here — an unknown reason is a programming error, not a
# silent default.
_SERVE_REASON_DEFINITIONS: dict[str, dict[str, Any]] = {
    "spawned": {
        "managed": True,
        "detail": "no server was running; launched a new gact server that became healthy",
    },
    "already_running": {
        "managed": True,
        "detail": "the gact server we previously spawned is already healthy; re-attached",
    },
    "attached_external": {
        "managed": False,
        "detail": "an external gact server was already healthy; attached without spawning",
    },
    "spawn_timeout": {
        "managed": False,
        "detail": "spawned server did not become healthy before the timeout; torn down",
    },
    "binary_not_found": {
        "managed": False,
        "detail": "the clio-agent-gact server binary could not be located on this system",
    },
    "stopped": {
        "managed": True,
        "detail": "terminated the gact server process tree we spawned; removed the pidfile",
    },
    "no_server": {
        "managed": False,
        "detail": "no pidfile recorded; nothing to stop",
    },
    "dead_pid": {
        "managed": False,
        "detail": "pidfile referenced a process that is no longer alive; cleaned it up",
    },
    "not_ours": {
        "managed": False,
        "detail": "recorded server was not spawned by us; left it running",
    },
    "unverifiable": {
        "managed": False,
        "detail": (
            "pidfile recorded no creation-time fingerprint; refused to kill an "
            "unverifiable PID (reuse-unsafe) and pruned the record"
        ),
    },
    "corrupt_pidfile": {
        "managed": False,
        "detail": (
            "pidfile was present but unparseable; pruned it — a managed process may "
            "have leaked and can no longer be identified from the record"
        ),
    },
}


def _serve_reason(reason: str, **extra: Any) -> dict[str, Any]:
    """Build a structured, catalog-validated reason payload.

    Args:
        reason: A key of :data:`_SERVE_REASON_DEFINITIONS`.
        **extra: Additional context fields (e.g. ``pid``, ``base_url``) merged in.

    Returns:
        The payload dict: ``{"reason", "managed", "detail", **extra}``.

    Raises:
        ValueError: If ``reason`` is not in the closed catalog.
    """
    definition = _SERVE_REASON_DEFINITIONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown serve reason: {reason}")
    return {"reason": reason, **definition, **extra}


class ServeError(RuntimeError):
    """Base class for connect-or-spawn failures, carrying a structured reason payload."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.reason = str(payload.get("reason", ""))
        super().__init__(payload.get("detail", self.reason))


class ServerBinaryNotFound(ServeError):
    """The ``clio-agent-gact`` server binary could not be located."""


class ServerStartTimeout(ServeError):
    """A spawned server did not become healthy before the timeout (and was torn down)."""


# --- last-action record (readable by Part 3 / tests) --------------------------
#
# ensure_server must return the base URL (a str) per its contract, so the structured
# outcome is stashed here in addition to being logged. Callers that want to know whether
# they attached vs spawned can read last_action() immediately after the call.
_LAST_ACTION: dict[str, Any] | None = None


def last_action() -> dict[str, Any] | None:
    """Return the structured reason payload from the most recent :func:`ensure_server`/
    :func:`stop_server` call in this process, or ``None`` if neither has run yet."""
    return _LAST_ACTION


def _record_action(payload: dict[str, Any]) -> dict[str, Any]:
    global _LAST_ACTION
    _LAST_ACTION = payload
    logger.info("serve action: %s", payload)
    return payload


# --- process helpers (portable; mirror arc.storage house style) ---------------


def server_base_url(host: str, port: int) -> str:
    """Return the canonical base URL for a gact server on ``host``/``port``."""
    return f"http://{host}:{port}"


def _detached_popen_kwargs() -> dict[str, Any]:
    """Popen kwargs that detach the server so it outlives the spawning process.

    POSIX: a new session (``setsid`` equivalent) so it is its own session/group leader and
    :func:`stop_server` can group-kill it plus every MCP child. Windows: a detached process
    in a new process group so a Ctrl-C / parent exit does not propagate to it.
    """
    if sys.platform.startswith("win"):
        flags = 0
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        return {"creationflags": flags, "close_fds": True}
    return {"start_new_session": True, "close_fds": True}


def _proc_create_time(pid: int) -> float | None:
    """Process creation time (epoch seconds) via psutil, or ``None`` if no such process.

    Used to defeat PID reuse: a recycled PID gets a different creation time, so a stale
    pidfile entry is not mistaken for our live server. psutil keeps this portable across
    Linux/macOS/Windows.
    """
    try:
        import psutil  # noqa: PLC0415

        return float(psutil.Process(pid).create_time())
    except Exception:  # noqa: BLE001 - NoSuchProcess/AccessDenied/import => "unknown"
        return None


def _pid_alive(pid: int, recorded_create_time: float | None) -> bool:
    """True if ``pid`` is alive AND (when known) its creation time matches the record."""
    try:
        import psutil  # noqa: PLC0415

        if not psutil.pid_exists(pid):
            return False
    except Exception:  # noqa: BLE001 - psutil missing => treat as conservatively alive
        return _proc_create_time(pid) is not None
    if recorded_create_time is None:
        return True
    current = _proc_create_time(pid)
    return current is not None and abs(current - recorded_create_time) < 1.0


def _server_bin() -> str:
    """Locate the ``clio-agent-gact`` console-script binary.

    Prefers the binary colocated with the running interpreter (the active venv's
    ``Scripts``/``bin`` dir), then falls back to ``PATH``.

    Raises:
        ServerBinaryNotFound: If the binary is nowhere to be found.
    """
    import shutil  # noqa: PLC0415

    exe = _SERVER_BIN + (".exe" if os.name == "nt" else "")
    colocated = Path(sys.executable).parent / exe
    if colocated.exists():
        return str(colocated)
    found = shutil.which(_SERVER_BIN)
    if found:
        return found
    raise ServerBinaryNotFound(_serve_reason("binary_not_found", searched=[str(colocated), "PATH"]))


# --- pidfile (JSON, filelock-guarded, under the canonical user data dir) -------


def _pidfile_path(port: int) -> Path:
    """Return the per-port pidfile path under the canonical user data dir."""
    root = user_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root / f"gact-server-{port}.pid"


def _pidfile_lock(pidfile: Path) -> FileLock:
    return FileLock(str(pidfile) + ".lock")


def _write_pidfile(
    pidfile: Path,
    *,
    pid: int,
    create_time: float | None,
    host: str,
    port: int,
    spawned_by_us: bool,
) -> None:
    """Atomically record a managed server in the pidfile (filelock-guarded)."""
    record = {
        "pid": pid,
        "create_time": create_time,
        "host": host,
        "port": port,
        "spawned_by_us": spawned_by_us,
    }
    with _pidfile_lock(pidfile):
        pidfile.write_text(json.dumps(record), encoding="utf-8")


def _read_pidfile(pidfile: Path) -> dict[str, Any] | None:
    """Read + parse the pidfile, or ``None`` if it is absent/garbled."""
    with _pidfile_lock(pidfile):
        try:
            raw = pidfile.read_text(encoding="utf-8")
        except OSError:
            return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _remove_pidfile(pidfile: Path) -> None:
    with _pidfile_lock(pidfile):
        with contextlib.suppress(OSError):
            pidfile.unlink()


# --- health probe -------------------------------------------------------------


def _probe_health(base_url: str, *, timeout_s: float = 1.0) -> bool:
    """Return True if the server answers ``/v1/health`` (any 200/503 == up).

    A refused connection / timeout / transport error means "not up" (returns False) — it
    never raises, so callers can treat the probe as a plain boolean gate.
    """
    try:
        resp = httpx.get(base_url + _HEALTH_PATH, timeout=timeout_s)
    except httpx.HTTPError:
        return False
    return resp.status_code in _HEALTHY_STATUS


# --- public API ---------------------------------------------------------------


def ensure_server(
    port: int = 8100,
    host: str = "127.0.0.1",
    *,
    timeout_s: float = 90.0,
) -> str:
    """Connect to a running gact server on ``host``/``port``, or spawn one if none is up.

    Probes ``GET /v1/health``. If a server answers (200 or 503), **attaches** and returns
    its base URL without spawning — an external server is left entirely alone (never
    recorded as ours, so :func:`stop_server` will not touch it). Otherwise spawns
    ``clio-agent-gact --port <port>`` **detached** (POSIX: new session; Windows: detached
    new process group), records it in the pidfile, and polls ``/v1/health`` until healthy
    or ``timeout_s`` elapses.

    Args:
        port: TCP port to probe/serve on. Defaults to 8100 (the gact default).
        host: Bind/probe host. Defaults to ``127.0.0.1``.
        timeout_s: Max seconds to wait for a freshly spawned server to become healthy.

    Returns:
        The server base URL (``http://<host>:<port>``). Inspect :func:`last_action` for the
        structured outcome (attached vs spawned vs already-running).

    Raises:
        ServerBinaryNotFound: If ``clio-agent-gact`` cannot be located.
        ServerStartTimeout: If a spawned server does not become healthy in time (the
            partially-started process is torn down before this is raised).
    """
    base_url = server_base_url(host, port)
    pidfile = _pidfile_path(port)

    # Already up? Attach without spawning. Distinguish our prior spawn from a stranger.
    if _probe_health(base_url):
        record = _read_pidfile(pidfile)
        if (
            record is not None
            and record.get("spawned_by_us")
            and isinstance(record.get("pid"), int)
            and _pid_alive(int(record["pid"]), record.get("create_time"))
        ):
            _record_action(_serve_reason("already_running", base_url=base_url, pid=record["pid"]))
        else:
            _record_action(_serve_reason("attached_external", base_url=base_url))
        return base_url

    # A single failed probe is not proof the server is down: a healthy server WE manage can
    # stall briefly (GC pause / load spike) within the probe timeout. Before discarding a
    # managed record and spawning a duplicate (which would fail to bind the still-held port
    # and orphan the original), re-probe with a bounded retry when the pidfile still points
    # at a live process we spawned.
    stale = _read_pidfile(pidfile)
    if (
        stale is not None
        and stale.get("spawned_by_us")
        and isinstance(stale.get("pid"), int)
        and _pid_alive(int(stale["pid"]), stale.get("create_time"))
    ):
        for _ in range(3):
            time.sleep(1.0)
            if _probe_health(base_url, timeout_s=2.0):
                _record_action(
                    _serve_reason("already_running", base_url=base_url, pid=stale["pid"])
                )
                return base_url
        # Still unhealthy after retries against a live managed PID: the process is wedged,
        # not merely slow. Tear it down (identity verified via create_time) before spawning a
        # replacement so the new server can bind the port.
        _terminate_tree(int(stale["pid"]), record_create_time=stale.get("create_time"))

    # No healthy server and no live managed process — drop any stale pidfile before spawning.
    _remove_pidfile(pidfile)

    server_bin = _server_bin()  # raises ServerBinaryNotFound (structured) if missing
    log_path = user_data_dir() / f"gact-server-{port}.log"
    log_fh = open(log_path, "ab")  # noqa: SIM115 - closed in finally after Popen
    try:
        proc = subprocess.Popen(
            [server_bin, "--host", host, "--port", str(port)],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            **_detached_popen_kwargs(),
        )
    finally:
        log_fh.close()

    _write_pidfile(
        pidfile,
        pid=proc.pid,
        create_time=_proc_create_time(proc.pid),
        host=host,
        port=port,
        spawned_by_us=True,
    )
    logger.info(
        "spawned gact server: %s --port %s (pid %s, log: %s)",
        server_bin,
        port,
        proc.pid,
        log_path,
    )

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _probe_health(base_url):
            _record_action(_serve_reason("spawned", base_url=base_url, pid=proc.pid))
            return base_url
        # Bail early if the process died outright (spawn crash) rather than waiting out
        # the full window on a corpse.
        if proc.poll() is not None:
            break
        time.sleep(0.5)

    # Timed out (or crashed): tear down what we spawned; no silent fallback.
    payload = _serve_reason(
        "spawn_timeout",
        base_url=base_url,
        pid=proc.pid,
        timeout_s=timeout_s,
        log=str(log_path),
    )
    _terminate_tree(proc.pid, record_create_time=None, trusted=True)
    _remove_pidfile(pidfile)
    _record_action(payload)
    raise ServerStartTimeout(payload)


def _terminate_tree(pid: int, *, record_create_time: float | None, trusted: bool = False) -> bool:
    """Terminate ``pid`` and its whole descendant tree (SIGTERM then SIGKILL).

    ``trusted=False`` (the :func:`stop_server` path, reading a persisted pidfile): the kill is
    PID-reuse-guarded by ``record_create_time`` — a recycled PID whose creation time no longer
    matches the record is left alone. ``trusted=True`` (the :func:`ensure_server` teardown path,
    where we still hold the live ``Popen`` and identity is certain): the liveness/identity gate
    is skipped, because a just-crashed leader is already dead yet its MCP children must still be
    reaped.

    On POSIX the spawned child is a session/group leader (``start_new_session``), so the whole
    process group is torn down by group id — this reaches every descendant and works **even
    after the leader itself has died**, the case a parent-tree walk (``proc.children()``) cannot
    handle. psutil is used as a belt-and-suspenders sweep, and as the primary path on Windows.

    Returns:
        True if a kill was attempted, False if the untrusted identity guard declined it.
    """
    if not trusted and not _pid_alive(pid, record_create_time):
        return False

    # POSIX: group-kill by pgid (== the spawned leader's pid). Survives leader death and reaches
    # every MCP child, so a crashed-parent teardown no longer orphans the tree.
    if not sys.platform.startswith("win"):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pid, sig)
            except (ProcessLookupError, OSError):
                break  # group already gone / not a group leader — fall through to psutil sweep
            if sig is signal.SIGTERM:
                time.sleep(1.0)

    # psutil sweep: catch any straggler not in the group (and the primary path on Windows). Safe
    # if the parent already vanished — enumeration simply yields nothing.
    try:
        import psutil  # noqa: PLC0415

        proc = psutil.Process(pid)
    except Exception:  # noqa: BLE001 - already gone (group-kill above handled it)
        return True
    try:
        children = proc.children(recursive=True)
    except Exception:  # noqa: BLE001 - process gone; nothing to enumerate
        children = []
    victims = [*children, proc]
    for victim in victims:
        with contextlib.suppress(Exception):
            victim.terminate()
    _, alive = _psutil_wait(victims, timeout=5.0)
    for victim in alive:
        with contextlib.suppress(Exception):
            victim.kill()
    return True


def _psutil_wait(procs: list[Any], *, timeout: float) -> tuple[list[Any], list[Any]]:
    """Thin wrapper over ``psutil.wait_procs`` returning ``(gone, alive)``."""
    try:
        import psutil  # noqa: PLC0415

        return psutil.wait_procs(procs, timeout=timeout)  # type: ignore[return-value]
    except Exception:  # noqa: BLE001 - psutil unavailable; treat all as still-alive
        return [], list(procs)


def stop_server(
    port: int = 8100,
    host: str = "127.0.0.1",
    *,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """Stop the gact server *we* spawned on ``port`` and clean up its pidfile. Idempotent.

    Reads the pidfile: if it is absent, references a dead process, or marks a server we did
    not spawn, this is a clean no-op that returns the corresponding structured note (and
    prunes a stale pidfile). Otherwise the recorded process tree is terminated (SIGTERM
    then SIGKILL), PID-reuse-guarded by the recorded creation time, and the pidfile removed.
    An **attached external** server is never recorded as ours, so this can never kill it.

    Args:
        port: Port whose recorded server should be stopped. Defaults to 8100.
        host: Unused for the kill (the PID is authoritative); kept for signature symmetry
            with :func:`ensure_server` and future host-scoped pidfiles.
        timeout_s: Reserved for future graceful-drain use; the terminate/kill escalation
            uses its own fixed 5s window.

    Returns:
        A structured note payload (see :data:`_SERVE_REASON_DEFINITIONS`): ``stopped``,
        ``no_server``, ``dead_pid``, ``unverifiable``, or ``not_ours``.
    """
    del host, timeout_s  # not load-bearing today; see docstring
    pidfile = _pidfile_path(port)
    record = _read_pidfile(pidfile)

    if record is None:
        # Distinguish a genuinely absent pidfile from a present-but-corrupt one: a torn
        # record for a server we spawned may mean we leaked a managed process we can no
        # longer identify, so it must NOT be reported as a clean no-op.
        if pidfile.exists():
            _remove_pidfile(pidfile)
            return _record_action(_serve_reason("corrupt_pidfile"))
        return _record_action(_serve_reason("no_server"))

    if not record.get("spawned_by_us"):
        return _record_action(_serve_reason("not_ours", pid=record.get("pid")))

    pid = record.get("pid")
    if not isinstance(pid, int):
        _remove_pidfile(pidfile)
        return _record_action(_serve_reason("no_server"))

    create_time = record.get("create_time")
    if create_time is None:
        # No creation-time fingerprint was recorded, so we cannot prove this PID is still our
        # server rather than an unrelated process that reused it. A wrong kill is worse than a
        # leaked server: refuse, and prune the unusable record.
        _remove_pidfile(pidfile)
        return _record_action(_serve_reason("unverifiable", pid=pid))
    if not _pid_alive(pid, create_time):
        _remove_pidfile(pidfile)
        return _record_action(_serve_reason("dead_pid", pid=pid))

    _terminate_tree(pid, record_create_time=create_time)
    _remove_pidfile(pidfile)
    return _record_action(_serve_reason("stopped", pid=pid))
