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

import pytest

from clio_agent.runtime import process_census as pc
from clio_agent.runtime.status import IntegrationState

_SERVER = 100
_DAEMON = 200


def _node(pid: int, ppid: int, name: str, *, ctime: float = 0.0) -> pc.ProcessNode:
    return pc.ProcessNode(
        pid=pid, ppid=ppid, name=name, create_time=ctime, kind=pc._classify_child(name)
    )


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
