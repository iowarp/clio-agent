"""Session-level process-hygiene audit for the test suite (the daemon-ghost fix).

Tests that build ``make_arc_store(backend="cte")`` attach a clio-core client to the
shared per-user daemon: ``ClioCoreStore._ensure_runtime`` registers this process's PID
under ``~/.clio/clio-runtime.clients/`` and hands release to an ``atexit`` hook. That is
enough on a *clean* interpreter exit, but a hard-killed run (Ctrl-C, ``taskkill``, a
harness ``TaskStop``, an ``os._exit`` xdist worker) skips ``atexit`` and leaves a ghost
registration behind — and the shared daemon it keeps alive accretes state (the owner saw
it reach 12.3 GiB after hours of suite runs).

This module is the test-side guarantee that the suite cleans up after itself:

1. :meth:`ProcessHygieneAudit.release` calls the *real* clean-release path
   (:func:`clio_agent.arc.storage.release_runtime_client`) at session end — a
   deterministic release that does not depend on ``atexit`` firing.
2. :meth:`ProcessHygieneAudit.finalize` runs a **leak audit**: any clio-core client that
   THIS run's process family registered and then left dead (registered-but-never-
   deregistered), plus any helper child process left running at session end, is reported
   with the culprit test that introduced it, so the run FAILS loudly and names names.

Scope + false-positive safety (this box runs parallel CLIO instances — "one tree, one
writer"): the client audit only tracks new registry entries that are **descendants of
this pytest process** at the moment they appear. A parallel instance's own client (a
sibling PID) is never attributed to us, so a parallel run finishing mid-session cannot
turn our audit red. Entries present at session start (a prior run's, or a live parallel
instance's) are baselined out.

Opt out — emergencies only, documented — with ``CLIO_TEST_SKIP_CLIENT_AUDIT=1``.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from typing import Callable, Optional

SKIP_ENV = "CLIO_TEST_SKIP_CLIENT_AUDIT"


@dataclass(frozen=True)
class LeakedClient:
    """A clio-core client this run registered that was never deregistered."""

    pid: int
    origin: str  # the test nodeid that first introduced it (or "<unknown>")


@dataclass(frozen=True)
class LeakedChild:
    """A helper child process left alive at session end."""

    pid: int
    name: str
    origin: str
    #: Best-effort forensics captured at finalize while the process is still
    #: alive (cmdline/ppid/age) — "<unknown>"-origin leaks come from background
    #: threads the per-test diff cannot attribute, so this is the only handle
    #: CI gives us on the culprit (#1240).
    detail: str = ""


@dataclass(frozen=True)
class AuditResult:
    """Outcome of the session-end process-hygiene audit."""

    client_leaks: tuple[LeakedClient, ...]
    child_leaks: tuple[LeakedChild, ...]

    @property
    def clean(self) -> bool:
        """True when nothing leaked."""
        return not self.client_leaks and not self.child_leaks

    def format_failure(self) -> str:
        """Render a loud, culprit-naming failure message (never called when clean)."""
        lines = ["process-hygiene audit FAILED — the test suite leaked OS resources:"]
        for leak in self.client_leaks:
            lines.append(
                f"  clio-core client leak: pid={leak.pid} registered under "
                f"~/.clio/clio-runtime.clients/ but the process died without "
                f"deregistering (introduced by {leak.origin})"
            )
        for child in self.child_leaks:
            lines.append(
                f"  child process leak: pid={child.pid} name={child.name!r} still alive "
                f"at session end (introduced by {child.origin}){child.detail}"
            )
        lines.append(
            f"  set {SKIP_ENV}=1 to bypass this audit in an emergency (and file the leak)."
        )
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Registry / process snapshots (thin wrappers over arc.storage's own seams).   #
# --------------------------------------------------------------------------- #


def _storage():  # type: ignore[no-untyped-def]
    """Import arc.storage lazily so this module is importable binding-free."""
    from clio_agent.arc import storage  # noqa: PLC0415

    return storage


def client_registry_snapshot() -> dict[int, Optional[float]]:
    """Return ``{pid: recorded_create_time}`` for every clio-core client registration.

    Reads the same ``~/.clio/clio-runtime.clients/`` directory the runtime registers
    into. Best-effort: a missing directory or an unreadable entry yields an empty/partial
    map rather than raising (the audit is visibility, not a control path).
    """
    storage = _storage()
    reg = storage._client_registry_dir()
    out: dict[int, Optional[float]] = {}
    if not reg.is_dir():
        return out
    for entry in reg.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = entry.read_text(encoding="utf-8").strip()
            ctime: Optional[float] = float(raw) if raw else None
        except (OSError, ValueError):
            ctime = None
        out[int(entry.name)] = ctime
    return out


def entry_is_dead(pid: int, recorded_ctime: Optional[float]) -> bool:
    """True when the registered ``pid`` is no longer alive (a stale ghost entry)."""
    return not _storage()._pid_alive(pid, recorded_ctime)


def daemon_pid() -> Optional[int]:
    """Return the shared clio-core daemon PID from its pidfile, or ``None``."""
    storage = _storage()
    try:
        parts = storage._daemon_pidfile().read_text(encoding="utf-8").split()
    except OSError:
        return None
    if not parts:
        return None
    try:
        return int(parts[0])
    except ValueError:
        return None


def _psutil():  # type: ignore[no-untyped-def]
    """Return the psutil module, or ``None`` if unavailable."""
    try:
        import psutil  # noqa: PLC0415

        return psutil
    except ImportError:
        return None


def _leak_forensics(pid: int) -> str:
    """Best-effort cmdline/parent/age of a still-alive leaked child (#1240).

    Called at finalize, while the leak is provably alive, so the failure
    message can NAME the culprit that per-test diffing could not attribute
    (background-thread spawns land between snapshots). Never raises.
    """
    psutil = _psutil()
    if psutil is None:
        return ""
    try:
        proc = psutil.Process(pid)
        with proc.oneshot():
            cmdline = " ".join(proc.cmdline())[:300]
            parent = proc.parent()
            parent_text = f"{parent.pid}:{parent.name()}" if parent is not None else "<gone>"
            age_s = max(0.0, _time.time() - proc.create_time())
        return f" cmdline={cmdline!r} parent={parent_text} age={age_s:.0f}s"
    except Exception:  # noqa: BLE001 - forensics are best-effort, never sink the audit
        return ""


def child_snapshot(root_pid: int, *, exclude_subtree: Optional[int] = None) -> dict[int, str]:
    """Return ``{pid: name}`` for live descendants of ``root_pid``.

    ``exclude_subtree`` (the shared clio-core daemon PID) and its own descendants are
    excluded: the daemon is the DELIBERATE second process-tree root (spawned with
    ``CREATE_BREAKAWAY_FROM_JOB``) and is released by refcount, not owned by any one test.
    A psutil-less environment yields an empty map (best-effort visibility).
    """
    psutil = _psutil()
    if psutil is None:
        return {}
    try:
        proc = psutil.Process(root_pid)
        descendants = proc.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return {}

    excluded: set[int] = set()
    if exclude_subtree is not None:
        excluded.add(exclude_subtree)
        try:
            daemon = psutil.Process(exclude_subtree)
            excluded.update(c.pid for c in daemon.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    out: dict[int, str] = {}
    for child in descendants:
        if child.pid in excluded:
            continue
        try:
            # CPython's own multiprocessing.resource_tracker is exempt (#1240,
            # named by the audit's own forensics): the stdlib spawns it lazily
            # ONCE per interpreter the first time any test touches
            # multiprocessing primitives, it is owned by the pytest process
            # itself, and it CANNOT be released by a test -- killing it would
            # break every later multiprocessing use. It dies with the
            # interpreter; flagging it is a false positive, not a leak.
            if "multiprocessing.resource_tracker" in " ".join(child.cmdline()):
                continue
            out[child.pid] = child.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def is_descendant_of(pid: int, root_pid: int, *, max_depth: int = 40) -> bool:
    """True when live ``pid`` has ``root_pid`` somewhere in its parent chain.

    Used to attribute a freshly-appeared client registration to THIS process family
    (and only this family), so a parallel CLIO instance's sibling client is never
    mistaken for our leak. A dead/vanished pid yields ``False``.
    """
    psutil = _psutil()
    if psutil is None:
        return False
    try:
        proc: object = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    for _ in range(max_depth):
        try:
            parent = proc.parent()  # type: ignore[attr-defined]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        if parent is None:
            return False
        if parent.pid == root_pid:
            return True
        proc = parent
    return False


# --------------------------------------------------------------------------- #
# The audit                                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class ProcessHygieneAudit:
    """Track clio-core clients + helper children a run introduces and audit at the end.

    Construct at session start (baseline snapshot), call :meth:`observe_test` at each
    test teardown (cheap registry diff → attribution), and :meth:`finalize` once at
    session end.
    """

    root_pid: int
    _snapshot_clients: Callable[[], dict[int, Optional[float]]] = client_registry_snapshot
    _snapshot_children: Callable[[int], dict[int, str]] = field(
        default=lambda root: child_snapshot(root)
    )
    _is_descendant: Callable[[int, int], bool] = is_descendant_of
    _is_dead: Callable[[int, Optional[float]], bool] = staticmethod(entry_is_dead)

    def __post_init__(self) -> None:
        self._baseline_clients: set[int] = set(self._snapshot_clients())
        self._baseline_children: set[int] = set(self._snapshot_children(self.root_pid))
        # pid -> nodeid that first introduced it (only OUR descendants are tracked).
        self._client_origin: dict[int, str] = {}
        self._child_origin: dict[int, str] = {}

    def observe_test(self, nodeid: str) -> None:
        """Attribute newly-appeared registry entries to ``nodeid`` (called per test).

        Only entries that are descendants of this pytest process are tracked, so a
        parallel CLIO instance's client can never be attributed to us. The client
        registry scan is a sub-millisecond ``listdir``; no psutil child scan runs here.
        """
        for pid in self._snapshot_clients():
            if pid in self._baseline_clients or pid in self._client_origin:
                continue
            if pid == self.root_pid or self._is_descendant(pid, self.root_pid):
                self._client_origin[pid] = nodeid

    def finalize(self, *, own_pid: int) -> AuditResult:
        """Audit the run: dead client registrations + surviving helper children.

        Args:
            own_pid: This pytest process's PID; excluded from client leaks (it is alive
                at finalize and released via :func:`release`).
        """
        snapshot = self._snapshot_clients()
        client_leaks: list[LeakedClient] = []
        for pid, origin in sorted(self._client_origin.items()):
            if pid == own_pid or pid not in snapshot:
                continue
            if self._is_dead(pid, snapshot[pid]):
                client_leaks.append(LeakedClient(pid=pid, origin=origin))

        child_leaks: list[LeakedChild] = []
        current = self._snapshot_children(self.root_pid)
        for pid, name in sorted(current.items()):
            if pid in self._baseline_children:
                continue
            child_leaks.append(
                LeakedChild(
                    pid=pid,
                    name=name,
                    origin=self._child_origin.get(pid, "<unknown>"),
                    detail=_leak_forensics(pid),
                )
            )
        return AuditResult(client_leaks=tuple(client_leaks), child_leaks=tuple(child_leaks))


def release_this_process_client() -> None:
    """Deterministically release THIS process's clio-core client (session-end teardown).

    Calls the real last-one-out release path so the pytest host deregisters even when
    ``atexit`` would not fire (hard kill / ``os._exit`` worker). Idempotent and a no-op
    when this process never attached a client. Never raises: a release failure must not
    mask the test results.
    """
    try:
        _storage().release_runtime_client()
    except Exception:  # noqa: BLE001 - teardown must not raise; a leak, if any, still audits
        pass
