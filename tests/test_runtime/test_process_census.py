"""Parent-chain census + orphan detection (#900, PART B; reap evidence gate #1303).

Pins the pure parentage classifier and the doctor row it feeds, plus a real spawn that
must descend from this process:

* a synthetic process table with a launcher that has EXITED (its child reparented) is
  classified ``orphaned_from_tree`` — the sabotage the task calls for;
* children of the server root and of the clio-core daemon root are classified to their
  correct root;
* the ``child_parentage`` doctor row goes DEGRADED (surfaced, not silent) on an orphan
  and READY otherwise;
* a really-spawned subprocess is a psutil descendant of the current process;
* #1303: the REAP requires POSITIVE cmdline evidence (a conservative clio marker) before
  killing an ``orphaned_from_tree`` row — name-substring (``uv``/``python``/``node``/...)
  plus a dead parent is NOT ownership evidence (every detached job on Windows has a dead
  parent), so a row with no matching cmdline is skipped typed ``no_clio_evidence``
  and never killed. The REPORT (``classify_parentage`` / ``probe_process_parentage``)
  stays machine-wide and unchanged — reporting never kills.
* #1303 review round 2: the marker set is PRECISE invocation tokens only (mirroring
  ``disk_gc._CLIO_MARKERS``'s rationale, as its own separate constant) — a bare
  ``clio-agent``/``clio_agent`` substring would still match the venv interpreter's own
  absolute path (every ``uv run`` in this checkout puts the
  ``clio-agent/.venv/Scripts`` interpreter path in argv[0]) and relocate the same bug
  one hop down a detached launch chain; the evidence function was renamed
  ``_has_clio_product_evidence`` to be honest that it proves "some clio product
  process", not "this server's own child"; the reap now ALSO re-verifies each
  candidate's live creation time against the snapshot (PID-reuse defeat) before
  killing; and a skip-count-by-reason boot summary was added, since a reap that
  quietly skips everything must not look identical to "nothing to reap".
* #1303 review round 3: split the create-time recheck's reason into ``pid_recycled``
  (a CONFIRMED mismatch) and ``pid_identity_unverified`` (an unresolvable lookup —
  ``NoSuchProcess``/``AccessDenied``/no psutil); the per-pid ``no_clio_evidence`` skip
  moved back to ``DEBUG`` (the AGGREGATE skip-count summary is what satisfies
  visibility — one INFO line per unrelated machine-wide process on every boot would be
  noise); and every negative reap test now pins ``_live_create_time`` via
  ``_pin_live_create_time_matches_snapshot`` — without it, a synthetic pid's real
  (unmocked) ``_live_create_time`` resolves to ``None`` on a real machine, so the NEW
  ``pid_identity_unverified`` gate would incidentally skip the row AFTER the guard
  each test isolates, masking that guard's own removal (proven live: deleting the
  evidence gate, or emptying ``_NEVER_REAP_KINDS``, left the unpinned suite green with
  ``skip_counts == {"pid_identity_unverified": 1}`` instead of red).
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


def _node(
    pid: int,
    ppid: int,
    name: str,
    *,
    ctime: float = 0.0,
    cmdline: tuple[str, ...] = (),
) -> pc.ProcessNode:
    return pc.ProcessNode(
        pid=pid,
        ppid=ppid,
        name=name,
        create_time=ctime,
        kind=pc._classify_child(name),
        cmdline=cmdline,
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


def test_snapshot_attaches_live_cmdline_only_to_orphan_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1303 F2b: pin the LIVE capture wiring in ``_snapshot_process_nodes`` itself.

    No other test in this module exercises the actual
    ``raw[node.pid] = replace(node, cmdline=_process_cmdline(node.pid))`` line -- every
    other reap test injects ``nodes=`` directly, bypassing ``_snapshot_process_nodes``
    entirely. Deleting that line would leave the WHOLE suite green while making the
    live reap a permanent no-op (every candidate's cmdline would stay ``()`` forever,
    so ``_has_clio_product_evidence`` would never see a match). This test monkeypatches
    ``psutil.process_iter`` with a synthetic process table (so it stays a fast, real
    psutil-free unit test) and asserts the capture happened for the reparented-orphan
    candidate specifically, and did NOT happen for a properly-rooted descendant (which
    never needs it -- it is never an orphan candidate).
    """
    psutil = pytest.importorskip("psutil")

    server_info = {"pid": _SERVER, "ppid": 1, "name": "python.exe", "create_time": 0.0}
    rooted_child_info = {
        "pid": 300,
        "ppid": _SERVER,
        "name": "clio-kit.exe",
        "create_time": 0.0,
    }
    # Orphan candidate: a clio-kind process (name-classifies to mcp_launcher) whose
    # parent pid (650) is absent from the table -> "dead" -> reparented-orphan pass.
    orphan_candidate_info = {"pid": 700, "ppid": 650, "name": "uv.exe", "create_time": 0.0}

    class _FakeProc:
        def __init__(self, info: dict) -> None:
            self.info = info

    def _fake_process_iter(attrs: list[str]) -> list[_FakeProc]:
        return [
            _FakeProc(server_info),
            _FakeProc(rooted_child_info),
            _FakeProc(orphan_candidate_info),
        ]

    live_marker = ("uv", "run", "clio-kit", "mcp-server")
    monkeypatch.setattr(psutil, "process_iter", _fake_process_iter)
    monkeypatch.setattr(pc, "_process_cmdline", lambda pid: live_marker if pid == 700 else ())

    nodes = pc._snapshot_process_nodes(_SERVER, None)
    by_pid = {n.pid: n for n in nodes}

    assert by_pid[700].cmdline == live_marker, (
        "the reparented-orphan candidate must carry the LIVE-captured cmdline -- if "
        "this is empty, the `replace(node, cmdline=_process_cmdline(...))` wiring in "
        "_snapshot_process_nodes was removed/broken and the reap is now a no-op"
    )
    assert by_pid[300].cmdline == (), (
        "a properly-rooted descendant is never an orphan candidate and must never pay "
        "for (or receive) a live cmdline capture"
    )


