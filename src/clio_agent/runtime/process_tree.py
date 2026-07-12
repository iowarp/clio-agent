"""Bind CLIO's child-process tree to the server's lifetime (#900).

One clio server fans out into ~7 processes: the ``uv``/``python`` (uvicorn) parent,
its ``clio-kit`` MCP stdio children, and the pooled Claude SDK ``claude`` CLI
process(es). On Windows, terminating the parent does **not** terminate that tree —
a crash, ``taskkill`` without ``/T``, a harness ``TaskStop``, or a Ctrl-C race
orphans the children, which then idle forever (the recurring ``uv.exe`` / ``python.exe``
pile-up the owner observed).

This module is the single owner of the OS-level guarantee that the tree dies with the
server even on a *hard* kill:

* **Windows** — a Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` is created and
  the current process assigned to it. Every child the server spawns *inherits* the job,
  so when the server process exits — cleanly or hard — the OS closes its last job handle
  and reaps every still-associated process. ``JOB_OBJECT_LIMIT_BREAKAWAY_OK`` is set so
  the deliberately-detached shared clio-core daemon (spawned with
  ``CREATE_BREAKAWAY_FROM_JOB`` in :func:`clio_agent.arc.storage._detached_popen_kwargs`)
  stays **outside** the job and survives a server kill — it is shared across clients and
  must not die with any one of them.
* **POSIX** — parent-death is already enforced per-child: MCP stdio children ride
  ``setpriv --pdeathsig SIGKILL`` (:func:`clio_agent.tools.mcp_config.pdeathsig_wrapped_command`)
  and the spawned server is a process-group leader that
  :func:`clio_agent.serve._terminate_tree` group-kills. There is no process-wide Job
  Object analogue to install here, so this module reports the delegated mechanism
  honestly (no silent no-op).

It also owns the *clean-shutdown* teardown of the pooled Claude SDK transports
(:func:`teardown_pooled_sdk_transports`) and a doctor probe that lists the live child
processes so leakage is visible (:func:`probe_process_tree`).
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from clio_agent.runtime.status import IntegrationState, IntegrationStatus

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Windows Job Object flags (winnt.h). Kept as a module constant so a test can  #
# pin the exact limit set without booting Windows.                            #
# --------------------------------------------------------------------------- #
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
# KILL_ON_JOB_CLOSE reaps the tree with the server; BREAKAWAY_OK lets the shared
# clio-core daemon leave the job (it is spawned with CREATE_BREAKAWAY_FROM_JOB and
# must outlive any single server).
WINDOWS_JOB_LIMIT_FLAGS = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_BREAKAWAY_OK

# JobObjectExtendedLimitInformation info-class ordinal (SetInformationJobObject).
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

# The Windows job handle is stashed here so it is NOT garbage-collected: closing it
# would fire KILL_ON_JOB_CLOSE early and reap the live tree. It must stay open for the
# whole process lifetime; the OS closes it on process exit (which is what we want).
_JOB_HANDLE: Any = None

# Mechanism labels (typed, so the doctor / trace never guess).
MECHANISM_WINDOWS_JOB = "windows_job_object"
MECHANISM_POSIX_DELEGATED = "posix_pdeathsig_process_group"
MECHANISM_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ChildReaperResult:
    """Outcome of installing the process-tree reaper (a typed, loggable reason).

    Attributes:
        mechanism: Which mechanism governs child-reaping in this process
            (:data:`MECHANISM_WINDOWS_JOB` / :data:`MECHANISM_POSIX_DELEGATED` /
            :data:`MECHANISM_UNAVAILABLE`).
        active: Whether an OS-level kill-on-parent-death binding was installed by
            THIS module (True only for a successfully assigned Windows Job Object).
            POSIX is ``False`` because the guarantee is delegated to per-child
            pdeathsig + the process-group teardown, not installed here.
        reason: A short machine-stable reason token (e.g. ``job_assigned``,
            ``job_create_failed``, ``delegated_to_pdeathsig``).
        details: Structured evidence (winerror, platform, pid, ...).
    """

    mechanism: str
    active: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    """winnt.h JOBOBJECT_BASIC_LIMIT_INFORMATION (only LimitFlags is load-bearing here)."""

    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    )


class _IO_COUNTERS(ctypes.Structure):
    """winnt.h IO_COUNTERS — an opaque padding member of the extended-limit struct."""

    _fields_ = (
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    )


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    """winnt.h JOBOBJECT_EXTENDED_LIMIT_INFORMATION passed to SetInformationJobObject."""

    _fields_ = (
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


def _install_windows_job() -> ChildReaperResult:
    """Create a KILL_ON_JOB_CLOSE Job Object and assign the current process to it.

    Returns a typed result either way — a failed create/assign is logged with a
    structured reason and surfaced (no silent fallback), never raised, so a server on
    a locked-down Windows host still boots (it simply loses the hard-kill guarantee,
    which the doctor row then makes visible).
    """
    global _JOB_HANDLE
    from ctypes import wintypes  # noqa: PLC0415 - Windows type aliases, resolved lazily

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        err = ctypes.get_last_error()
        logger.error("child-tree reaper install failed reason=job_create_failed winerror=%s", err)
        return ChildReaperResult(
            mechanism=MECHANISM_UNAVAILABLE,
            active=False,
            reason="job_create_failed",
            details={"platform": sys.platform, "winerror": err},
        )

    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = WINDOWS_JOB_LIMIT_FLAGS
    ok = kernel32.SetInformationJobObject(
        handle,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        err = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        logger.error(
            "child-tree reaper install failed reason=job_set_information_failed winerror=%s", err
        )
        return ChildReaperResult(
            mechanism=MECHANISM_UNAVAILABLE,
            active=False,
            reason="job_set_information_failed",
            details={"platform": sys.platform, "winerror": err},
        )

    ok = kernel32.AssignProcessToJobObject(handle, kernel32.GetCurrentProcess())
    if not ok:
        err = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        logger.error("child-tree reaper install failed reason=job_assign_failed winerror=%s", err)
        return ChildReaperResult(
            mechanism=MECHANISM_UNAVAILABLE,
            active=False,
            reason="job_assign_failed",
            details={"platform": sys.platform, "winerror": err},
        )

    _JOB_HANDLE = handle  # keep the handle open for the process lifetime (see module note)
    logger.info(
        "child-tree reaper active reason=job_assigned mechanism=%s limit_flags=0x%x",
        MECHANISM_WINDOWS_JOB,
        WINDOWS_JOB_LIMIT_FLAGS,
    )
    return ChildReaperResult(
        mechanism=MECHANISM_WINDOWS_JOB,
        active=True,
        reason="job_assigned",
        details={
            "platform": sys.platform,
            "pid": os.getpid(),
            "limit_flags": WINDOWS_JOB_LIMIT_FLAGS,
        },
    )


def install_child_reaper() -> ChildReaperResult:
    """Install the OS binding that reaps CLIO's child tree when the server dies.

    Idempotent on Windows: a second call returns the already-installed result without
    creating a second job. Safe to call at server startup from the gact lifespan.

    Returns:
        A :class:`ChildReaperResult` describing the mechanism and whether an OS-level
        binding was installed (Windows Job Object) or delegated (POSIX pdeathsig +
        process-group teardown). The result is also cached for :func:`child_reaper_status`.
    """
    global _LAST_RESULT
    if sys.platform.startswith("win"):
        if _JOB_HANDLE is not None:
            # Already assigned this process; report the standing binding.
            _LAST_RESULT = ChildReaperResult(
                mechanism=MECHANISM_WINDOWS_JOB,
                active=True,
                reason="job_already_assigned",
                details={"platform": sys.platform, "pid": os.getpid()},
            )
            return _LAST_RESULT
        try:
            _LAST_RESULT = _install_windows_job()
        except OSError as exc:
            logger.error(
                "child-tree reaper install failed reason=job_install_oserror error=%r", exc
            )
            _LAST_RESULT = ChildReaperResult(
                mechanism=MECHANISM_UNAVAILABLE,
                active=False,
                reason="job_install_oserror",
                details={"platform": sys.platform, "error": repr(exc)},
            )
        return _LAST_RESULT

    # POSIX: the guarantee is delegated per-child (pdeathsig) + process-group teardown.
    logger.info(
        "child-tree reaper delegated reason=delegated_to_pdeathsig mechanism=%s",
        MECHANISM_POSIX_DELEGATED,
    )
    _LAST_RESULT = ChildReaperResult(
        mechanism=MECHANISM_POSIX_DELEGATED,
        active=False,
        reason="delegated_to_pdeathsig",
        details={
            "platform": sys.platform,
            "pid": os.getpid(),
            "note": (
                "MCP children ride setpriv --pdeathsig SIGKILL; the server is a "
                "process-group leader torn down by clio_agent.serve._terminate_tree."
            ),
        },
    )
    return _LAST_RESULT


# The last install result, for the doctor probe (meaningful only IN the process that
# installed it — a standalone doctor CLI holds none and reports no reaper row).
_LAST_RESULT: ChildReaperResult | None = None


def child_reaper_status() -> ChildReaperResult | None:
    """Return the reaper result installed in THIS process, or None if never installed."""
    return _LAST_RESULT


def teardown_pooled_sdk_transports() -> dict[str, str]:
    """Close the pooled Claude SDK transports on clean shutdown (#900).

    The blocking SDK session pool and the streaming client pool each hold persistent
    ``claude`` CLI connections + a loop thread. They register ``atexit`` best-effort
    closers, but this promotes teardown to an explicit, typed-logged step on the gact
    lifespan shutdown so the CLI process(es) are reaped promptly (not only at
    interpreter exit) and the outcome reaches the trace. Idempotent — a later ``atexit``
    run is a safe no-op. Never raises: a per-pool failure is logged with a structured
    reason and recorded in the returned map.

    Returns:
        A ``{pool_name: outcome}`` map where each outcome is ``"closed"`` or
        ``"error:<repr>"``.
    """
    results: dict[str, str] = {}

    try:
        from clio_agent.providers.claude_code_sessions import (  # noqa: PLC0415
            _STREAM_CLIENT_POOL,
        )

        _STREAM_CLIENT_POOL.close_blocking()
        results["stream_client_pool"] = "closed"
    except Exception as exc:  # noqa: BLE001 - teardown must not raise; reason logged + recorded
        logger.warning(
            "sdk transport teardown failed reason=sdk_stream_pool_close_failed error=%r", exc
        )
        results["stream_client_pool"] = f"error:{exc!r}"

    try:
        from clio_agent.providers.claude_code_sdk_pool import _SDK_SESSION_POOL  # noqa: PLC0415

        _SDK_SESSION_POOL.close()
        results["sdk_session_pool"] = "closed"
    except Exception as exc:  # noqa: BLE001 - teardown must not raise; reason logged + recorded
        logger.warning(
            "sdk transport teardown failed reason=sdk_session_pool_close_failed error=%r", exc
        )
        results["sdk_session_pool"] = f"error:{exc!r}"

    try:
        from clio_agent.providers.codex_app_server import _APP_SERVER_POOL  # noqa: PLC0415

        _APP_SERVER_POOL.close_blocking()
        results["codex_app_server_pool"] = "closed"
    except Exception as exc:  # noqa: BLE001 - teardown must not raise; reason logged + recorded
        logger.warning(
            "sdk transport teardown failed reason=codex_app_server_pool_close_failed error=%r", exc
        )
        results["codex_app_server_pool"] = f"error:{exc!r}"

    logger.info(
        "pooled SDK transports torn down on shutdown reason=sdk_pools_closed outcome=%s", results
    )
    return results


def close_tool_executors(executors: Iterable[Any]) -> None:
    """Close each MCP tool executor, reaping its stdio children + loop thread (#900).

    Best-effort per executor: a failure is logged with a structured reason and teardown
    continues to the next, so one wedged executor cannot block the rest. Used by
    :meth:`clio_agent.agent.ClioAgent.shutdown`.
    """
    for executor in executors:
        close = getattr(executor, "close", None)
        if not callable(close):
            continue
        try:
            close()
        except Exception as exc:  # noqa: BLE001 - one bad executor must not block teardown
            logger.warning(
                "tool executor close failed reason=tool_executor_close_failed error=%r", exc
            )


def shutdown_child_processes(agent: Any) -> dict[str, str]:
    """Reap the whole child tree on a CLEAN server shutdown (agent + SDK pools) (#900).

    Closes the agent's persistent MCP tool executors (via ``agent.shutdown``) and the
    pooled Claude SDK transports, each with typed logging. Blocking (thread joins) — the
    gact lifespan runs it off the event loop. Never raises: a failure in ``agent.shutdown``
    is logged with a structured reason so the SDK-pool teardown still runs.

    Args:
        agent: The live ``ClioAgent`` (or ``None`` for an agent-less server).

    Returns:
        The per-pool teardown outcome map from :func:`teardown_pooled_sdk_transports`.
    """
    agent_shutdown = getattr(agent, "shutdown", None)
    if callable(agent_shutdown):
        try:
            agent_shutdown()
        except Exception as exc:  # noqa: BLE001 - drain must continue to the SDK pools
            logger.warning(
                "agent shutdown failed on teardown reason=agent_shutdown_failed error=%r", exc
            )
    return teardown_pooled_sdk_transports()


# --------------------------------------------------------------------------- #
# Doctor probe: live child-process census                                       #
# --------------------------------------------------------------------------- #

# Coarse classification of a child by its executable name, so the census names WHAT
# leaked, not just how many.
_CHILD_KINDS: tuple[tuple[str, str], ...] = (
    ("clio-kit", "mcp_stdio"),
    ("clio_run", "clio_core_daemon"),
    ("uvx", "mcp_launcher"),
    ("uv", "mcp_launcher"),
    ("claude", "sdk_cli"),
    ("codex", "codex_cli"),
    ("node", "mcp_stdio"),
    ("npx", "mcp_launcher"),
    ("python", "python_child"),
)


def _classify_child(name: str) -> str:
    """Return a coarse kind label for a child process ``name``."""
    lowered = name.lower()
    for needle, kind in _CHILD_KINDS:
        if needle in lowered:
            return kind
    return "other"


def _live_children(pid: int | None = None) -> list[dict[str, Any]]:
    """Enumerate live descendant processes of ``pid`` (default: this process).

    Returns one dict per descendant with ``pid``, ``name``, ``age_seconds`` and a coarse
    ``kind``. A psutil-less environment or a vanished process yields an empty list rather
    than raising (the census is best-effort visibility, not a control path).
    """
    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        return []

    target_pid = os.getpid() if pid is None else pid
    try:
        proc = psutil.Process(target_pid)
        descendants = proc.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []

    now = time.time()
    rows: list[dict[str, Any]] = []
    for child in descendants:
        try:
            name = child.name()
            created = child.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        rows.append(
            {
                "pid": child.pid,
                "name": name,
                "age_seconds": round(max(0.0, now - created), 1),
                "kind": _classify_child(name),
            }
        )
    return rows


def probe_process_tree(
    *,
    reaper: ChildReaperResult | None = None,
    children: Sequence[Mapping[str, Any]] | None = None,
    _reaper_unset: bool = True,
) -> list[IntegrationStatus]:
    """Report the child-tree reaper binding + a live child-process census (#900).

    Two doctor rows (following the ``clio_core_health`` house style):

    * ``child_reaper`` — READY when a Windows Job Object is active (hard-kill reaps the
      tree), READY-informational when POSIX-delegated, DEGRADED when a Windows install
      failed (the hard-kill guarantee is lost — surfaced, never silent). Omitted entirely
      when no reaper was installed in this process (a standalone doctor CLI), mirroring
      :func:`clio_agent.runtime.clio_core_health.probe_clio_core_liveness`.
    * ``child_processes`` — a census of the live descendant processes (pid, name, age,
      kind) so an orphan pile-up is visible. Always emitted (READY), even at zero.

    Args:
        reaper: Reaper result to report; defaults to :func:`child_reaper_status`.
        children: Pre-computed child census (injected for tests); defaults to a live
            :func:`_live_children` enumeration.
        _reaper_unset: Internal flag distinguishing "caller passed reaper=None
            explicitly" from "use the process default"; callers should not set it.

    Returns:
        One or two :class:`IntegrationStatus` rows.
    """
    if reaper is None and _reaper_unset:
        reaper = child_reaper_status()
    census = list(children) if children is not None else _live_children()

    rows: list[IntegrationStatus] = []
    if reaper is not None:
        rows.append(_reaper_row(reaper))
    rows.append(_census_row(census))
    return rows


def _reaper_row(reaper: ChildReaperResult) -> IntegrationStatus:
    """Build the ``child_reaper`` doctor row from a reaper result."""
    common = {
        "name": "child_reaper",
        "config_source": "runtime:process_tree",
        "details": {"reason": reaper.reason, "mechanism": reaper.mechanism, **reaper.details},
        "required": True,
    }
    if reaper.mechanism == MECHANISM_WINDOWS_JOB and reaper.active:
        return IntegrationStatus(
            state=IntegrationState.READY,
            summary=(
                "Windows Job Object active (KILL_ON_JOB_CLOSE); the MCP + SDK child tree "
                "is reaped with the server even on a hard kill. The shared clio-core "
                "daemon breaks away and survives."
            ),
            next_action="No action required.",
            capabilities=["hard-kill-reap", "daemon-breakaway"],
            **common,
        )
    if reaper.mechanism == MECHANISM_POSIX_DELEGATED:
        return IntegrationStatus(
            state=IntegrationState.READY,
            summary=(
                "Child-tree reaping is delegated to per-child pdeathsig (setpriv) + the "
                "process-group teardown (POSIX); no process-wide Job Object is needed."
            ),
            next_action="No action required.",
            capabilities=["pdeathsig", "process-group-teardown"],
            **common,
        )
    return IntegrationStatus(
        state=IntegrationState.DEGRADED,
        summary=(
            "Child-tree reaper is NOT active: the Windows Job Object could not be "
            f"installed (reason={reaper.reason}). A hard kill of the server may orphan "
            "its MCP/SDK children."
        ),
        next_action=(
            "Check the Windows job-object privileges for the clio process; graceful "
            "`clio stop` still reaps the tree via the process walk."
        ),
        fallback="graceful-terminate-tree-only",
        **common,
    )


def _census_row(census: list[Mapping[str, Any]]) -> IntegrationStatus:
    """Build the ``child_processes`` census doctor row."""
    if not census:
        return IntegrationStatus(
            name="child_processes",
            state=IntegrationState.READY,
            summary="No live child processes in this process tree.",
            config_source="runtime:process_tree",
            next_action="No action required.",
            details={"reason": "no_children", "count": 0, "children": []},
            required=False,
        )
    kinds: dict[str, int] = {}
    for row in census:
        kind = str(row.get("kind", "other"))
        kinds[kind] = kinds.get(kind, 0) + 1
    breakdown = ", ".join(f"{kind}={n}" for kind, n in sorted(kinds.items()))
    return IntegrationStatus(
        name="child_processes",
        state=IntegrationState.READY,
        summary=f"{len(census)} live child process(es) in this tree ({breakdown}).",
        config_source="runtime:process_tree",
        next_action=(
            "Expected while the server runs (MCP stdio + SDK CLI). A hard-kill reaper "
            "(Windows Job Object / POSIX pdeathsig) prevents these from orphaning."
        ),
        details={"reason": "child_census", "count": len(census), "children": list(census)},
        required=False,
    )
