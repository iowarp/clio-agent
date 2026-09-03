"""Parent-chain census + orphan detection (#900, PART B).

Pins the pure parentage classifier and the doctor row it feeds, plus a real spawn that
must descend from this process:

* a synthetic process table with a launcher that has EXITED (its child reparented) is
  classified ``orphaned_from_tree`` — the sabotage the task calls for;
* children of the server root and of the clio-core daemon root are classified to their
  correct root;
* the ``child_parentage`` doctor row goes DEGRADED (surfaced, not silent) on an orphan
  and READY otherwise;
* a really-spawned subprocess is a psutil descendant of the current process.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from clio_agent.runtime import process_census as pc
from clio_agent.runtime.status import IntegrationState

_SERVER = 100
_DAEMON = 200


def _node(
    pid: int,
    ppid: int,
    name: str,
    *,
    ctime: float = 0.0,
    executable: str = "",
    cwd: str = "",
) -> pc.ProcessNode:
    return pc.ProcessNode(
        pid=pid,
        ppid=ppid,
        name=name,
        create_time=ctime,
        kind=pc._classify_child(name),
        executable=executable,
        cwd=cwd,
    )


def test_detached_process_requires_runtime_path_ownership(tmp_path: Path) -> None:
    """A parallel installation's surviving daemon is not this server's orphan."""

    owner_path = tmp_path / "contained" / "worktrees" / "clio-agent"
    owner_root = str(owner_path)
    owned = _node(
        700,
        650,
        "clio_run.exe",
        executable=str(owner_path / ".venv/Lib/site-packages/iowarp_core/bin/clio_run"),
        cwd=owner_root,
    )
    parallel_root = tmp_path / "other-stack"
    parallel = _node(
        701,
        651,
        "clio_run.exe",
        executable=str(parallel_root / ".venv/Lib/site-packages/iowarp_core/bin/clio_run"),
        cwd=str(parallel_root),
    )

    assert pc._belongs_to_runtime(owned, (owner_root,)) is True
    assert pc._belongs_to_runtime(parallel, (owner_root,)) is False


def test_children_classified_to_their_root() -> None:
    """MCP child under the server, daemon-spawned proc under the daemon."""
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(_DAEMON, 1, "clio_run.exe"),
        _node(300, _SERVER, "clio-kit.exe"),  # MCP stdio child of the server
        _node(400, 300, "uvx.exe"),  # grandchild via the launcher -> still server
        _node(500, _DAEMON, "python.exe"),  # a daemon-side child
    ]
    rows = pc.classify_parentage(nodes, server_root_pid=_SERVER, daemon_root_pid=_DAEMON)
    by_pid = {r.pid: r for r in rows}
    assert by_pid[300].descends_from == pc.DESCENDS_SERVER_ROOT
    assert by_pid[400].descends_from == pc.DESCENDS_SERVER_ROOT
    assert by_pid[400].parent_chain == (300, _SERVER)
    assert by_pid[500].descends_from == pc.DESCENDS_DAEMON_ROOT
    # The roots themselves are not emitted as rows.
    assert _SERVER not in by_pid and _DAEMON not in by_pid


def test_orphaned_child_after_launcher_exit_is_flagged() -> None:
    """SABOTAGE: an intermediary launcher exited; its child reparented -> orphaned_from_tree.

    pid 700 (a clio-kit MCP child) was spawned by launcher pid 650, which has since
    EXITED (it is absent from the process table). 700's chain therefore reaches neither
    root and must be flagged.
    """
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(_DAEMON, 1, "clio_run.exe"),
        _node(700, 650, "clio-kit.exe"),  # parent 650 is GONE from the table
    ]
    rows = pc.classify_parentage(nodes, server_root_pid=_SERVER, daemon_root_pid=_DAEMON)
    orphan = next(r for r in rows if r.pid == 700)
    assert orphan.descends_from == pc.ORPHANED_FROM_TREE
    assert orphan.parent_chain == ()  # dead parent -> chain terminates immediately


def test_non_clio_processes_are_ignored() -> None:
    """A random non-CLIO process is never classified (kind == 'other')."""
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(800, 1, "explorer.exe"),  # unrelated desktop process
    ]
    rows = pc.classify_parentage(nodes, server_root_pid=_SERVER, daemon_root_pid=None)
    assert all(r.pid != 800 for r in rows)


def test_probe_row_degraded_on_orphan() -> None:
    """The doctor row surfaces an orphan as DEGRADED with the named culprit (no silent path)."""
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(900, 42, "claude.exe"),  # parent 42 absent -> orphan
    ]
    row = pc.probe_process_parentage(nodes=nodes, server_root_pid=_SERVER, daemon_root_pid=None)
    assert row.name == "child_parentage"
    assert row.state is IntegrationState.DEGRADED
    assert row.details["orphan_count"] == 1
    assert "claude.exe(pid=900)" in row.summary
    assert row.fallback == "orphans-idle-until-manually-reaped"


def test_probe_row_ready_when_all_rooted() -> None:
    """All-rooted tree is a clean READY row."""
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(910, _SERVER, "clio-kit.exe"),
    ]
    row = pc.probe_process_parentage(nodes=nodes, server_root_pid=_SERVER, daemon_root_pid=None)
    assert row.state is IntegrationState.READY
    assert row.details["orphan_count"] == 0
    assert row.details["count"] == 1


def test_reap_kills_provably_orphaned_process() -> None:
    """#1232 pt 4: a dead-parent CLIO child is REAPED (killed), not just reported."""
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(_DAEMON, 1, "clio_run.exe"),
        _node(700, 650, "clio-kit.exe"),  # parent 650 is GONE from the table
    ]
    killed: list[int] = []
    reaped = pc.reap_orphaned_processes(
        nodes=nodes,
        server_root_pid=_SERVER,
        daemon_root_pid=_DAEMON,
        kill=killed.append,
        parent_alive=lambda _pid: False,
    )
    assert killed == [700]
    assert len(reaped) == 1
    assert reaped[0] == pc.ReapedProcess(pid=700, name="clio-kit.exe", kind="mcp_stdio")