def _pin_live_create_time_matches_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1303 F3: make every synthetic node's live create_time match its snapshot value.

    All ``_node()`` calls default ``ctime=0.0`` with no real process behind the pid, so
    the reap's PID-reuse recheck (:func:`clio_agent.runtime.process_census._live_create_time`)
    would otherwise call the REAL psutil against a pid that (almost certainly) does not
    exist on the test machine and read that as ``pid_recycled``. Tests exercising the
    actual kill path pin this so the recheck passes deterministically, isolating the
    behavior each test targets.
    """
    monkeypatch.setattr(pc, "_live_create_time", lambda pid: 0.0)


def test_reap_kills_provably_orphaned_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1232 pt 4 / #1303: a dead-parent CLIO child WITH cmdline evidence is REAPED."""
    _pin_live_create_time_matches_snapshot(monkeypatch)
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(_DAEMON, 1, "clio_run.exe"),
        _node(
            700,
            650,
            "clio-kit.exe",  # parent 650 is GONE from the table
            cmdline=("clio-kit.exe", "mcp-server", "ndp"),
        ),
    ]
    killed: list[int] = []
    reaped = pc.reap_orphaned_processes(
        nodes=nodes,
        server_root_pid=_SERVER,
        daemon_root_pid=_DAEMON,
        kill=killed.append,
    )
    assert killed == [700]
    assert len(reaped) == 1
    assert reaped[0] == pc.ReapedProcess(pid=700, name="clio-kit.exe", kind="mcp_stdio")


