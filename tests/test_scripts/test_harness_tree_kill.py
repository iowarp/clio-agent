"""Harness discipline: measurement scripts must TREE-kill their spawned server (#900).

``profile_session_memory.py`` and ``run_thinking_experiment.py`` boot a real gact server
that fans out into MCP stdio children + a pooled SDK CLI process. A plain
``proc.terminate()`` on the parent orphans that tree (on Windows terminating the parent
never reaps it). Both scripts must route teardown through the audited tree-kill
(``clio_agent.serve._terminate_tree``). These pins fail if a script regresses to a
parent-only terminate.
"""

from __future__ import annotations

import pytest

from scripts import profile_session_memory, run_thinking_experiment


class _FakeProc:
    """Minimal Popen stand-in exposing only the ``pid`` the tree-kill needs."""

    def __init__(self, pid: int) -> None:
        self.pid = pid


@pytest.mark.parametrize(
    "module",
    [profile_session_memory, run_thinking_experiment],
    ids=["profile_session_memory", "run_thinking_experiment"],
)
def test_harness_terminates_the_whole_tree(module, monkeypatch: pytest.MonkeyPatch) -> None:
    """The script's teardown helper delegates to the trusted recursive tree-kill."""
    from clio_agent import serve

    seen: list[tuple[int, bool]] = []

    def _record(pid: int, *, record_create_time=None, trusted: bool = False) -> bool:
        seen.append((pid, trusted))
        return True

    monkeypatch.setattr(serve, "_terminate_tree", _record)
    module._terminate_server_tree(_FakeProc(4242))

    assert seen == [(4242, True)], (
        f"{module.__name__} did not tree-kill its spawned server via "
        "serve._terminate_tree(trusted=True) — a parent-only terminate orphans the tree"
    )
