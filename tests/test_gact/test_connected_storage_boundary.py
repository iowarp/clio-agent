"""Source permission changes cannot outlive previously compiled child policies."""

import threading
from types import SimpleNamespace

import pytest

from clio_agent.gact.storage.boundary import source_policy_change


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