def test_reap_skips_dead_parent_with_unrelated_cmdline(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1303: name-substring (``uv.exe``) + dead parent is NOT ownership evidence.

    Live evidence (2026-09-03): a gact boot killed pid 43472, an unrelated detached
    ``uv run python ...`` launcher, purely because ``"uv"`` name-matched ``mcp_launcher``
    and its transient shell parent had exited -- the completely normal detached-launch
    shape on Windows. This process's cmdline never names a clio entry point; it must NOT
    be reaped, and the skip must be typed ``no_clio_evidence``.

    #1303 review round 3: pins ``_live_create_time`` so this negative result is
    PROVABLY caused by the evidence gate, not incidentally masked by the (unrelated)
    ``pid_recycled``/``pid_identity_unverified`` gate that runs later -- on a real
    machine pid 43472 almost certainly does not exist, so an unpinned
    ``_live_create_time`` would return ``None`` and skip the row for THAT reason even
    if the evidence gate were deleted, leaving this test green for the wrong reason.
    """
    _pin_live_create_time_matches_snapshot(monkeypatch)
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(
            43472,
            999,  # the transient PowerShell parent has exited -> classic detached-launch shape
            "uv.exe",
            cmdline=("uv", "run", "python", "my_script.py"),
        ),
    ]
    killed: list[int] = []
    reaped = pc.reap_orphaned_processes(
        nodes=nodes, server_root_pid=_SERVER, daemon_root_pid=None, kill=killed.append
    )
    assert killed == []
    assert reaped == []


def test_reap_skips_dead_parent_with_empty_cmdline(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1303: an unresolved (``AccessDenied``-shaped, empty) cmdline is NO evidence.

    #1303 review round 3: pinned so this is provably the evidence gate, not an
    incidental ``pid_identity_unverified`` masking (see the sibling
    ``test_reap_skips_dead_parent_with_unrelated_cmdline`` docstring for the mechanism).
    """
    _pin_live_create_time_matches_snapshot(monkeypatch)
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(500, 999, "python.exe", cmdline=()),  # cmdline never resolved
    ]
    killed: list[int] = []
    reaped = pc.reap_orphaned_processes(
        nodes=nodes, server_root_pid=_SERVER, daemon_root_pid=None, kill=killed.append
    )
    assert killed == []
    assert reaped == []


def test_reap_kills_dead_parent_with_clio_cmdline_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1303: a dead-parent ``uv.exe`` whose cmdline actually launches clio-kit IS reaped."""
    _pin_live_create_time_matches_snapshot(monkeypatch)
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(
            600,
            999,
            "uv.exe",
            cmdline=("uv", "run", "clio-kit", "mcp-server", "ndp"),
        ),
    ]
    killed: list[int] = []
    reaped = pc.reap_orphaned_processes(
        nodes=nodes, server_root_pid=_SERVER, daemon_root_pid=None, kill=killed.append
    )
    assert killed == [600]
    assert len(reaped) == 1
    assert reaped[0] == pc.ReapedProcess(pid=600, name="uv.exe", kind="mcp_launcher")


def test_reap_skips_repo_path_cmdline_not_a_clio_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1303 F1: a cmdline that merely sits under a ``clio-agent`` checkout path (the
    venv interpreter's own absolute path) is NOT clio ownership evidence -- only a
    PRECISE invocation token is (see ``_CLIO_CMDLINE_MARKERS``, mirroring
    ``disk_gc._CLIO_MARKERS``'s "no bare repo-path string" rationale). A bare
    ``clio-agent``/``clio_agent`` marker would still have killed a detached leg-runner
    script one hop down its own launch chain -- live-verified: every ``uv run`` in this
    checkout puts the venv interpreter's path in argv[0] -- the same #1303 bug
    relocated rather than fixed.

    #1303 review round 3: pinned so this is provably the evidence gate, not an
    incidental ``pid_identity_unverified`` masking.
    """
    _pin_live_create_time_matches_snapshot(monkeypatch)
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(
            800,
            999,  # dead parent -> reparented-orphan candidate
            "python.exe",
            cmdline=(
                r"D:\proj\clio-agent\.venv\Scripts\python.exe",
                r"scripts\live_verification\leg_c.py",
            ),
        ),
    ]
    killed: list[int] = []
    reaped = pc.reap_orphaned_processes(
        nodes=nodes, server_root_pid=_SERVER, daemon_root_pid=None, kill=killed.append
    )
    assert killed == []
    assert reaped == []


