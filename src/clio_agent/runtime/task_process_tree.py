"""Per-task process-tree ownership, distinct from the application's child reaper."""

from __future__ import annotations

import ctypes
import os
import signal
import sys
from typing import Any

import psutil


def task_spawn_options(existing: dict[str, Any]) -> dict[str, Any]:
    """Keep confinement options and create an independently cancellable process tree."""
    options = dict(existing)
    if sys.platform == "win32":
        # No code runs before the new process is assigned to its task's Job.
        options["creationflags"] = int(options.get("creationflags", 0)) | 0x00000004
    else:
        options["start_new_session"] = True
    return options


def _group_running(pgid: int) -> bool:
    """Whether a member of process group ``pgid`` is still running, not just unreaped.

    A descendant that outlived its shell is re-parented to the system's reaper; once
    it exits it stays in the group as a zombie until that reaper collects it. A
    zombie holds no pipes and does no work, so it does not keep a task running. Some
    reapers collect on a timer (a container's init took about 2 s), which every
    command with a background or substituted process waited out. Where ``/proc`` is
    not readable the group counts as running, as before.
    """
    try:
        entries = os.listdir("/proc")
    except OSError:
        return True
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="ascii", errors="replace") as handle:
                stat = handle.read()
        except OSError:
            continue  # exited while listing
        fields = stat[stat.rfind(")") + 2 :].split()
        if len(fields) > 2 and fields[2] == str(pgid) and fields[0] != "Z":
            return True
    return False


class TaskProcessTree:
    """Own a fresh process group/Job in memory; never reconstruct ownership from a PID."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.birth: float | None = None
        self.job: Any = None
        self.kernel: Any = None
        try:
            process = psutil.Process(pid)
            self.birth = process.create_time()
            if sys.platform == "win32":
                self._bind_windows(process)
        except psutil.NoSuchProcess:
            if sys.platform == "win32":
                raise RuntimeError(
                    "Suspended shell disappeared before ownership was established"
                ) from None

    def _bind_windows(self, process: psutil.Process) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows task Jobs require Windows")
        from ctypes import wintypes

        from clio_agent.runtime.process_tree import _JOBOBJECT_EXTENDED_LIMIT_INFORMATION

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
        ]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            process.kill()
            raise ctypes.WinError(ctypes.get_last_error())
        child = None
        try:
            limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE, no breakaway.
            if not kernel.SetInformationJobObject(
                job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            child = kernel.OpenProcess(0x0100 | 0x0001, False, self.pid)  # SET_QUOTA | TERMINATE.
            if not child or not kernel.AssignProcessToJobObject(job, child):
                raise ctypes.WinError(ctypes.get_last_error())
            self.kernel, self.job = kernel, job
            process.resume()
        except Exception:
            process.kill()
            kernel.CloseHandle(job)
            raise
        finally:
            if child:
                kernel.CloseHandle(child)

    def active(self) -> bool:
        """Include descendants that outlive their shell, using the owned OS container."""
        if self.job is not None:
            # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION: four LARGE_INTEGERs,
            # page faults, total processes, active processes, terminated processes.
            accounting = (ctypes.c_uint32 * 12)()
            if not self.kernel.QueryInformationJobObject(
                self.job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
            ):
                raise OSError("Could not query the owned shell Job")
            return bool(accounting[10])
        if sys.platform != "win32":
            try:
                os.killpg(self.pid, 0)
            except ProcessLookupError:
                return False
            return _group_running(self.pid)
        return False

    def terminate(self) -> None:
        """Stop this task's live tree, including children with closed inherited pipes."""
        if self.job is not None:
            if not self.kernel.TerminateJobObject(self.job, 1):
                raise OSError("Could not terminate the owned shell Job")
            return
        if sys.platform != "win32":
            # A fence can start its sandbox in a session of its own (bwrap --new-session),
            # out of this group's reach but still a descendant of the owned process.
            escaped = self._descendants()
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            for process in escaped:
                try:
                    process.kill()
                except psutil.NoSuchProcess:
                    pass  # it ended with its group

    def _descendants(self) -> list[psutil.Process]:
        """The owned process's live descendants, wherever their group or session."""
        try:
            process = psutil.Process(self.pid)
            if self.birth is None or process.create_time() != self.birth:
                return []  # not the process this tree was given
            return process.children(recursive=True)
        except psutil.NoSuchProcess:
            return []

    def close(self) -> None:
        """Release an already-settled Job; closing a live Job also reaps its tree."""
        if self.job is not None:
            self.kernel.CloseHandle(self.job)
            self.job = None
