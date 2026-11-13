"""CLIO's per-provider request queue: tiers, fairness, push-back, allowances, headers."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from clio_agent.search import http, scheduler
from clio_agent.search.http import SourceError
from clio_agent.search.scheduler import SCHEDULER, CallEvents, QueueBusy
from clio_agent.search.settings import build_settings
from clio_agent.search.terms import KEY, KEYLESS, PAID, REGISTERED, Terms, terms_of, tier_of
from clio_agent.tools.servers.web_limits import SEARCH_LIMITS


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    monkeypatch.setattr(scheduler, "RETRY_PAUSE_S", 0.0)
    monkeypatch.setattr(scheduler, "_POLL_S", 0.005)


def _answer(value: Any = "ok", delay: float = 0.0, log: list | None = None):
    async def run() -> Any:
        if log is not None:
            log.append(value)
        await asyncio.sleep(delay)
        return value

    return run


def test_tiers_follow_key_paid_mark_and_contact_email() -> None:
    plain = build_settings()
    assert tier_of("openalex", plain, has_key=False) == KEYLESS
    assert tier_of("openalex", plain, has_key=True) == KEY
    assert tier_of("openalex", build_settings(paid=["openalex"]), has_key=True) == PAID
    assert tier_of("openalex", build_settings(paid=["openalex"]), has_key=False) == KEYLESS
    assert tier_of("crossref", build_settings(contact_email="a@b.org"), has_key=False) == (
        REGISTERED
    )
    assert terms_of("crossref", plain, has_key=False).concurrency == 1
    assert terms_of("arxiv", plain, has_key=False).interval_s == pytest.approx(3.0)
    assert terms_of("openalex", plain, has_key=False).usd_per_day == pytest.approx(0.10)
    assert terms_of("you", plain, has_key=False).per_day == 100


def test_requests_from_all_sessions_share_the_concurrency_cap() -> None:
    running = 0
    most = 0

    async def run() -> str:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.02)
        running -= 1
        return "ok"

    terms = Terms(KEYLESS, concurrency=2)

    async def main() -> list[str]:
        return await asyncio.gather(
            *(
                SCHEDULER.call("exa", run, terms=terms, caller=f"sess_{i % 3}", patience_s=5)
                for i in range(8)
            )
        )

    assert asyncio.run(main()) == ["ok"] * 8
    assert most == 2


def test_sessions_take_turns_so_one_busy_session_does_not_starve_another() -> None:
    order: list[str] = []
    terms = Terms(KEYLESS, concurrency=1)

    async def main() -> None:
        busy = [
            SCHEDULER.call(
                "crossref", _answer(f"a{i}", 0.01, order), terms=terms, caller="a", patience_s=5
            )
            for i in range(4)
        ]
        tasks = [asyncio.create_task(c) for c in busy]
        await asyncio.sleep(0.001)
        other = asyncio.create_task(
            SCHEDULER.call("crossref", _answer("b0", 0.01, order), terms=terms, caller="b")
        )
        await asyncio.gather(*tasks, other)

    asyncio.run(main())
    assert order.index("b0") <= 2  # its turn comes after at most one more of a's


def test_too_many_requests_pauses_the_provider_and_requeues_the_request() -> None:
    calls = 0

    async def run() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SourceError("openalex", http.RATE_LIMITED, "HTTP 429", 0.05)
        return "ok"

    events = CallEvents()
    terms = Terms(KEYLESS, concurrency=2)
    result = asyncio.run(
        SCHEDULER.call("openalex", run, terms=terms, caller="s", patience_s=5, events=events)
    )
    assert result == "ok" and calls == 2 and events.requeued == 1
    assert SEARCH_LIMITS.status()["openalex"]["failed_today"] == 1
    assert SCHEDULER._lanes["openalex"].concurrency() == 1  # pushed back: one at a time


def test_a_long_pause_leaves_the_queue_with_its_kind() -> None:
    SEARCH_LIMITS.cool("you", 3600, http.ALLOWANCE_USED, "HTTP 429")
    log: list[str] = []
    with pytest.raises(SourceError) as failed:
        asyncio.run(SCHEDULER.call("you", _answer(log=log), terms=Terms(KEYLESS), patience_s=1))
    assert failed.value.kind == http.ALLOWANCE_USED and log == []


def test_one_outright_failure_is_retried_then_the_provider_rests() -> None:
    glitch = SourceError("keenable", http.FAILED, "HTTP 404")
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise glitch
        return "ok"

    events = CallEvents()
    assert (
        asyncio.run(SCHEDULER.call("keenable", flaky, terms=Terms(KEYLESS), events=events)) == "ok"
    )
    assert events.retried == ["HTTP 404"] and SEARCH_LIMITS.cooldown("keenable") is None

    async def down() -> str:
        raise glitch

    with pytest.raises(SourceError):
        asyncio.run(SCHEDULER.call("keenable", down, terms=Terms(KEYLESS)))
    assert SEARCH_LIMITS.cooldown("keenable").kind == http.FAILED


def test_the_free_allowance_stops_before_the_provider_refuses() -> None:
    terms = Terms(KEYLESS, per_day=2)
    log: list[str] = []
    for _ in range(2):
        asyncio.run(SCHEDULER.call("you", _answer(log=log), terms=terms))
    with pytest.raises(SourceError) as failed:
        asyncio.run(SCHEDULER.call("you", _answer(log=log), terms=terms))
    assert failed.value.kind == http.ALLOWANCE_USED and len(log) == 2
    assert SEARCH_LIMITS.cooldown("you").kind == http.ALLOWANCE_USED
    assert SCHEDULER.view("you")["used"] == {
        "spent": 2,
        "limit": 2.0,
        "unit": "requests",
        "per": "day",
    }


def test_openalex_dollars_are_counted_and_the_provider_remaining_is_believed() -> None:
    terms = Terms(KEYLESS, usd_per_day=0.10)
    asyncio.run(SCHEDULER.call("openalex", _answer(), terms=terms, cost_usd=0.001))
    asyncio.run(SCHEDULER.call("openalex", _answer(), terms=terms, cost_usd=0.0))
    assert SCHEDULER.view("openalex")["used"]["spent"] == pytest.approx(0.001)
    SCHEDULER.learn("openalex", {"X-RateLimit-Remaining-USD": "0.0003"})
    with pytest.raises(SourceError) as failed:
        asyncio.run(SCHEDULER.call("openalex", _answer(), terms=terms, cost_usd=0.001))
    assert failed.value.kind == http.ALLOWANCE_USED
    # a free single-work lookup still runs while the search budget is spent
    SEARCH_LIMITS.reset()
    assert asyncio.run(SCHEDULER.call("openalex", _answer("w"), terms=terms)) == "w"


def test_crossref_headers_set_the_pace() -> None:
    asyncio.run(SCHEDULER.call("crossref", _answer(), terms=Terms(REGISTERED, concurrency=3)))
    SCHEDULER.learn(
        "crossref",
        {"x-rate-limit-limit": "5", "x-rate-limit-interval": "1s", "x-concurrency-limit": "1"},
    )
    lane = SCHEDULER._lanes["crossref"]
    assert lane.interval() == pytest.approx(0.2) and lane.concurrency() == 1


def test_a_request_behind_a_long_line_reports_busy_without_resting_the_provider() -> None:
    terms = Terms(KEYLESS, concurrency=1)

    async def main() -> None:
        slow = asyncio.create_task(
            SCHEDULER.call("exa", _answer(delay=0.3), terms=terms, caller="a", patience_s=5)
        )
        await asyncio.sleep(0.01)
        with pytest.raises(QueueBusy):
            await SCHEDULER.call("exa", _answer(), terms=terms, caller="b", patience_s=0.05)
        await slow

    asyncio.run(main())
    assert SEARCH_LIMITS.cooldown("exa") is None
    assert SCHEDULER.view("exa")["waiting"] == 0


def test_failures_during_one_outage_are_one_strike() -> None:
    SEARCH_LIMITS.cool("keenable", 30, http.FAILED, "HTTP 502")
    for _ in range(5):  # parallel callers hit by the same outage
        SEARCH_LIMITS.cool("keenable", 30, http.FAILED, "HTTP 502")
    assert SEARCH_LIMITS.failures_in_row("keenable") == 1
    SEARCH_LIMITS.answered("keenable")
    assert SEARCH_LIMITS.failures_in_row("keenable") == 0


def test_a_refusal_while_the_provider_reports_budget_left_is_a_rate_limit() -> None:
    terms = Terms(KEYLESS, usd_per_day=0.10)
    asyncio.run(SCHEDULER.call("openalex", _answer(), terms=terms))
    SCHEDULER.learn("openalex", {"x-ratelimit-remaining-usd": "0.03"})
    calls = 0

    async def refuse_once() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SourceError("openalex", http.ALLOWANCE_USED, "HTTP 429", 0.02)
        return "ok"

    result = asyncio.run(
        SCHEDULER.call("openalex", refuse_once, terms=terms, cost_usd=0.001, patience_s=5)
    )
    assert result == "ok" and calls == 2


def test_a_low_budget_report_rests_the_provider_minutes_not_a_day(monkeypatch) -> None:
    terms = Terms(KEYLESS, usd_per_day=0.10)
    asyncio.run(SCHEDULER.call("openalex", _answer(), terms=terms))
    SCHEDULER.learn("openalex", {"x-ratelimit-remaining-usd": "0.0002"})
    with pytest.raises(SourceError):
        asyncio.run(SCHEDULER.call("openalex", _answer(), terms=terms, cost_usd=0.001))
    resting = SEARCH_LIMITS.cooldown("openalex")
    assert resting.until - __import__("time").time() <= scheduler.PROVIDER_TRUST_S + 1
    # an old report is no longer believed
    SEARCH_LIMITS.reset()
    lane = SCHEDULER._lanes["openalex"]
    lane.remaining_at -= scheduler.PROVIDER_TRUST_S + 1
    assert (
        asyncio.run(SCHEDULER.call("openalex", _answer("again"), terms=terms, cost_usd=0.001))
        == "again"
    )


def test_reset_times_are_read_as_seconds_epochs_or_dates() -> None:
    now = 1_800_000_000.0
    assert scheduler._reset_at("60", now) == now + 60
    assert scheduler._reset_at("1800000300", now) == 1_800_000_300
    stamp = scheduler._reset_at("2026-11-13T02:46:20.338Z", now)
    assert abs(stamp - 1794537980.338) < 1
    assert scheduler._reset_at("soon", now) == 0.0