def test_reap_excludes_daemon_root_by_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1232 pt 4: the breakaway clio-core daemon is NEVER a kill candidate.

    ``classify_parentage`` skips both root pids entirely (they are never
    emitted as rows), so the daemon root can never be reaped no matter how
    stale ITS own parent chain looks -- prove it directly: a daemon root
    whose own "parent" is long gone still yields zero kills.
    """
    _pin_live_create_time_matches_snapshot(monkeypatch)
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(_DAEMON, 99999, "clio_run.exe"),  # daemon's own ppid is dead/unknown
        _node(
            700,
            650,
            "clio-kit.exe",  # a genuine orphan, for contrast
            cmdline=("clio-kit.exe", "mcp-server", "ndp"),
        ),
    ]
    killed: list[int] = []
    reaped = pc.reap_orphaned_processes(
        nodes=nodes,
        server_root_pid=_SERVER,
        daemon_root_pid=_DAEMON,
        kill=killed.append,
    )
    assert _DAEMON not in killed
    assert killed == [700]
    assert all(r.pid != _DAEMON for r in reaped)


def test_reap_never_kills_any_clio_core_daemon_kind_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    #1303 F2a hardening: pid 900 carries REAL clio cmdline evidence, so the evidence
    gate alone would NOT have blocked this kill -- the ``_NEVER_REAP_KINDS`` guard is
    the only thing preventing it, which is exactly what this test must isolate.

    #1303 review round 3: ``_live_create_time`` is ALSO pinned, so if the
    ``_NEVER_REAP_KINDS`` guard were ever deleted, the row would proceed all the way
    to a real kill (provably exposing the regression) instead of being incidentally
    caught by the later ``pid_identity_unverified`` gate.
    """
    _pin_live_create_time_matches_snapshot(monkeypatch)
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(_DAEMON, 1, "clio_run.exe"),  # the recorded (correctly-identified) daemon
        _node(
            900,
            88888,  # dead parent
            "clio_run.exe",  # a DIFFERENT clio_run.exe
            cmdline=("clio_run.exe", "--port", "9413"),
        ),
    ]
    killed: list[int] = []
    reaped = pc.reap_orphaned_processes(
        nodes=nodes,
        server_root_pid=_SERVER,
        daemon_root_pid=_DAEMON,
        kill=killed.append,
    )
    # It still surfaces as a reportable orphan (unchanged doctor visibility)...
    rows = pc.classify_parentage(nodes, server_root_pid=_SERVER, daemon_root_pid=_DAEMON)
    orphan = next(r for r in rows if r.pid == 900)
    assert orphan.descends_from == pc.ORPHANED_FROM_TREE
    assert orphan.kind == "clio_core_daemon"
    # ...but is NEVER auto-killed.
    assert killed == []
    assert reaped == []


def test_reap_skips_when_parent_alive_at_kill_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """A parent that came back alive between snapshot and kill is NEVER reaped.

    #1303: cmdline evidence is attached so this row clears the evidence gate and the
    parent-alive recheck is actually the thing that skips it (not a false-positive
    evidence skip masking the behavior under test).

    #1303 review round 3: ``_live_create_time`` is ALSO pinned, so if the
    parent-alive recheck were ever deleted, the row would proceed all the way to a
    real kill instead of being incidentally caught by the later pid-identity gate.
    """
    _pin_live_create_time_matches_snapshot(monkeypatch)
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(
            700,
            650,  # snapshot says parent 650 is dead
            "clio-kit.exe",
            cmdline=("clio-kit.exe", "mcp-server", "ndp"),
        ),
    ]
    monkeypatch.setattr(pc, "_pid_alive", lambda pid: pid == 650)  # ...but it is alive NOW
    killed: list[int] = []
    reaped = pc.reap_orphaned_processes(
        nodes=nodes, server_root_pid=_SERVER, daemon_root_pid=None, kill=killed.append
    )
    assert killed == []
    assert reaped == []


