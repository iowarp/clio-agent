"""Process-tree, port-discovery, and OS-level assertion helpers for the desktop
lifecycle proof (#B11, ``scripts/live_verification/desktop_lifecycle_proof.py``).

Every decision here is split into a PURE half (plain dataclasses in, plain data
out -- no psutil calls, no OS access) and a thin REAL wrapper that calls psutil
and hands the pure half its inputs. This is what lets
``tests/test_scripts/test_desktop_lifecycle_proof.py`` exercise the actual
decision logic -- owned-tree filtering, GACT port discovery, preflight conflict
detection, Quit-CLIO teardown assertions -- against synthetic process/
connection snapshots. The live OS calls themselves (:func:`list_all_processes`,
:func:`list_connections_for_pids`) are exercised only by a live run against the
real installed app.

Standalone module: no sibling flat-imports (unlike ``desktop_lifecycle_proof.py``,
which flat-imports ``_common``/``_desktop_cdp``/this module as siblings
following the package's existing convention -- see ``_common.py``'s own
``sys.path.insert`` + ``import _common as common`` pattern), so this module
loads cleanly under ``importlib`` with no ``sys.path`` surgery.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Container, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import psutil

#: Process names the installed desktop app's owned tree is expected to be made
#: of (see the task's "Desktop facts"): the Tauri shell, the Go launcher, the
#: bundled Python GACT server, and (unless ``--allow-shared-core``) the shared
#: clio-core runtime daemon.
DESKTOP_PROCESS_NAME = "clio-desktop.exe"
LAUNCHER_PROCESS_GLOB = "clio-agent-*.exe"
PYTHON_PROCESS_NAME = "python.exe"
CTE_DAEMON_PROCESS_NAME = "clio_run.exe"

#: Interactive shells the owned tree must never leave behind (a debug console
#: or a stray elevation-prompt shell would show up here).
PTY_SHELL_NAMES = frozenset({"pwsh.exe", "powershell.exe", "cmd.exe"})


@dataclass(frozen=True)
class ProcSnapshot:
    """A single process, as of one point-in-time OS scan."""

    pid: int
    ppid: int
    name: str
    exe: str = ""
    cmdline: tuple[str, ...] = ()
    create_time: float = 0.0


@dataclass(frozen=True)
class ConnSnapshot:
    """A single TCP connection, as of one point-in-time OS scan."""

    pid: int
    status: str
    ip: str
    port: int


# --------------------------------------------------------------------------- #
# Pure: owned-tree filtering
# --------------------------------------------------------------------------- #
def descendant_pids(root_pid: int, processes: Sequence[ProcSnapshot]) -> list[int]:
    """Every pid whose parent chain reaches ``root_pid`` (BFS, ``root_pid`` excluded).

    Pure: works over a plain snapshot list, not a live psutil.Process handle,
    so it still finds descendants even if ``root_pid`` itself has already
    exited (``psutil.Process(root_pid).children(recursive=True)`` cannot -- it
    needs a live handle) and is directly unit-testable against synthetic data.
    """

    by_ppid: dict[int, list[int]] = {}
    for proc in processes:
        by_ppid.setdefault(proc.ppid, []).append(proc.pid)

    seen = {root_pid}
    ordered: list[int] = []
    frontier = [root_pid]
    while frontier:
        next_frontier: list[int] = []
        for pid in frontier:
            for child in by_ppid.get(pid, []):
                if child in seen:
                    continue
                seen.add(child)
                ordered.append(child)
                next_frontier.append(child)
        frontier = next_frontier
    return ordered


def owned_tree(root_pid: int, processes: Sequence[ProcSnapshot]) -> list[ProcSnapshot]:
    """:func:`descendant_pids`, resolved back to full snapshots, root first."""

    by_pid = {proc.pid: proc for proc in processes}
    root = by_pid.get(root_pid)
    tree = [root] if root is not None else []
    tree.extend(by_pid[pid] for pid in descendant_pids(root_pid, processes) if pid in by_pid)
    return tree


def _name_matches(name: str, pattern: str) -> bool:
    return fnmatch.fnmatch(name.lower(), pattern.lower())


def find_by_name(processes: Sequence[ProcSnapshot], pattern: str) -> list[ProcSnapshot]:
    """Every process whose name glob-matches ``pattern`` (case-insensitive)."""

    return [p for p in processes if _name_matches(p.name, pattern)]


def find_leftover_pty_shells(
    processes: Sequence[ProcSnapshot], ever_seen_pids: Container[int]
) -> list[ProcSnapshot]:
    """PTY shells parented by a pid this run ever saw in the owned tree.

    A shell an app spawns legitimately elsewhere (the user's own terminal) is
    never flagged -- only one whose ``ppid`` traces back to something THIS run
    recorded as part of the desktop's process tree at some point
    (``ever_seen_pids`` accumulates across every snapshot the script takes, so
    a shell that outlives its immediate parent's own teardown is still
    caught).
    """

    return [p for p in processes if p.name.lower() in PTY_SHELL_NAMES and p.ppid in ever_seen_pids]


# --------------------------------------------------------------------------- #
# Pure: preflight conflict detection
# --------------------------------------------------------------------------- #
def find_preflight_conflicts(
    processes: Sequence[ProcSnapshot],
    *,
    install_dir: str,
    allow_shared_core: bool,
) -> list[ProcSnapshot]:
    """Every already-running process the preflight step must refuse to proceed past.

    ``python.exe`` only counts as a conflict when its ``exe`` resolves under
    ``install_dir`` (the bundled interpreter) -- an unrelated ``python.exe`` on
    the same machine (this very script's own interpreter, a dev venv, ...) is
    not CLIO and must never block the proof.
    """

    install_prefix = install_dir.replace("\\", "/").rstrip("/").lower()
    conflicts: list[ProcSnapshot] = []
    for proc in processes:
        name = proc.name.lower()
        if _name_matches(name, DESKTOP_PROCESS_NAME):
            conflicts.append(proc)
        elif _name_matches(name, LAUNCHER_PROCESS_GLOB):
            conflicts.append(proc)
        elif _name_matches(name, PYTHON_PROCESS_NAME):
            exe = proc.exe.replace("\\", "/").lower()
            if install_prefix and exe.startswith(install_prefix):
                conflicts.append(proc)
        elif not allow_shared_core and _name_matches(name, CTE_DAEMON_PROCESS_NAME):
            conflicts.append(proc)
    return conflicts


# --------------------------------------------------------------------------- #
# Pure: GACT server identification + port discovery
# --------------------------------------------------------------------------- #
def is_gact_server_process(name: str, cmdline: Sequence[str]) -> bool:
    """True for the bundled ``python.exe -m clio_agent.gact --no-agent`` child."""

    if not name.lower().startswith("python"):
        return False
    return "clio_agent.gact" in " ".join(cmdline)


def find_gact_server_pid(processes: Sequence[ProcSnapshot]) -> int | None:
    """The one process matching :func:`is_gact_server_process`, if any."""

    for proc in processes:
        if is_gact_server_process(proc.name, proc.cmdline):
            return proc.pid
    return None


def discover_gact_port(
    connections: Iterable[ConnSnapshot], owned_pids: Container[int]
) -> int | None:
    """The first loopback ``LISTEN`` port owned by one of ``owned_pids``.

    Pure: takes an already-collected connection snapshot (real callers build
    one via a per-pid psutil scan over the owned python.exe child -- see
    :func:`list_connections_for_pids`), so WHICH port is the GACT server's is
    unit-testable against a fabricated connection list.
    """

    for conn in connections:
        if conn.pid in owned_pids and conn.status == "LISTEN" and conn.ip in ("127.0.0.1", "::1"):
            return conn.port
    return None


# --------------------------------------------------------------------------- #
# Pure: Quit-CLIO teardown assertion logic
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TeardownCheck:
    """Inputs to :func:`assert_quit_teardown`, as of one final snapshot."""

    owned_pids: tuple[int, ...]
    alive_pids: frozenset[int]
    python_pid: int
    registry_pids: frozenset[int]
    clio_run_pid: int | None
    allow_shared_core: bool
    leftover_pty_shells: tuple[str, ...] = ()


def assert_quit_teardown(check: TeardownCheck) -> list[str]:
    """Every teardown violation found in ``check`` (empty list == pass)."""

    violations: list[str] = []
    for pid in check.owned_pids:
        if pid in check.alive_pids:
            violations.append(f"owned pid {pid} is still alive after Quit CLIO")
    if check.python_pid in check.registry_pids:
        violations.append(
            f"runtime-client registry still has an entry for python pid {check.python_pid}"
        )
    if check.allow_shared_core:
        if check.clio_run_pid is not None and check.clio_run_pid not in check.alive_pids:
            violations.append(
                "clio_run.exe exited despite --allow-shared-core (expected to stay alive)"
            )
    elif check.clio_run_pid is not None and check.clio_run_pid in check.alive_pids:
        violations.append("clio_run.exe is still alive (expected torn down with no other client)")
    violations.extend(f"leftover PTY shell: {name}" for name in check.leftover_pty_shells)
    return violations


# --------------------------------------------------------------------------- #
# Real: thin psutil wrappers (not unit-tested directly; exercised only by a
# live run against the real installed app)
# --------------------------------------------------------------------------- #
def list_all_processes() -> list[ProcSnapshot]:
    """A best-effort, whole-system point-in-time process snapshot."""

    snapshots: list[ProcSnapshot] = []
    for proc in psutil.process_iter(["pid", "ppid", "name", "exe", "cmdline", "create_time"]):
        try:
            info = proc.info
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        snapshots.append(
            ProcSnapshot(
                pid=int(info.get("pid") or 0),
                ppid=int(info.get("ppid") or 0),
                name=str(info.get("name") or ""),
                exe=str(info.get("exe") or ""),
                cmdline=tuple(info.get("cmdline") or ()),
                create_time=float(info.get("create_time") or 0.0),
            )
        )
    return snapshots


def list_connections_for_pids(pids: Iterable[int]) -> list[ConnSnapshot]:
    """Loopback TCP connections owned by each of ``pids`` (per-pid scan).

    Per-pid ``Process.net_connections()`` (not the system-wide
    ``psutil.net_connections()``) deliberately: it only needs access to
    processes this script itself spawned (a descendant of the current user's
    own session), so it works without elevation, unlike enumerating every
    socket on the machine.
    """

    conns: list[ConnSnapshot] = []
    for pid in pids:
        try:
            raw = psutil.Process(pid).net_connections(kind="inet")
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
        for entry in raw:
            addr = _laddr_ip_port(entry.laddr)
            if addr is None:
                continue
            conns.append(ConnSnapshot(pid=pid, status=str(entry.status), ip=addr[0], port=addr[1]))
    return conns


def _laddr_ip_port(laddr: Any) -> tuple[str, int] | None:
    """Normalize a psutil ``addr`` namedtuple (or a plain 2-tuple, for tests)."""

    if not laddr:
        return None
    ip = getattr(laddr, "ip", None)
    port = getattr(laddr, "port", None)
    if ip is not None and port is not None:
        return str(ip), int(port)
    try:
        return str(laddr[0]), int(laddr[1])
    except (TypeError, IndexError, ValueError):
        return None


def is_process_alive(pid: int) -> bool:
    return psutil.pid_exists(pid)
