"""ONE active Codex login flow, ever -- the owner's live-tested OAuth defect.

Two (or more) concurrent flows each bind their own PKCE state, but only ONE
of them can ever hold the fixed loopback port; the browser redirect for
whichever flow the user actually completed can then land on a DIFFERENT
flow's listener, which rejects it as "state mismatch". `start_login` is the
single entry point that makes this impossible: a still-pending flow for the
same method is returned as-is (same flow_id, same state, no second
listener); an explicit retry (`force=True`) or a method change cancels the
old one -- closing its listener -- before a new one starts.
"""

from __future__ import annotations

import httpx
import pytest

from clio_agent.providers.codex import constants as c
from clio_agent.providers.codex import login_flow


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    # An OS-assigned ephemeral port, never the fixed 1455 -- these tests run
    # concurrently with everything else on this machine.
    monkeypatch.setattr(c, "LOOPBACK_PORT", 0)
    login_flow._reset_login_flows_for_tests()  # noqa: SLF001
    yield
    login_flow._reset_login_flows_for_tests()  # noqa: SLF001


def _listener_port(flow: login_flow.CodexLoginFlow) -> int | None:
    listener = flow._loopback  # noqa: SLF001 - test-only introspection
    if listener is None or listener._server is None:  # noqa: SLF001
        return None
    return listener._server.server_address[1]  # noqa: SLF001


class TestStartIsIdempotentWhilePending:
    def test_start_twice_without_force_returns_the_same_flow_and_listener(self) -> None:
        first = login_flow.start_login(method="browser", force=False)
        second = login_flow.start_login(method="browser", force=False)

        assert second.flow_id == first.flow_id
        assert second.browser == first.browser

        flow = login_flow.get_login_flow(first.flow_id)
        assert flow is not None
        port = _listener_port(flow)
        assert port is not None
        # Still bound to the SAME port -- no second listener was created.
        response = httpx.get(f"http://127.0.0.1:{port}/auth/callback?code=x&state=y")
        assert response.status_code in (200, 400)  # reached the ONE listener either way
        flow.cancel()

    def test_status_never_starts_a_flow(self) -> None:
        # No flow exists yet; polling status for a made-up id must not create one.
        assert login_flow.get_login_flow("never-started") is None
        assert login_flow._current is None  # noqa: SLF001


class TestForceCancelsTheOldFlowBeforeStartingAFreshOne:
    def test_force_closes_the_old_listener_and_issues_a_new_state(self) -> None:
        first = login_flow.start_login(method="browser", force=False)
        old_flow = login_flow.get_login_flow(first.flow_id)
        assert old_flow is not None
        old_port = _listener_port(old_flow)
        assert old_port is not None

        second = login_flow.start_login(method="browser", force=True)

        assert second.flow_id != first.flow_id
        # The old flow is cancelled -- its listener closed -- rather than left
        # dangling to contend with the new one for state.
        assert old_flow.status() == ("failed", "cancelled")
        # get_login_flow no longer finds the old id: there is only ONE current flow.
        assert login_flow.get_login_flow(first.flow_id) is None
        new_flow = login_flow.get_login_flow(second.flow_id)
        assert new_flow is not None
        new_flow.cancel()

    def test_old_state_is_rejected_by_the_new_listener(self) -> None:
        """The exact failure mode this fixes: a redirect carrying the OLD
        (cancelled) flow's state must never be accepted as completing the NEW
        one -- it is a typed rejection on the new listener, never silently
        treated as success."""
        first = login_flow.start_login(method="browser", force=False)
        old_flow = login_flow.get_login_flow(first.flow_id)
        assert old_flow is not None
        old_state = old_flow._pkce.state  # noqa: SLF001

        second = login_flow.start_login(method="browser", force=True)
        new_flow = login_flow.get_login_flow(second.flow_id)
        assert new_flow is not None
        new_port = _listener_port(new_flow)
        assert new_port is not None
        new_state = new_flow._pkce.state  # noqa: SLF001
        assert new_state != old_state

        rejected = httpx.get(
            f"http://127.0.0.1:{new_port}/auth/callback?code=abc&state={old_state}"
        )
        assert rejected.status_code == 400
        with pytest.raises(login_flow.OAuthError):
            new_flow._loopback.wait_for_code(5.0)  # noqa: SLF001
        status, reason = new_flow.status()
        assert status == "failed"
        assert "state" in reason

    def test_new_states_own_redirect_is_accepted(self) -> None:
        """The NEW flow's own (correct) state completes it normally -- this
        is the successful-retry path the fix must not break."""
        login_flow.start_login(method="browser", force=False)
        second = login_flow.start_login(method="browser", force=True)
        new_flow = login_flow.get_login_flow(second.flow_id)
        assert new_flow is not None
        new_port = _listener_port(new_flow)
        assert new_port is not None
        new_state = new_flow._pkce.state  # noqa: SLF001

        accepted = httpx.get(
            f"http://127.0.0.1:{new_port}/auth/callback?code=abc&state={new_state}"
        )
        assert accepted.status_code == 200
        code, state = new_flow._loopback.wait_for_code(5.0)  # noqa: SLF001
        assert (code, state) == ("abc", new_state)

    def test_a_method_change_is_treated_like_force(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from clio_agent.providers.codex import oauth as codex_oauth

        first = login_flow.start_login(method="browser", force=False)
        old_flow = login_flow.get_login_flow(first.flow_id)
        assert old_flow is not None

        monkeypatch.setattr(
            codex_oauth,
            "start_device_login",
            lambda: codex_oauth.DeviceLogin(
                device_auth_id="d",
                user_code="ABCD-1234",
                verification_url="https://auth.openai.com/codex/device",
                interval_s=1.0,
            ),
        )
        second = login_flow.start_login(method="device", force=False)

        assert second.flow_id != first.flow_id
        assert old_flow.status() == ("failed", "cancelled")


class TestResolvedOrExpiredFlowsAreNeverReused:
    def test_a_completed_flow_is_not_returned_by_a_later_start(self) -> None:
        first = login_flow.start_login(method="browser", force=False)
        flow = login_flow.get_login_flow(first.flow_id)
        assert flow is not None
        with flow._result.lock:  # noqa: SLF001
            flow._result.status = "complete"  # noqa: SLF001

        second = login_flow.start_login(method="browser", force=False)
        assert second.flow_id != first.flow_id
        login_flow.get_login_flow(second.flow_id).cancel()  # type: ignore[union-attr]

    def test_an_expired_flow_is_not_returned_by_a_later_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = login_flow.start_login(method="browser", force=False)
        monkeypatch.setattr(login_flow, "_expired", lambda _created_at: True)

        second = login_flow.start_login(method="browser", force=False)
        assert second.flow_id != first.flow_id
        login_flow.get_login_flow(second.flow_id).cancel()  # type: ignore[union-attr]
