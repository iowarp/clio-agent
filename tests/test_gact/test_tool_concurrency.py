"""A step's and a script's tool calls share one bound; the cancellation poll stays cheap."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import context as _ctx
from clio_agent.gact.agents import tool_concurrency
from clio_agent.gact.agents.tool_concurrency import run_bounded, server_of
from clio_agent.gact.cancellation_check import make_cancellation_checker


class _Peak:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running: dict[str, int] = {}
        self.peak: dict[str, int] = {}

    def run(self, name: str) -> str:
        server = server_of(name, ("records", "home"))
        with self.lock:
            self.running[server] = self.running.get(server, 0) + 1
            self.peak[server] = max(self.peak.get(server, 0), self.running[server])
        time.sleep(0.01)
        with self.lock:
            self.running[server] -= 1
        return name


def test_calls_to_one_server_run_within_the_limit_and_keep_their_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_TOOLS_PER_SERVER_CONCURRENCY", "3")
    calls = [f"records_get_record_{i}" for i in range(40)] + ["home_list", "read_file"]
    peak = _Peak()
    out = run_bounded(calls, peak.run, lambda c: c, ("records", "home"), thread_name_prefix="t")
    assert out == calls
    assert peak.peak["records"] == 3
    assert peak.peak["home"] == 1 and peak.peak["read_file"] == 1


def test_no_more_threads_than_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_TOOLS_CALL_WORKERS", "4")
    names: set[str] = set()

    def run(call: int) -> int:
        names.add(threading.current_thread().name)
        return call

    assert run_bounded(list(range(50)), run, lambda c: f"x{c}", (), thread_name_prefix="t") == (
        list(range(50))
    )
    assert len(names) <= 4


def test_the_pool_size_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLIO_TOOLS_CALL_WORKERS", raising=False)
    assert tool_concurrency.call_workers() == 32
    monkeypatch.setenv("CLIO_TOOLS_CALL_WORKERS", "0")
    with pytest.raises(ValueError, match="tools.call_workers"):
        tool_concurrency.call_workers()


def test_calls_run_in_a_copy_of_the_callers_context() -> None:
    token = _ctx.set_tool_session_id("sess_a")
    try:
        seen = run_bounded(
            [1, 2], lambda _c: _ctx.active_tool_session_id(), str, (), thread_name_prefix="t"
        )
    finally:
        _ctx._RUNTIME.reset(token)
    assert seen == ["sess_a", "sess_a"]


def _app(**sessions: Any) -> Any:
    def get(_sid: str) -> Any:
        raise AssertionError("the poll must not load the session")

    store = SimpleNamespace(get=get, most_recent=lambda: sessions.get("recent"))
    return SimpleNamespace(
        state=SimpleNamespace(sessions=store, cancel_events={}, cancel_flags=set())
    )


def test_the_cancellation_poll_reads_the_id_without_loading_the_session() -> None:
    app = _app()
    check = make_cancellation_checker(app)
    token = _ctx.set_tool_session_id("sess_a")
    try:
        assert check() is False
        app.state.cancel_flags.add("sess_a")
        assert check() is True
    finally:
        _ctx._RUNTIME.reset(token)


def test_an_out_of_band_call_still_resolves_the_recent_session() -> None:
    app = _app(recent=SimpleNamespace(id="sess_b"))
    app.state.cancel_flags.add("sess_b")
    assert make_cancellation_checker(app)() is True


def test_a_steps_calls_go_through_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact.agents.clio_react import _Loop

    monkeypatch.setenv("CLIO_TOOLS_PER_SERVER_CONCURRENCY", "2")
    peak = _Peak()
    step = SimpleNamespace(
        agent=SimpleNamespace(_clio_tool_servers=frozenset({"records"})),
        _call=lambda call: peak.run(call.name),
    )
    calls = [SimpleNamespace(name=f"records_get_record_{i}") for i in range(12)]
    assert _Loop._execute(step, calls) == [c.name for c in calls]  # type: ignore[arg-type]
    assert peak.peak["records"] == 2
