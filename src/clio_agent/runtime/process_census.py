"""Parent-chain census of CLIO's process tree — orphan detection (#900, PART B).

The owner's goal is a *clean Task Manager tree*: every CLIO child process shows as a
descendant of one of exactly two roots — the **clio-agent server** (this process, which
installs the Windows Job Object in :mod:`clio_agent.runtime.process_tree`) or the
**shared clio-core daemon** (the deliberate ``CREATE_BREAKAWAY_FROM_JOB`` breakaway
spawned in :func:`clio_agent.arc.storage._spawn_runtime_daemon`). An intermediate
launcher that exits and orphans its child — leaving a reparented ``uv``/``python``/
``node`` idling under init — is exactly what this census makes visible.

Where :func:`clio_agent.runtime.process_tree.probe_process_tree` answers "how many live
children, of what kind", this module answers "does each CLIO process still descend from a
legitimate root?" and flags any that does not as a typed ``orphaned_from_tree`` row (no
silent no-op — an orphan surfaces as a DEGRADED doctor row).

The classifier :func:`classify_parentage` is a pure function over an injected process
snapshot, so the orphan verdict is unit-testable without spawning anything. The live
:func:`probe_process_parentage` builds that snapshot from psutil, scoped to descendants of
the two roots plus any CLIO-kind process whose parent has died (a reparented orphan), so a
*parallel* CLIO instance's healthy children — which descend from their own live root — are
never mislabelled as this server's orphans.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Optional

from clio_agent.runtime.process_tree import _classify_child
from clio_agent.runtime.sandbox import confinement_for_kind
from clio_agent.runtime.status import IntegrationState, IntegrationStatus

logger = logging.getLogger(__name__)

# Typed parentage verdicts (so the doctor / trace never guess).
DESCENDS_SERVER_ROOT = "server_root"
DESCENDS_DAEMON_ROOT = "daemon_root"
ORPHANED_FROM_TREE = "orphaned_from_tree"

_MAX_CHAIN_DEPTH = 64  # guard against a cyclic/self-referential ppid table


@dataclass(frozen=True)
class ProcessNode:
    """One process in a census snapshot (the minimal fields parentage needs)."""

    pid: int
    ppid: int
    name: str
    create_time: float
    kind: str


@dataclass(frozen=True)
class ParentageRow:
    """A CLIO process and where its parent chain lands.

    Attributes:
        pid: The process id.
        name: The executable name.
        kind: The coarse child kind (:func:`clio_agent.runtime.process_tree._classify_child`).
        parent_chain: PIDs from this process's parent up toward a root, in order
            (empty when the parent is already unknown/dead).
        descends_from: One of :data:`DESCENDS_SERVER_ROOT`, :data:`DESCENDS_DAEMON_ROOT`,
            or :data:`ORPHANED_FROM_TREE`.
    """

    pid: int
    name: str
    kind: str
    parent_chain: tuple[int, ...]
    descends_from: str
    confinement: str = "excluded"


def classify_parentage(
    nodes: Iterable[ProcessNode],
    *,
    server_root_pid: int,
    daemon_root_pid: Optional[int],
) -> list[ParentageRow]:
    """Classify each CLIO-kind process by which root its parent chain reaches.

    Pure function: no OS calls, so an orphan verdict can be pinned with a synthetic
    process table. A node is CLIO-kind when :func:`_classify_child` returns anything but
    ``"other"``. The two roots themselves are never emitted as rows.

    Args:
        nodes: The process snapshot to classify.
        server_root_pid: The clio-agent server process id (the first root).
        daemon_root_pid: The shared clio-core daemon pid (the second root), or ``None``
            when no daemon is running.

    Returns:
        One :class:`ParentageRow` per CLIO-kind process, sorted by pid.
    """
    by_pid: dict[int, ProcessNode] = {n.pid: n for n in nodes}
    roots = {server_root_pid}
    if daemon_root_pid is not None:
        roots.add(daemon_root_pid)

    rows: list[ParentageRow] = []
    for node in by_pid.values():
        if node.pid in roots or node.kind == "other":
            continue
        chain: list[int] = []
        verdict = ORPHANED_FROM_TREE
        cursor = node.ppid
        seen: set[int] = set()
        for _ in range(_MAX_CHAIN_DEPTH):
            if cursor == server_root_pid:
                verdict = DESCENDS_SERVER_ROOT
                chain.append(cursor)
                break
            if daemon_root_pid is not None and cursor == daemon_root_pid:
                verdict = DESCENDS_DAEMON_ROOT
                chain.append(cursor)
                break
            if cursor in seen or cursor not in by_pid:
                break  # dead/unknown parent or a cycle -> chain terminates (orphan)
            seen.add(cursor)
            chain.append(cursor)
            cursor = by_pid[cursor].ppid
        rows.append(
            ParentageRow(
                pid=node.pid,
                name=node.name,
                kind=node.kind,
                parent_chain=tuple(chain),
                descends_from=verdict,
                confinement=confinement_for_kind(node.kind),
            )
        )
    rows.sort(key=lambda r: r.pid)
    return rows


def _daemon_root_pid() -> Optional[int]:
    """Return the shared clio-core daemon pid from its pidfile, or ``None``."""
    from clio_agent.arc import storage  # noqa: PLC0415 - avoid import cycle at module load

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


def _snapshot_process_nodes(
    server_root_pid: int, daemon_root_pid: Optional[int]
) -> list[ProcessNode]:
    """Build a live process snapshot scoped to CLIO's tree (best-effort, psutil-gated).

    Included: every descendant of the two roots, the roots themselves, and any CLIO-kind
    process whose parent PID is not alive (a reparented orphan). Scoping to our own roots
    keeps a *parallel* CLIO instance's healthy children — which descend from their own
    live root and have a live parent — out of the snapshot, so they are never mislabelled
    as this server's orphans. A psutil-less environment yields an empty list.
    """
    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        return []

    raw: dict[int, ProcessNode] = {}
    for proc in psutil.process_iter(["pid", "ppid", "name", "create_time"]):
        try:
            info = proc.info
            pid = int(info["pid"])
            raw[pid] = ProcessNode(
                pid=pid,
                ppid=int(info.get("ppid") or 0),
                name=str(info.get("name") or ""),
                create_time=float(info.get("create_time") or 0.0),
                kind=_classify_child(str(info.get("name") or "")),
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError, TypeError, ValueError):
            continue

    alive = set(raw)
    children_of: dict[int, list[int]] = {}
    for node in raw.values():
        children_of.setdefault(node.ppid, []).append(node.pid)

    keep: set[int] = set()
    for root in (server_root_pid, daemon_root_pid):
        if root is None or root not in raw:
            if root is not None and root == server_root_pid:
                keep.add(root)  # our own pid may be absent from a partial scan; keep it
            continue
        keep.add(root)
        stack = list(children_of.get(root, ()))
        while stack:
            pid = stack.pop()
            if pid in keep:
                continue
            keep.add(pid)
            stack.extend(children_of.get(pid, ()))

    # Reparented orphans: a CLIO-kind process whose parent is no longer alive.
    for node in raw.values():
        if node.kind != "other" and node.ppid not in alive and node.pid not in keep:
            keep.add(node.pid)

    return [raw[pid] for pid in keep if pid in raw]


def probe_process_parentage(
    *,
    nodes: Optional[Sequence[ProcessNode]] = None,
    server_root_pid: Optional[int] = None,
    daemon_root_pid: Optional[int] = None,
    _daemon_unset: bool = True,
) -> IntegrationStatus:
    """Doctor row: each CLIO process's parent chain + any ``orphaned_from_tree`` flag.

    READY when every CLIO process descends from the server root or the daemon root;
    DEGRADED (surfaced, never silent) when one or more descend from neither — an
    intermediate launcher exited and orphaned its child.

    Args:
        nodes: Pre-computed process snapshot (injected for tests); defaults to a live
            :func:`_snapshot_process_nodes` scan.
        server_root_pid: The server root pid; defaults to this process.
        daemon_root_pid: The clio-core daemon pid; defaults to the pidfile lookup.
        _daemon_unset: Internal flag distinguishing an explicit ``daemon_root_pid=None``
            from "look it up"; callers should not set it.

    Returns:
        A single :class:`IntegrationStatus` row.
    """
    if server_root_pid is None:
        server_root_pid = os.getpid()
    if daemon_root_pid is None and _daemon_unset:
        daemon_root_pid = _daemon_root_pid()
    snapshot = (
        list(nodes)
        if nodes is not None
        else _snapshot_process_nodes(server_root_pid, daemon_root_pid)
    )
    rows = classify_parentage(
        snapshot, server_root_pid=server_root_pid, daemon_root_pid=daemon_root_pid
    )
    orphans = [r for r in rows if r.descends_from == ORPHANED_FROM_TREE]
    detail_rows = [
        {
            "pid": r.pid,
            "name": r.name,
            "kind": r.kind,
            "parent_chain": list(r.parent_chain),
            "descends_from": r.descends_from,
            "confinement": r.confinement,
        }
        for r in rows
    ]
    details: dict[str, Any] = {
        "reason": "orphaned_from_tree" if orphans else "all_rooted",
        "server_root_pid": server_root_pid,
        "daemon_root_pid": daemon_root_pid,
        "count": len(rows),
        "orphan_count": len(orphans),
        "processes": detail_rows,
    }
    if orphans:
        named = ", ".join(f"{o.name}(pid={o.pid})" for o in orphans)
        logger.warning(
            "process-tree census: %d orphaned CLIO process(es) reason=orphaned_from_tree %s",
            len(orphans),
            named,
        )
        return IntegrationStatus(
            name="child_parentage",
            state=IntegrationState.DEGRADED,
            summary=(
                f"{len(orphans)} CLIO process(es) do NOT descend from the server root "
                f"(pid {server_root_pid}) or the clio-core daemon root "
                f"(pid {daemon_root_pid}): {named}. An intermediate launcher exited and "
                "orphaned its child."
            ),
            config_source="runtime:process_census",
            next_action=(
                "Reap the orphan(s); a hard-kill reaper (Windows Job Object / POSIX "
                "pdeathsig) normally prevents this. Spawn the final executable directly "
                "from the server so no intermediary can exit and sever parentage."
            ),
            fallback="orphans-idle-until-manually-reaped",
            details=details,
            required=False,
        )
    return IntegrationStatus(
        name="child_parentage",
        state=IntegrationState.READY,
        summary=(
            f"All {len(rows)} CLIO process(es) descend from the server root "
            f"(pid {server_root_pid}) or the clio-core daemon root (the two intended roots)."
        ),
        config_source="runtime:process_census",
        next_action="No action required.",
        details=details,
        required=False,
    )


__all__ = [
    "ProcessNode",
    "ParentageRow",
    "classify_parentage",
    "probe_process_parentage",
    "DESCENDS_SERVER_ROOT",
    "DESCENDS_DAEMON_ROOT",
    "ORPHANED_FROM_TREE",
]
