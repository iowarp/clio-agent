"""Low-impact live process snapshots for CLIO parentage checks."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Optional

from clio_agent.runtime.process_tree import _classify_child


@dataclass(frozen=True)
class ProcessNode:
    """One process in a parentage snapshot."""

    pid: int
    ppid: int
    name: str
    create_time: float
    kind: str
    executable: str = ""
    cwd: str = ""
    cmdline: tuple[str, ...] = ()


def process_cmdline(pid: int) -> tuple[str, ...]:
    """Return one process command line, or no evidence when it cannot be read."""

    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        return ()
    try:
        return tuple(psutil.Process(pid).cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return ()


def belongs_to_runtime(node: ProcessNode, owner_roots: Sequence[str]) -> bool:
    """Return whether a detached process has path evidence of runtime ownership."""

    for owner_root in owner_roots:
        for candidate in (node.executable, node.cwd):
            if not candidate:
                continue
            try:
                normalized_root = os.path.normcase(os.path.abspath(owner_root))
                normalized_candidate = os.path.normcase(os.path.abspath(candidate))
                if os.path.commonpath((normalized_root, normalized_candidate)) == normalized_root:
                    return True
            except (OSError, ValueError):
                continue
    return False


def _windows_parent_table(psutil: Any) -> Optional[dict[int, int]]:
    """Read every parent pid in one system snapshot, or ``None`` to ask per process.

    psutil's Windows ``ppid()`` rebuilds this whole-system table for each process it
    is asked about: about 15 ms each, 9 s for 600 processes, which held server boot.
    """

    if os.name != "nt":
        return None
    try:
        return {int(pid): int(ppid) for pid, ppid in psutil._psplatform.cext.ppid_map().items()}
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def snapshot_process_nodes(
    server_root_pid: int,
    daemon_root_pid: Optional[int],
    *,
    cmdline_resolver: Callable[[int], tuple[str, ...]] = process_cmdline,
) -> list[ProcessNode]:
    """Build a live snapshot scoped to CLIO's roots and owned orphan candidates.

    The machine-wide pass intentionally requests only cheap identity fields. On
    Windows, resolving ``exe`` and ``cwd`` for every foreign process can occupy a
    worker for minutes. Path and command-line evidence is therefore hydrated only
    for the owner roots and detached CLIO-kind candidates.
    """

    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        return []

    raw: dict[int, ProcessNode] = {}
    process_handles: dict[int, Any] = {}
    parents = _windows_parent_table(psutil)
    # create_time is read only for the kept nodes below: for an access-denied process
    # psutil's Windows fallback re-reads the whole-system table (~13 ms each, 2 s+ per boot).
    fields = ["pid", "name"]
    if parents is None:
        fields.append("ppid")
    for proc in psutil.process_iter(fields):
        try:
            info = proc.info
            pid = int(info["pid"])
            name = str(info.get("name") or "")
            if parents is None:
                ppid = int(info.get("ppid") or 0)
            elif pid in parents:
                ppid = parents[pid]
            else:  # started after the table was read
                ppid = int(proc.ppid() or 0)
            raw[pid] = ProcessNode(
                pid=pid,
                ppid=ppid,
                name=name,
                create_time=0.0,
                kind=_classify_child(name),
            )
            process_handles[pid] = proc
        except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError, TypeError, ValueError):
            continue

    def _path_details(pid: int) -> tuple[str, str]:
        proc = process_handles.get(pid)
        if proc is None:
            return "", ""
        info = getattr(proc, "info", {})
        executable = str(info.get("exe") or "")
        cwd = str(info.get("cwd") or "")
        if not executable:
            try:
                executable = str(proc.exe() or "")
            except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError, OSError):
                pass
        if not cwd:
            try:
                cwd = str(proc.cwd() or "")
            except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError, OSError):
                pass
        return executable, cwd

    alive = set(raw)
    children_of: dict[int, list[int]] = {}
    for node in raw.values():
        children_of.setdefault(node.ppid, []).append(node.pid)

    keep: set[int] = set()
    for root in (server_root_pid, daemon_root_pid):
        if root is None or root not in raw:
            if root == server_root_pid:
                keep.add(root)
            continue
        keep.add(root)
        stack = list(children_of.get(root, ()))
        while stack:
            pid = stack.pop()
            if pid in keep:
                continue
            keep.add(pid)
            stack.extend(children_of.get(pid, ()))

    owner_root_values: list[str] = []
    for root_pid in (server_root_pid, daemon_root_pid):
        if root_pid is None:
            continue
        root_node = raw.get(root_pid)
        if root_node is None:
            continue
        executable, cwd = _path_details(root_pid)
        raw[root_pid] = replace(root_node, executable=executable, cwd=cwd)
        if cwd:
            owner_root_values.append(cwd)
    owner_roots = tuple(owner_root_values)

    for node in list(raw.values()):
        if node.kind == "other" or node.ppid in alive or node.pid in keep:
            continue
        executable, cwd = _path_details(node.pid)
        candidate = replace(node, executable=executable, cwd=cwd)
        if belongs_to_runtime(candidate, owner_roots):
            raw[node.pid] = replace(candidate, cmdline=cmdline_resolver(node.pid))
            keep.add(node.pid)

    kept: list[ProcessNode] = []
    for pid in keep:
        node = raw.get(pid)
        if node is None:
            continue
        proc = process_handles.get(pid)
        try:
            created = float(proc.create_time() or 0.0) if proc is not None else 0.0
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError, OSError):
            created = 0.0
        kept.append(replace(node, create_time=created))
    return kept


__all__ = [
    "ProcessNode",
    "belongs_to_runtime",
    "process_cmdline",
    "snapshot_process_nodes",
]
