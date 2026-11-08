"""Source permission changes cannot outlive previously compiled child policies."""

import threading
from types import SimpleNamespace

import pytest

from clio_agent.agent import ClioAgent
from clio_agent.gact.storage.boundary import source_policy_change
from clio_agent.tools.reaper import WorkspaceExecutorReaper, residency_lease
from clio_agent.tools.workspace_root import canonical_workspace_root


def test_live_turn_prevents_policy_change() -> None:
    state = (threading.Lock(), {}, {"/workspace": 1})
    agent = SimpleNamespace(_workspace_state=lambda: state)
    with pytest.raises(ValueError, match="active CLIO turns"):
        with source_policy_change(agent):
            pytest.fail("A live turn must not receive a new policy")


def test_policy_change_recycles_idle_fleets_and_releases_after_failure() -> None:
    state = (threading.Lock(), {"/workspace": SimpleNamespace(busy=False)}, {})
    calls = []

    def restart(root: str) -> str:
        assert agent._source_policy_changing
        calls.append(root)
        return "restarted_live"

    agent = SimpleNamespace(_workspace_state=lambda: state, request_fleet_restart=restart)
    with pytest.raises(ValueError, match="rejected configuration"):
        with source_policy_change(agent):
            assert calls == ["/workspace"]
            with pytest.raises(ValueError, match="active CLIO"):
                with source_policy_change(agent):
                    pytest.fail("Concurrent policy changes must serialize")
            raise ValueError("rejected configuration")
    assert not agent._source_policy_changing


def test_restart_failure_never_reports_new_access() -> None:
    state = (threading.Lock(), {"/workspace": SimpleNamespace(busy=False)}, {})
    agent = SimpleNamespace(
        _workspace_state=lambda: state, request_fleet_restart=lambda root: "restart_deferred_busy"
    )
    with pytest.raises(RuntimeError, match="safely refresh"):
        with source_policy_change(agent):
            pytest.fail("Unconfirmed teardown must not allow a permission change")
    assert not agent._source_policy_changing


def _agent_with_fleet(root: str) -> tuple[ClioAgent, SimpleNamespace]:
    agent = object.__new__(ClioAgent)
    lock, executors, leases = agent._workspace_state()
    executor = SimpleNamespace(busy=False, closed=False, idle_for=lambda: 0.0)
    executor.close = lambda: setattr(executor, "closed", True)
    executors[canonical_workspace_root(root)] = executor
    agent._workspace_reaper = WorkspaceExecutorReaper(
        executors, lock, leases=leases, tick_s=3600, channel_closer=lambda _root: 0
    )
    return agent, executor


def test_conversation_on_screen_does_not_block_policy_change(tmp_path) -> None:
    agent, executor = _agent_with_fleet(str(tmp_path))
    with residency_lease(agent, str(tmp_path)):
        with source_policy_change(agent):
            assert executor.closed, "the idle fleet restarts with the new permissions"
        lock, executors, leases = agent._workspace_state()
        assert sum(leases.values()) == 1, "the view keeps holding the root"
    assert not agent._workspace_state()[2]
    assert not agent._workspace_reaper.residency


def test_live_turn_beside_a_view_still_blocks_policy_change(tmp_path) -> None:
    agent, executor = _agent_with_fleet(str(tmp_path))
    with residency_lease(agent, str(tmp_path)), agent.lease_workspace_fleet(str(tmp_path)):
        with pytest.raises(ValueError, match="active CLIO turns"):
            with source_policy_change(agent):
                pytest.fail("A live turn must not receive a new policy")
    assert not executor.closed
