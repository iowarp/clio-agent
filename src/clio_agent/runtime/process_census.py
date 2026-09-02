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
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Optional

from clio_agent.errors import PROCESS_CENSUS_ORPHAN_REAPED
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

    # A venv or package-manager launcher may remain as the live server's parent
    # after its own shell has exited.  It is part of the bootstrap chain, not an
    # orphaned child.  Reaping it can close a Windows Job Object and terminate
    # the just-started server along with it, so exclude the server's ancestors
    # from child classification.
    server_ancestors: set[int] = set()
    server_root = by_pid.get(server_root_pid)
    cursor = server_root.ppid if server_root is not None else 0
    while cursor in by_pid and cursor not in server_ancestors:
        server_ancestors.add(cursor)
        cursor = by_pid[cursor].ppid

    rows: list[ParentageRow] = []
    for node in by_pid.values():
        if node.pid in roots or node.pid in server_ancestors or node.kind == "other":
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


@dataclass(frozen=True)
class ReapedProcess:
    """One provably-orphaned CLIO child that was actually killed (#1232 pt 4)."""

    pid: int
    name: str
    kind: str


def _pid_alive(pid: int) -> bool:
    """True when ``pid`` currently names a live process (best-effort, psutil-gated)."""

    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        return False
    return bool(pid) and psutil.pid_exists(pid)


def _kill_pid(pid: int) -> None:
    """Default killer: a real ``psutil.Process(pid).kill()`` (SIGKILL/TerminateProcess)."""

    import psutil  # noqa: PLC0415

    psutil.Process(pid).kill()


#: Kinds NEVER auto-killed by :func:`reap_orphaned_processes`, no matter how
#: "orphaned" they look. ``classify_parentage`` excludes the CURRENT
#: ``daemon_root_pid`` by construction (roots are never emitted as rows), but
#: that exclusion is only as reliable as the ONE pidfile-based lookup behind
#: it -- a live multi-daemon test run proved that lookup can miss a
#: genuinely-live daemon (a session-scoped/isolated daemon whose pidfile the
#: default lookup does not resolve), which then reads as a plain
#: dead-parent orphan and gets killed: a real clio-core daemon carrying live
#: sessions' data was killed this way during test development (see #1232 PR
#: discussion / test_process_census.py's exclusion test). Daemon identity
#: cannot be proven robustly enough from parentage alone to risk that again,
#: so ``clio_core_daemon``-kind rows are excluded by KIND, not just by the
#: fragile pid match -- they stay report-only (the pre-#1232, safe behavior)
#: until a stronger liveness signal (e.g. the daemon's own port/health check)
#: backs an auto-kill decision. Every OTHER kind (mcp_stdio, mcp_launcher,
#: python_child, sdk_cli, codex_cli) is an unambiguous per-tool-call spawn --
#: never a shared coordination process -- and stays a safe reap target.
_NEVER_REAP_KINDS = frozenset({"clio_core_daemon"})