def test_reap_excludes_daemon_root_by_construction() -> None:
    """#1232 pt 4: the breakaway clio-core daemon is NEVER a kill candidate.

    ``classify_parentage`` skips both root pids entirely (they are never
    emitted as rows), so the daemon root can never be reaped no matter how
    stale ITS own parent chain looks -- prove it directly: a daemon root
    whose own "parent" is long gone still yields zero kills.
    """
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(_DAEMON, 99999, "clio_run.exe"),  # daemon's own ppid is dead/unknown
        _node(700, 650, "clio-kit.exe"),  # a genuine orphan, for contrast
    ]
    killed: list[int] = []
    reaped = pc.reap_orphaned_processes(
        nodes=nodes,
        server_root_pid=_SERVER,
        daemon_root_pid=_DAEMON,
        kill=killed.append,
        parent_alive=lambda _pid: False,
    )
    assert _DAEMON not in killed
    assert killed == [700]
    assert all(r.pid != _DAEMON for r in reaped)


def test_reap_excludes_live_server_launcher_ancestor() -> None:
    """A bootstrap launcher above the server root is not an orphaned child.

    Windows venv launchers can outlive their shell while owning the Job Object
    that contains the real interpreter. Killing that launcher during server
    startup also kills the server, so the current server's ancestor chain must
    never enter the reap candidate set.
    """
    launcher_pid = 90
    nodes = [
        _node(launcher_pid, 1, "python.exe"),
        _node(_SERVER, launcher_pid, "python.exe"),
        _node(700, 650, "clio-kit.exe"),  # genuine dead-parent orphan
    ]
    killed: list[int] = []
    reaped = pc.reap_orphaned_processes(
        nodes=nodes,
        server_root_pid=_SERVER,
        daemon_root_pid=None,
        kill=killed.append,
        parent_alive=lambda _pid: False,
    )
    assert killed == [700]
    assert [row.pid for row in reaped] == [700]


