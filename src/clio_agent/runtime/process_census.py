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
from dataclasses import dataclass, replace
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
    """One process in a census snapshot (the minimal fields parentage needs).

    Attributes:
        pid: The process id.
        ppid: The parent process id (0/dead when the parent is gone).
        name: The executable name (used only for the coarse ``kind`` classification
            and display -- see :func:`clio_agent.runtime.process_tree._classify_child`).
        create_time: The process's start time (epoch seconds), from the SNAPSHOT.
            The REAP re-verifies this against the live process right before killing
            (#1303 F3, mirroring :func:`clio_agent.serve._pid_alive`'s PID-reuse
            defeat): a PID recycled by the OS between snapshot and kill gets a
            DIFFERENT creation time, so a stale row is never mistaken for the
            process it named.
        kind: The coarse child kind.
        cmdline: The process's argv, captured best-effort (#1303). Empty by default --
            populated only for reparented-orphan candidates in the live
            :func:`_snapshot_process_nodes` scan (``AccessDenied``/any resolution
            failure also yields empty). This is the REAP's positive product
            evidence (see :func:`_has_clio_product_evidence`): a name-substring
            match plus a dead parent is NOT evidence on its own -- every detached
            job on the box has a dead parent -- so an empty cmdline is treated as
            NO evidence, never as implicit ownership.
    """

    pid: int
    ppid: int
    name: str
    create_time: float
    kind: str
    cmdline: tuple[str, ...] = ()


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


#: Conservative clio ownership markers matched against a process's CMDLINE, never its
#: bare executable name (#1303). A name-substring match (``"uv"``, ``"python"``,
#: ``"node"``, ``"claude"``, ``"codex"`` -- see ``process_tree._CHILD_KINDS``) plus a
#: dead parent is NOT ownership evidence: every detached job on Windows (a closed
#: terminal, a scheduled task, `Start-Process`) has a dead parent, so ANY unrelated
#: uv/python/node/claude/codex process on the box would match by name alone. Only an
#: invocation whose actual command line names a clio entry point/module counts.
#:
#: PRECISE invocation tokens ONLY -- deliberately NOT the bare ``"clio-agent"`` /
#: ``"clio_agent"`` strings (review round, live-verified: every ``uv run`` in this
#: checkout puts the venv interpreter's own ABSOLUTE PATH,
#: ``D:\...\clio-agent\.venv\Scripts\python.exe``, in argv[0] -- a bare-substring
#: marker would still match that path and kill a detached leg-runner script one hop
#: down its own launch chain, the same bug relocated). This mirrors
#: :data:`clio_agent.runtime.disk_gc._CLIO_MARKERS` (see its rationale comment: same
#: "precise tokens, not the bare repo-path string" reasoning) but is intentionally its
#: OWN constant, never shared/imported -- the two have OPPOSITE risk polarity.
#: ``disk_gc``'s markers gate a "live peer, don't touch shared state" decision, where
#: an under-match is the dangerous direction (a missed peer risks colliding with it);
#: this constant gates a KILL, where an over-match is the dangerous direction (a false
#: positive kills an unrelated process). Diverging in the future is expected and safe
#: -- do not "simplify" by importing one into the other.
_CLIO_CMDLINE_MARKERS: tuple[str, ...] = (
    "clio-kit",
    "clio_kit",
    "clio_run",
    "clio-agent-gact",
    "clio_agent.gact",
    "clio-agent serve",
    "clio_agent.ui.cli serve",
)


def _has_clio_product_evidence(node: ProcessNode) -> bool:
    """True when ``node.cmdline`` names a clio entry point (#1303 reap evidence gate).

    An empty ``cmdline`` (unresolved -- ``AccessDenied``, an already-exited process, or
    a synthetic test node that never set it) is treated as NO evidence, never as
    implicit ownership by name+dead-parent alone.

    IMPORTANT scope limit (review round, deliberate): this proves the process is SOME
    clio product invocation -- clio-kit, the gact server, the clio-core daemon -- NOT
    that it is specifically THIS server's own child. A detached process from a
    *parallel* clio stack on the same box still matches (its cmdline is equally a real
    clio invocation) and would still be reaped by this pass. That is a pre-existing
    limitation of parentage-plus-cmdline evidence, not something this gate can fix on
    its own -- proving "mine, not a peer's" would need an owning-instance stamp (e.g.
    an env var this server sets on every child it spawns), which is deliberately NOT
    built here; it is out of scope for #1303 (which closes the far worse "any unrelated
    non-clio process" hole). Do not read this function's name as "belongs to me".
    """
    if not node.cmdline:
        return False
    joined = " ".join(node.cmdline).lower()
    return any(marker in joined for marker in _CLIO_CMDLINE_MARKERS)