def reap_orphaned_processes(
    *,
    nodes: Optional[Sequence[ProcessNode]] = None,
    server_root_pid: Optional[int] = None,
    daemon_root_pid: Optional[int] = None,
    _daemon_unset: bool = True,
    kill: Optional[Callable[[int], None]] = None,
    parent_alive: Optional[Callable[[int], bool]] = None,
) -> list[ReapedProcess]:
    """KILL every provably-orphaned CLIO child — the census REAPS, not just reports (#1232 pt 4).

    Owner-observed: 7+ orphaned ``clio_run.exe`` children from prior hard-kills sat
    reported (``orphaned_from_tree``) across multiple boots, holding lock-adjacent
    state, while the census "listed them and moved on". This is a PARTIAL fix for
    that: every row :func:`classify_parentage` marks :data:`ORPHANED_FROM_TREE` is,
    BY THAT FUNCTION'S OWN CONSTRUCTION (see :func:`_snapshot_process_nodes`'s
    "reparented orphans" pass — the only source of a non-descendant row in the LIVE
    scan), a CLIO-kind process (``kind != "other"``) whose IMMEDIATE parent pid was
    already confirmed dead at snapshot time — EXCEPT any :data:`_NEVER_REAP_KINDS`
    kind (see that constant: a real live clio-core daemon was killed this way during
    development, so daemon-kind rows stay report-only). Kill time re-confirms the
    parent is STILL dead (a fresh ``psutil`` check, guarding the snapshot-to-kill
    window) before acting, and skips — never kills — a row whose parent came back
    alive in that window.

    The breakaway shared clio-core daemon is ADDITIONALLY excluded BY CONSTRUCTION
    (belt-and-suspenders, not the primary guard): :func:`classify_parentage` never
    emits a row for either root pid at all (``if node.pid in roots: continue``), so
    the CURRENT live daemon (``daemon_root_pid``, when correctly resolved) can never
    appear as a candidate no matter how stale ITS own parent chain is.

    Args:
        nodes / server_root_pid / daemon_root_pid / _daemon_unset: Same as
            :func:`probe_process_parentage` (injectable for tests).
        kill: ``pid -> None`` killer (injected for tests); defaults to
            :func:`_kill_pid` (a real ``psutil.Process(pid).kill()``).
        parent_alive: ``pid -> bool`` liveness probe (injected for synthetic
            process tables); defaults to the real :func:`_pid_alive` check.

    Returns:
        One :class:`ReapedProcess` per pid actually killed. A typed
        ``PROCESS_CENSUS_ORPHAN_REAPED`` log line is emitted per kill (never
        silent); a kill that itself raises (already exited, access denied) is
        logged and skipped rather than aborting the pass.
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
    by_pid = {n.pid: n for n in snapshot}
    rows = classify_parentage(
        snapshot, server_root_pid=server_root_pid, daemon_root_pid=daemon_root_pid
    )
    killer = kill or _kill_pid
    is_parent_alive = parent_alive or _pid_alive

    reaped: list[ReapedProcess] = []
    for row in rows:
        if row.descends_from != ORPHANED_FROM_TREE:
            continue
        if row.kind in _NEVER_REAP_KINDS:
            logger.debug(
                "orphan_reap_skipped pid=%s name=%s kind=%s reason=never_reap_kind",
                row.pid,
                row.name,
                row.kind,
            )
            continue
        node = by_pid.get(row.pid)
        immediate_parent = node.ppid if node is not None else None
        if immediate_parent is not None and is_parent_alive(immediate_parent):
            # The snapshot-to-kill window closed: the parent came back (or a
            # slower scan caught it mid-restart). Never kill a child whose
            # parent is provably alive right now — re-probed next pass.
            logger.debug(
                "orphan_reap_skipped pid=%s name=%s reason=parent_alive_at_kill_time ppid=%s",
                row.pid,
                row.name,
                immediate_parent,
            )
            continue
        try:
            killer(row.pid)
        except Exception as exc:  # noqa: BLE001 - typed skip, never abort the pass
            logger.warning(
                "orphan_reap_failed pid=%s name=%s kind=%s error=%s",
                row.pid,
                row.name,
                row.kind,
                exc,
            )
            continue
        reaped.append(ReapedProcess(pid=row.pid, name=row.name, kind=row.kind))
        from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

        stream_audit(
            "orphan_reap_killed",
            reason=PROCESS_CENSUS_ORPHAN_REAPED,
            pid=row.pid,
            name=row.name,
            kind=row.kind,
        )
        logger.warning(
            "orphan_reap_killed reason=%s pid=%s name=%s kind=%s parent_chain=%s",
            PROCESS_CENSUS_ORPHAN_REAPED,
            row.pid,
            row.name,
            row.kind,
            list(row.parent_chain),
        )
    return reaped


async def boot_reap_off_loop() -> None:
    """Run :func:`reap_orphaned_processes` off the event loop (server-boot hook, #1232 pt 4).

    Called BEFORE ``tools.mcp_cache.boot_prune_off_loop`` in the lifespan
    (``gact/app.py``): ``live_peer_clio_processes`` (the prune's liveness
    gate, ``runtime/disk_gc.py``) matches any RUNNING process with a clio
    marker — a still-running-but-orphaned ``clio_run.exe`` from a prior hard
    kill counts as a "live peer" to it regardless of parentage, which is
    exactly the observed bug (a boot-prune deferred for two days across
    multiple boots because stale orphans kept looking like peers). Reaping
    them here first, before that check runs, is a direct side-effect fix — no
    change needed in ``disk_gc.py`` itself. A ``psutil`` scan must never block
    uvicorn's port binding, so this runs in the default executor; best-effort
    (a failure is logged and swallowed, never breaks server boot).
    """

    import asyncio  # noqa: PLC0415
    import logging  # noqa: PLC0415

    loop = asyncio.get_running_loop()
    try:
        reaped = await loop.run_in_executor(None, reap_orphaned_processes)
    except Exception:  # noqa: BLE001 - best-effort boot reap; a failure must never break server boot
        logging.getLogger(__name__).exception(
            "boot orphan-process reap failed (#1232 pt 4); continuing boot"
        )
        return
    if reaped:
        logging.getLogger(__name__).warning(
            "boot orphan-process reap killed %d process(es): %s",
            len(reaped),
            ", ".join(f"{r.name}(pid={r.pid})" for r in reaped),
        )


__all__ = [
    "ProcessNode",
    "ParentageRow",
    "ReapedProcess",
    "classify_parentage",
    "probe_process_parentage",
    "reap_orphaned_processes",
    "boot_reap_off_loop",
    "DESCENDS_SERVER_ROOT",
    "DESCENDS_DAEMON_ROOT",
    "ORPHANED_FROM_TREE",
]
