"""Unit tests for the session process-hygiene audit (the daemon-ghost fix, PART A).

These drive :class:`tests._process_hygiene.ProcessHygieneAudit` with injected registry /
child / liveness snapshots so the audit's decision logic is deterministic and touches
neither the real ``~/.clio`` registry nor any spawned process. They pin the three
load-bearing behaviours:

* a client this process family registered and left DEAD is reported red, named by the
  test that introduced it (the sabotage the task calls for);
* a parallel CLIO instance's sibling client (not our descendant) is NEVER attributed to
  us, even if it dies mid-session (false-positive safety on this multi-instance box);
* a helper child left alive at session end is reported red.
"""

from __future__ import annotations

from typing import Optional

from tests._process_hygiene import (
    SKIP_ENV,
    AuditResult,
    LeakedChild,
    LeakedClient,
    ProcessHygieneAudit,
)

_ROOT = 1000  # the pretend pytest process pid


def _make_audit(
    *,
    clients: dict[int, Optional[float]],
    children: dict[int, str],
    descendants: set[int],
    dead: set[int],
) -> ProcessHygieneAudit:
    """Build an audit whose snapshots/liveness are fully injected (no real OS calls).

    ``clients`` / ``children`` are MUTATED by the test between observe/finalize to model
    processes appearing and dying; ``descendants`` marks which client pids are children
    of our root; ``dead`` marks which client pids are no longer alive.
    """
    return ProcessHygieneAudit(
        root_pid=_ROOT,
        _snapshot_clients=lambda: dict(clients),
        _snapshot_children=lambda _root: dict(children),
        _is_descendant=lambda pid, root: pid in descendants,
        _is_dead=lambda pid, ctime: pid in dead,
    )


def test_clean_run_has_no_leaks() -> None:
    """A run that introduces nothing new audits clean."""
    clients: dict[int, Optional[float]] = {_ROOT: 1.0}
    audit = _make_audit(clients=clients, children={}, descendants={_ROOT}, dead=set())
    audit.observe_test("t::a")
    result = audit.finalize(own_pid=_ROOT)
    assert result.clean
    assert result.client_leaks == ()
    assert result.child_leaks == ()


def test_dead_descendant_client_is_flagged_red_and_named() -> None:
    """A client we spawned that died without deregistering is named with its culprit test."""
    clients: dict[int, Optional[float]] = {_ROOT: 1.0}
    descendants = {_ROOT, 2222}
    dead: set[int] = set()
    audit = _make_audit(clients=clients, children={}, descendants=descendants, dead=dead)

    # During test 't::leaky', a subprocess we spawned attaches a client (pid 2222)...
    clients[2222] = 5.0
    audit.observe_test("tests/test_arc/test_x.py::t_leaky")
    # ...then it is hard-killed before deregistering: still in the registry, now dead.
    dead.add(2222)

    result = audit.finalize(own_pid=_ROOT)
    assert not result.clean
    assert result.client_leaks == (
        LeakedClient(pid=2222, origin="tests/test_arc/test_x.py::t_leaky"),
    )
    msg = result.format_failure()
    assert "pid=2222" in msg
    assert "t_leaky" in msg
    assert SKIP_ENV in msg  # the documented emergency opt-out is surfaced


def test_parallel_sibling_client_death_is_not_our_leak() -> None:
    """A parallel CLIO instance's client (not our descendant) is never blamed on us."""
    clients: dict[int, Optional[float]] = {_ROOT: 1.0}
    # pid 9999 belongs to a sibling process: it appears and later dies during our session,
    # but it is NOT a descendant of our root.
    audit = _make_audit(clients=clients, children={}, descendants={_ROOT}, dead={9999})
    clients[9999] = 7.0
    audit.observe_test("t::a")
    result = audit.finalize(own_pid=_ROOT)
    assert result.clean, "a parallel instance's client death must not fail our audit"


def test_baseline_entries_are_excluded() -> None:
    """Entries present at session start (prior run / live parallel) are baselined out."""
    clients: dict[int, Optional[float]] = {_ROOT: 1.0, 8888: 2.0}  # 8888 pre-exists
    audit = _make_audit(clients=clients, children={}, descendants={_ROOT, 8888}, dead={8888})
    audit.observe_test("t::a")  # 8888 already in baseline -> not tracked
    result = audit.finalize(own_pid=_ROOT)
    assert result.clean


def test_own_pid_never_reported() -> None:
    """The pytest host's own client (released at teardown) is excluded even if listed."""
    clients: dict[int, Optional[float]] = {}
    audit = _make_audit(clients=clients, children={}, descendants={_ROOT}, dead={_ROOT})
    clients[_ROOT] = 1.0
    audit.observe_test("t::a")
    result = audit.finalize(own_pid=_ROOT)
    assert result.clean


def test_surviving_child_is_flagged() -> None:
    """A helper child left alive at session end is a child leak."""
    children: dict[int, str] = {}
    audit = _make_audit(clients={_ROOT: 1.0}, children=children, descendants={_ROOT}, dead=set())
    children[3333] = "clio-kit.exe"  # a child appears and never dies
    result = audit.finalize(own_pid=_ROOT)
    assert not result.clean
    assert result.child_leaks == (LeakedChild(pid=3333, name="clio-kit.exe", origin="<unknown>"),)
    assert "clio-kit.exe" in result.format_failure()


def test_audit_result_clean_property() -> None:
    """AuditResult.clean is the empty-both predicate."""
    assert AuditResult((), ()).clean
    assert not AuditResult((LeakedClient(1, "t"),), ()).clean
    assert not AuditResult((), (LeakedChild(1, "x", "t"),)).clean