def _process_cmdline(pid: int) -> tuple[str, ...]:
    """Best-effort live ``cmdline()`` for ``pid`` (#1303 instance-evidence gate).

    Only called for reparented-orphan candidates in :func:`_snapshot_process_nodes`
    (never the whole machine-wide scan), so the extra per-process syscall stays cheap.
    Returns an empty tuple on ANY resolution failure -- already exited, permission
    denied, a zombie, a psutil-less environment -- which :func:`_has_clio_product_evidence`
    then correctly reads as no evidence, never as implicit ownership.
    """
    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        return ()
    try:
        return tuple(psutil.Process(pid).cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return ()


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
    # #1303: name+dead-parent alone is NOT ownership evidence -- every detached job on
    # the box has a dead parent. Attach the live cmdline here (lazy, only for these
    # candidate rows -- never the whole machine-wide scan) so the REAP's evidence gate
    # (`_has_clio_product_evidence`) can require an actual clio marker before a kill
    # is ever considered. Rows already reachable from a root never pass through here;
    # they were never orphan candidates in the first place.
    for node in list(raw.values()):
        if node.kind != "other" and node.ppid not in alive and node.pid not in keep:
            raw[node.pid] = replace(node, cmdline=_process_cmdline(node.pid))
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


def _live_create_time(pid: int) -> Optional[float]:
    """Live process creation time (epoch seconds) via psutil, or ``None`` if unresolvable.

    #1303 F3, PID-reuse defeat: mirrors :func:`clio_agent.serve._proc_create_time`. The
    OS can recycle a PID between the census snapshot and the kill; a recycled PID gets a
    DIFFERENT creation time, so comparing against it (see the reap's ``pid_recycled``
    check) stops a stale snapshot row from being mistaken for the unrelated process now
    holding that number. A module-level function (not inlined) so synthetic tests --
    whose ``ProcessNode.create_time`` is a fabricated value with no real process behind
    it -- can monkeypatch it to a matching value.
    """
    try:
        import psutil  # noqa: PLC0415

        return float(psutil.Process(pid).create_time())
    except Exception:  # noqa: BLE001 - NoSuchProcess/AccessDenied/ImportError => unknown
        return None


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
    skip_counts: Optional[dict[str, int]] = None,
) -> list[ReapedProcess]:
    """KILL every provably-orphaned CLIO child — the census REAPS, not just reports (#1232 pt 4).

    Owner-observed: 7+ orphaned ``clio_run.exe`` children from prior hard-kills sat
    reported (``orphaned_from_tree``) across multiple boots, holding lock-adjacent
    state, while the census "listed them and moved on". This is a PARTIAL fix for
    that: every row :func:`classify_parentage` marks :data:`ORPHANED_FROM_TREE` is a
    CLIO-KIND process (``kind != "other"``, a NAME-substring classification — see
    :func:`clio_agent.runtime.process_tree._classify_child`) whose IMMEDIATE parent pid
    was already confirmed dead at snapshot time. **Name-substring + dead parent is NOT
    ownership evidence** (#1303, proven live: a gact boot killed pid 43472, an unrelated
    detached ``uv run python ...`` launcher, purely because ``"uv"`` matched
    ``mcp_launcher`` and its transient shell parent had exited — every detached job on
    Windows has a dead parent). So a row is only ever a KILL candidate when it clears
    ALL of:

    1. Not a :data:`_NEVER_REAP_KINDS` kind (see that constant: a real live clio-core
       daemon was killed this way during #1232 development, so daemon-kind rows stay
       report-only).
    2. **Positive product evidence** (#1303): :func:`_has_clio_product_evidence`
       requires the row's live ``cmdline`` (captured in :func:`_snapshot_process_nodes`'s
       "reparented orphans" pass — the only source of a non-descendant row in the LIVE
       scan) to contain one of the PRECISE invocation tokens in
       :data:`_CLIO_CMDLINE_MARKERS`. An unresolved/empty cmdline (``AccessDenied``,
       already exited) — or a cmdline that merely happens to sit under a
       ``clio-agent`` checkout path (e.g. the venv interpreter's own absolute path) —
       is NO evidence, never implicit ownership: the row is skipped typed
       ``no_clio_evidence`` and stays report-only, exactly like any other unproven row.
       This proves the process is SOME clio product invocation, not specifically THIS
       server's child (see :func:`_has_clio_product_evidence`'s docstring for that
       scope limit).
    3. **Still dead at kill time**: a fresh ``psutil`` check (guarding the
       snapshot-to-kill window) re-confirms the parent is STILL dead before acting;
       a row whose parent came back alive in that window is skipped, never killed.
    4. **Not a recycled PID** (#1303 F3): the live process now holding ``row.pid`` must
       have the SAME creation time the snapshot recorded (:func:`_live_create_time`,
       1.0s tolerance, mirroring :func:`clio_agent.serve._pid_alive`). The OS can hand
       that PID to an unrelated process between snapshot and kill; a mismatch (or an
       unresolvable live create_time) is skipped typed ``pid_recycled``.

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
        skip_counts: Optional out-param (mutated in place, never read) -- when given,
            every typed skip reason (``never_reap_kind`` / ``no_clio_evidence`` /
            ``parent_alive_at_kill_time`` / ``pid_recycled``) increments its count here,
            so a caller (:func:`boot_reap_off_loop`) can log a skip-count-by-reason
            summary alongside the kill count instead of the per-pid lines being the
            only trace of a reap that quietly skipped everything (#1303 F5).

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

    def _record_skip(reason: str) -> None:
        if skip_counts is not None:
            skip_counts[reason] = skip_counts.get(reason, 0) + 1

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
            _record_skip("never_reap_kind")
            continue
        node = by_pid.get(row.pid)
        if node is None or not _has_clio_product_evidence(node):
            # #1303: name+dead-parent alone is NOT ownership evidence. No cmdline
            # marker match -> report-only, never a kill candidate (mirrors the
            # never_reap_kind / parent_alive_at_kill_time typed-skip shapes above).
            # logger.info (not .debug): this is the difference between "reap works"
            # and "reap is a permanent no-op" -- must not be invisible (#1303 F5).
            logger.info(
                "orphan_reap_skipped pid=%s name=%s kind=%s reason=no_clio_evidence",
                row.pid,
                row.name,
                row.kind,
            )
            _record_skip("no_clio_evidence")
            continue
        immediate_parent = node.ppid
        if _pid_alive(immediate_parent):
            # The snapshot-to-kill window closed: the parent came back (or a
            # slower scan caught it mid-restart). Never kill a child whose
            # parent is provably alive right now — re-probed next pass.
            logger.debug(
                "orphan_reap_skipped pid=%s name=%s reason=parent_alive_at_kill_time ppid=%s",
                row.pid,
                row.name,
                immediate_parent,
            )
            _record_skip("parent_alive_at_kill_time")
            continue
        current_create_time = _live_create_time(row.pid)
        if current_create_time is None or abs(current_create_time - node.create_time) >= 1.0:
            # #1303 F3: the PID was recycled (or is no longer resolvable) between
            # snapshot and kill -- never kill whatever now holds that number.
            logger.info(
                "orphan_reap_skipped pid=%s name=%s kind=%s reason=pid_recycled "
                "snapshot_create_time=%s live_create_time=%s",
                row.pid,
                row.name,
                row.kind,
                node.create_time,
                current_create_time,
            )
            _record_skip("pid_recycled")
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

    Also logs a skip-count-by-reason summary (#1303 F5) when the reap skipped
    anything: without it, a reap that skips EVERY candidate (e.g. the fleet
    genuinely carries no clio-owned orphans right now, or a marker regression made
    the evidence gate over-strict) looks identical in the boot log to "nothing to
    reap" -- the per-pid ``orphan_reap_skipped`` lines exist but are easy to miss
    among boot noise; this one-line summary is not.
    """

    import asyncio  # noqa: PLC0415
    import functools  # noqa: PLC0415
    import logging  # noqa: PLC0415

    loop = asyncio.get_running_loop()
    skip_counts: dict[str, int] = {}
    try:
        reaped = await loop.run_in_executor(
            None, functools.partial(reap_orphaned_processes, skip_counts=skip_counts)
        )
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
    if skip_counts:
        logging.getLogger(__name__).info(
            "boot orphan-process reap skip summary (#1303): %s",
            ", ".join(f"{reason}={count}" for reason, count in sorted(skip_counts.items())),
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