def test_reap_never_kills_any_clio_core_daemon_kind_row() -> None:
    """#1232 pt 4 safety hardening: a live test caught this exact scenario killing a
    REAL, in-use clio-core daemon holding live session data.

    A ``clio_core_daemon``-kind process that classifies as ``orphaned_from_tree`` is
    NOT automatically the stale corpse the issue describes: a multi-daemon
    environment (e.g. a session-isolated/private daemon whose pidfile the single
    ``daemon_root_pid`` lookup does not resolve) can make a genuinely-live,
    currently-in-use daemon look identical to a stale one by parentage alone. This
    row -- pid 900, kind clio_core_daemon, dead parent, NOT the recorded daemon
    root -- is exactly the shape that got auto-killed during development. It must
    now be report-only (never a kill candidate), regardless of parentage.
    """
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(_DAEMON, 1, "clio_run.exe"),  # the recorded (correctly-identified) daemon
        _node(900, 88888, "clio_run.exe"),  # a DIFFERENT clio_run.exe, dead parent
    ]
    killed: list[int] = []
    reaped = pc.reap_orphaned_processes(
        nodes=nodes,
        server_root_pid=_SERVER,
        daemon_root_pid=_DAEMON,
        kill=killed.append,
        parent_alive=lambda _pid: False,
    )
    # It still surfaces as a reportable orphan (unchanged doctor visibility)...
    rows = pc.classify_parentage(nodes, server_root_pid=_SERVER, daemon_root_pid=_DAEMON)
    orphan = next(r for r in rows if r.pid == 900)
    assert orphan.descends_from == pc.ORPHANED_FROM_TREE
    assert orphan.kind == "clio_core_daemon"
    # ...but is NEVER auto-killed.
    assert killed == []
    assert reaped == []


def test_reap_skips_when_parent_alive_at_kill_time() -> None:
    """A parent that came back alive between snapshot and kill is NEVER reaped."""
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(700, 650, "clio-kit.exe"),  # snapshot says parent 650 is dead
    ]
    killed: list[int] = []
    reaped = pc.reap_orphaned_processes(
        nodes=nodes,
        server_root_pid=_SERVER,
        daemon_root_pid=None,
        kill=killed.append,
        parent_alive=lambda pid: pid == 650,
    )
    assert killed == []
    assert reaped == []


def test_reap_ignores_non_orphaned_rows() -> None:
    """A cleanly-rooted process is never a kill candidate."""
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(300, _SERVER, "clio-kit.exe"),
    ]
    killed: list[int] = []
    reaped = pc.reap_orphaned_processes(
        nodes=nodes, server_root_pid=_SERVER, daemon_root_pid=None, kill=killed.append
    )
    assert killed == []
    assert reaped == []


def test_reap_continues_after_one_kill_failure() -> None:
    """A kill that raises (already exited, access denied) is typed-skipped, never aborts the pass."""
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(700, 650, "clio-kit.exe"),
        _node(701, 651, "uvx.exe"),
    ]

    def _flaky_kill(pid: int) -> None:
        if pid == 700:
            raise ProcessLookupError("already exited")

    reaped = pc.reap_orphaned_processes(
        nodes=nodes,
        server_root_pid=_SERVER,
        daemon_root_pid=None,
        kill=_flaky_kill,
        parent_alive=lambda _pid: False,
    )
    assert [r.pid for r in reaped] == [701]


@pytest.mark.asyncio
async def test_boot_reap_off_loop_runs_the_reap_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boot hook runs the reap off-loop and swallows a failure (never breaks boot)."""
    calls: list[str] = []
    monkeypatch.setattr(pc, "reap_orphaned_processes", lambda: calls.append("ran") or [])
    await pc.boot_reap_off_loop()
    assert calls == ["ran"]

    def _boom() -> list[pc.ReapedProcess]:
        raise RuntimeError("boom")

    monkeypatch.setattr(pc, "reap_orphaned_processes", _boom)
    await pc.boot_reap_off_loop()  # must not raise


def test_real_spawn_is_descendant_of_this_process() -> None:
    """A really-spawned child is a psutil descendant of the current process (live path)."""
    psutil = pytest.importorskip("psutil")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        deadline = time.time() + 10
        me = psutil.Process()
        while time.time() < deadline:
            kids = {c.pid for c in me.children(recursive=True)}
            if proc.pid in kids:
                break
            time.sleep(0.05)
        assert proc.pid in {c.pid for c in me.children(recursive=True)}, (
            "spawned child did not descend from this process — parentage severed"
        )
        # And the live parentage probe, rooted here, classifies it under the server root.
        nodes = pc._snapshot_process_nodes(me.pid, None)
        rows = pc.classify_parentage(nodes, server_root_pid=me.pid, daemon_root_pid=None)
        child = next((r for r in rows if r.pid == proc.pid), None)
        if child is not None:  # python.exe is a CLIO kind ('python_child'); it should appear
            assert child.descends_from == pc.DESCENDS_SERVER_ROOT
    finally:
        proc.kill()
        proc.wait(timeout=10)