def test_reap_ignores_non_orphaned_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cleanly-rooted process is never a kill candidate.

    #1303 F2a hardening: pid 300 carries REAL clio cmdline evidence, so the evidence
    gate alone would NOT have blocked this kill -- the ``descends_from`` check (never
    consider a properly-rooted row at all) is the only thing preventing it.

    #1303 review round 3: ``_live_create_time`` is ALSO pinned, so if the
    ``descends_from`` guard were ever deleted, the row would proceed all the way to a
    real kill instead of being incidentally caught by the later pid-identity gate.
    """
    _pin_live_create_time_matches_snapshot(monkeypatch)
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(300, _SERVER, "clio-kit.exe", cmdline=("clio-kit.exe", "mcp-server", "ndp")),
    ]
    killed: list[int] = []
    reaped = pc.reap_orphaned_processes(
        nodes=nodes, server_root_pid=_SERVER, daemon_root_pid=None, kill=killed.append
    )
    assert killed == []
    assert reaped == []


def test_reap_continues_after_one_kill_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A kill that raises (already exited, access denied) is typed-skipped, never aborts the pass."""
    _pin_live_create_time_matches_snapshot(monkeypatch)
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(700, 650, "clio-kit.exe", cmdline=("clio-kit.exe", "mcp-server", "ndp")),
        _node(701, 651, "uvx.exe", cmdline=("uvx", "clio-kit", "mcp-server")),
    ]

    def _flaky_kill(pid: int) -> None:
        if pid == 700:
            raise ProcessLookupError("already exited")

    reaped = pc.reap_orphaned_processes(
        nodes=nodes, server_root_pid=_SERVER, daemon_root_pid=None, kill=_flaky_kill
    )
    assert [r.pid for r in reaped] == [701]


def test_reap_skips_recycled_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1303 F3: a PID whose LIVE creation time no longer matches the snapshot's is a
    RECYCLED pid -- the OS handed that number to an unrelated process between snapshot
    and kill. Never kill it; typed skip ``reason=pid_recycled`` (mirrors
    ``clio_agent.serve._pid_alive``'s PID-reuse defeat, 1.0s tolerance).

    #1303 review round 3: asserts the DISTINCT ``pid_recycled`` token (split from the
    ``pid_identity_unverified`` unresolvable-lookup case below) via ``skip_counts``.
    """
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(
            700,
            650,
            "clio-kit.exe",
            ctime=1000.0,  # the snapshot's recorded creation time
            cmdline=("clio-kit.exe", "mcp-server", "ndp"),
        ),
    ]
    # The live process now AT pid 700 has a DIFFERENT creation time -> recycled.
    monkeypatch.setattr(pc, "_live_create_time", lambda pid: 5000.0)
    killed: list[int] = []
    skip_counts: dict[str, int] = {}
    reaped = pc.reap_orphaned_processes(
        nodes=nodes,
        server_root_pid=_SERVER,
        daemon_root_pid=None,
        kill=killed.append,
        skip_counts=skip_counts,
    )
    assert killed == []
    assert reaped == []
    assert skip_counts == {"pid_recycled": 1}


def test_reap_skips_recycled_pid_when_live_create_time_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1303 F3: an unresolvable live create_time (``NoSuchProcess``/``AccessDenied``
    shape, ``None``) is conservatively NOT killed.

    #1303 review round 3: this is now its OWN typed reason, ``pid_identity_unverified``
    -- split from ``pid_recycled`` (a confirmed MISMATCH) since "identity could not be
    confirmed" and "identity was confirmed different" are distinct failure shapes worth
    distinguishing in the skip-count summary. Asserted via ``skip_counts``.
    """
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(
            700,
            650,
            "clio-kit.exe",
            cmdline=("clio-kit.exe", "mcp-server", "ndp"),
        ),
    ]
    monkeypatch.setattr(pc, "_live_create_time", lambda pid: None)
    killed: list[int] = []
    skip_counts: dict[str, int] = {}
    reaped = pc.reap_orphaned_processes(
        nodes=nodes,
        server_root_pid=_SERVER,
        daemon_root_pid=None,
        kill=killed.append,
        skip_counts=skip_counts,
    )
    assert killed == []
    assert reaped == []
    assert skip_counts == {"pid_identity_unverified": 1}


def test_reap_records_skip_counts_by_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1303 F5: the ``skip_counts`` out-param tallies every typed skip reason, so a
    caller (the boot hook) can log a skip-count-by-reason summary instead of the
    per-pid lines being the only trace of a reap that skipped everything.

    #1303 review round 3: extended to also cover the two SPLIT create-time reasons
    (``pid_recycled`` for a confirmed mismatch, ``pid_identity_unverified`` for an
    unresolvable lookup) alongside the two pre-existing reasons, so all four distinct
    tokens are pinned in one place.
    """
    nodes = [
        _node(_SERVER, 1, "python.exe"),
        _node(_DAEMON, 1, "clio_run.exe"),
        # never_reap_kind: a different clio_run.exe, dead parent, real evidence.
        _node(900, 88888, "clio_run.exe", cmdline=("clio_run.exe", "--port", "9413")),
        # no_clio_evidence: dead parent, no clio marker in cmdline.
        _node(910, 77777, "uv.exe", cmdline=("uv", "run", "python", "unrelated.py")),
        # pid_recycled: real evidence, dead parent, live create_time MISMATCHES.
        _node(
            920,
            66666,
            "clio-kit.exe",
            ctime=1000.0,
            cmdline=("clio-kit.exe", "mcp-server", "ndp"),
        ),
        # pid_identity_unverified: real evidence, dead parent, live create_time UNRESOLVABLE.
        _node(930, 55555, "clio-kit.exe", cmdline=("clio-kit.exe", "mcp-server", "ndp")),
    ]

    def _fake_live_create_time(pid: int) -> float | None:
        if pid == 920:
            return 9999.0  # mismatches node 920's ctime=1000.0
        if pid == 930:
            return None
        return 0.0  # unreached by the other nodes (blocked at an earlier gate)

    monkeypatch.setattr(pc, "_live_create_time", _fake_live_create_time)
    skip_counts: dict[str, int] = {}
    reaped = pc.reap_orphaned_processes(
        nodes=nodes,
        server_root_pid=_SERVER,
        daemon_root_pid=_DAEMON,
        kill=lambda pid: None,
        skip_counts=skip_counts,
    )
    assert reaped == []
    assert skip_counts == {
        "never_reap_kind": 1,
        "no_clio_evidence": 1,
        "pid_recycled": 1,
        "pid_identity_unverified": 1,
    }


@pytest.mark.asyncio
async def test_boot_reap_off_loop_runs_the_reap_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boot hook runs the reap off-loop and swallows a failure (never breaks boot).

    #1303 F5: the mocks accept ``**kwargs`` since ``boot_reap_off_loop`` now passes a
    ``skip_counts`` out-param for the boot-time skip-count-by-reason summary.
    """
    calls: list[str] = []
    monkeypatch.setattr(pc, "reap_orphaned_processes", lambda **kwargs: calls.append("ran") or [])
    await pc.boot_reap_off_loop()
    assert calls == ["ran"]

    def _boom(**kwargs: object) -> list[pc.ReapedProcess]:
        raise RuntimeError("boom")

    monkeypatch.setattr(pc, "reap_orphaned_processes", _boom)
    await pc.boot_reap_off_loop()  # must not raise


@pytest.mark.asyncio
async def test_boot_reap_off_loop_logs_skip_count_summary(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#1303 F5: the boot hook logs a skip-count-by-reason summary when the reap
    skipped anything, so a reap that quietly skips everything is not indistinguishable
    from "nothing to reap" in the boot log.
    """

    def _fake_reap(**kwargs: object) -> list[pc.ReapedProcess]:
        skip_counts = kwargs.get("skip_counts")
        assert isinstance(skip_counts, dict)
        skip_counts["no_clio_evidence"] = 3
        skip_counts["pid_recycled"] = 1
        return []

    monkeypatch.setattr(pc, "reap_orphaned_processes", _fake_reap)
    with caplog.at_level("INFO", logger="clio_agent.runtime.process_census"):
        await pc.boot_reap_off_loop()
    assert any(
        "no_clio_evidence=3" in record.getMessage() and "pid_recycled=1" in record.getMessage()
        for record in caplog.records
    )


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
