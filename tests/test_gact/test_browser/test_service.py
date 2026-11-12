"""The browser service: profiles, pages owned by sessions, limits, the clock and cleanup."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import psutil
import pytest

from clio_agent.gact.browser.settings import BrowserError

pytestmark = pytest.mark.timeout(120)  # each test launches a real Chromium


def _chromes() -> list[psutil.Process]:
    """Chromium processes this test process started (its descendants)."""
    me = psutil.Process()
    return [p for p in me.children(recursive=True) if "chrom" in (p.name() or "").lower()]


def _goto(browser: Any, record: Any, url: str) -> None:
    browser.run(record, "goto", lambda r: r.page.goto(url))


def test_pages_belong_to_the_session_that_opened_them(browser: Any, site: dict[str, Any]) -> None:
    mine = browser.wait(browser.open("sess_a", "Survey"))
    browser.wait(browser.open("sess_b", "Other"))
    _goto(browser, mine, site["a"] + "/table")
    assert mine.host() == "a.fixture.localhost"
    assert [r.id for r in browser.session_pages("sess_a")] == [mine.id]
    assert browser.owned("sess_a", mine.id) is mine
    with pytest.raises(BrowserError) as refused:
        browser.owned("sess_b", mine.id)
    assert refused.value.reason == "browser_page_not_found"
    state = browser.state(mine)
    assert state["label"] == "Survey" and state["url"].endswith("/table")
    assert state["sandbox_off"] is (os.geteuid() == 0)


def test_the_page_limit_refuses_one_more(browser: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_BROWSER_MAX_PAGES", "1")
    browser.wait(browser.open("sess_a", ""))
    with pytest.raises(BrowserError) as refused:
        browser.wait(browser.open("sess_a", ""))
    assert refused.value.reason == "browser_page_limit"
    assert "1 in this conversation" in refused.value.message
    with pytest.raises(BrowserError) as elsewhere:
        browser.wait(browser.open("sess_b", ""))
    assert "0 in this conversation and 1 in 1 other conversations" in elsewhere.value.message


def test_pages_nobody_uses_close_but_watched_ones_stay(
    browser: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    unused = browser.wait(browser.open("sess_a", ""))
    watched = browser.wait(browser.open("sess_b", ""))
    busy = browser.wait(browser.open("sess_c", ""))
    watched.viewers = 1
    for record in (unused, watched):
        record.last_used -= 3600
    monkeypatch.setenv("CLIO_BROWSER_IDLE_MINUTES", "30")
    browser.reap_idle()
    assert unused.closed and not watched.closed and not busy.closed


def test_an_action_past_its_clock_is_stopped(browser: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    record = browser.wait(browser.open("sess_a", ""))
    monkeypatch.setenv("CLIO_BROWSER_ACTION_TIMEOUT_S", "0.5")

    async def slow(_record: Any) -> None:
        await asyncio.sleep(5)

    with pytest.raises(BrowserError) as stopped:
        browser.run(record, "wait", slow)
    assert stopped.value.reason == "browser_action_timeout"
    assert record.inflight is None or record.inflight.action == "wait"


def test_time_waiting_for_the_person_is_not_counted(
    browser: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = browser.wait(browser.open("sess_a", ""))
    monkeypatch.setenv("CLIO_BROWSER_ACTION_TIMEOUT_S", "0.5")

    async def asks(rec: Any) -> str:
        rec.inflight.waiting = True
        await asyncio.sleep(1.0)  # longer than the clock, but the person is answering
        rec.inflight.paused_s += 1.0
        rec.inflight.waiting = False
        return "answered"

    assert browser.run(record, "click", asks) == "answered"


def test_an_action_has_no_clock_by_default(browser: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default 0: a slow action finishes; only the person (or an operator limit) stops it."""
    from clio_agent.gact.browser import settings

    monkeypatch.delenv("CLIO_BROWSER_ACTION_TIMEOUT_S", raising=False)
    assert settings.action_timeout_s() is None
    record = browser.wait(browser.open("sess_a", ""))

    async def slow(_record: Any) -> str:
        await asyncio.sleep(1.0)
        return "finished"

    assert browser.run(record, "wait", slow) == "finished"


def test_strictness_is_a_typed_reason(browser: Any, site: dict[str, Any]) -> None:
    record = browser.wait(browser.open("sess_a", ""))
    _goto(browser, record, site["a"] + "/table")
    with pytest.raises(BrowserError) as ambiguous:
        browser.run(record, "click", lambda r: r.page.get_by_role("row").click())
    assert ambiguous.value.reason == "browser_target_ambiguous"
    assert "11 elements" in ambiguous.value.message


def test_a_download_lands_in_session_scratch_and_is_reported_once(
    browser: Any, site: dict[str, Any], tmp_path: Path
) -> None:
    record = browser.wait(browser.open("sess_dl", ""))
    _goto(browser, record, site["a"] + "/download")
    browser.run(record, "click", lambda r: r.page.get_by_role("link").click())
    notes = browser.take_notes(record)
    assert [d["name"] for d in notes["downloads"]] == ["report.csv"]
    saved = Path(notes["downloads"][0]["path"])
    assert saved.parent == tmp_path / "scratch" / "sess_dl" / "downloads"
    assert saved.read_text().startswith("name,format,year")
    assert "downloads" not in browser.take_notes(record)


def test_a_popup_belongs_to_its_openers_session(browser: Any, site: dict[str, Any]) -> None:
    record = browser.wait(browser.open("sess_pop", "Main"))
    _goto(browser, record, site["a"] + "/")
    browser.run(record, "evaluate", lambda r: r.page.evaluate(f"window.open('{site['a']}/table')"))
    for _ in range(50):
        if len(browser.session_pages("sess_pop")) == 2:
            break
        browser.wait(asyncio.sleep(0.1))
    labels = sorted(r.label for r in browser.session_pages("sess_pop"))
    assert labels == ["Main", "Main (popup)"]


def test_sign_ins_survive_a_restart_of_the_profile(
    browser: Any, site: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = browser.wait(browser.open("sess_a", ""))
    _goto(browser, record, site["a"] + "/signin")

    async def sign_in(r: Any) -> None:
        await r.page.fill("input[name=username]", "ada")
        await r.page.fill("input[type=password]", "correct horse")
        await r.page.click("button")
        await r.page.wait_for_url("**/account")

    browser.run(record, "sign in", sign_in)
    browser.close_all()
    from clio_agent.gact.browser import service

    again = service.BrowserService()
    try:
        page = again.wait(again.open("sess_new", ""))
        again.run(page, "goto", lambda r: r.page.goto(site["a"] + "/account"))
        assert page.url().endswith("/account")
    finally:
        again.close_all()


def test_closing_a_session_closes_only_its_pages_and_shutdown_stops_chromium(
    browser: Any,
) -> None:
    browser.wait(browser.open("sess_a", ""))
    keep = browser.wait(browser.open("sess_b", ""))
    assert browser.close_session("sess_a") == 1
    assert [r.id for r in browser.pages.values()] == [keep.id]
    assert _chromes()
    browser.close_all()
    psutil.wait_procs(_chromes(), timeout=10)
    assert _chromes() == []


@pytest.mark.skipif(os.geteuid() != 0, reason="Chromium's sandbox only fails this way as root")
def test_an_unstartable_sandbox_is_refused_unless_allowed(
    browser: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLIO_BROWSER_ALLOW_UNSANDBOXED", "0")
    with pytest.raises(BrowserError) as refused:
        browser.wait(browser.open("sess_a", ""))
    assert refused.value.reason == "browser_sandbox_unavailable"
    assert "sandbox" in refused.value.message.lower()
    from clio_agent.gact.browser.install import probe_browser

    row = probe_browser()
    assert row.details["sandbox"]["sandboxed"] is False


def test_switching_the_browser_closes_its_pages_and_the_next_page_opens_again(
    browser: Any,
) -> None:
    first = browser.wait(browser.open("sess_a", ""))
    assert browser.switch_browser() == 1
    assert first.closed and not browser.profiles
    again = browser.wait(browser.open("sess_a", ""))
    assert not again.closed and browser.profiles


def test_a_gate_error_block_grants_no_host() -> None:
    """A page blocked by a gate error names no host; allowing it grants nothing.

    SABOTAGE: drop the empty-host check in ``allow_blocked`` -> a grant on host "" -> red."""
    from types import SimpleNamespace

    from clio_agent.gact.browser import host_gate

    granted: list[str] = []
    record = SimpleNamespace(
        navigation_blocked={"host": "", "url": "https://x/", "reason": "browser_host_gate_error"},
        session_id="sess_x",
    )
    original = host_gate.grant
    host_gate.grant = lambda app, sid, host, grantor: granted.append(host)  # type: ignore[assignment]
    try:
        assert host_gate.allow_blocked(SimpleNamespace(app=None), record) == ""  # type: ignore[arg-type]
    finally:
        host_gate.grant = original  # type: ignore[assignment]
    assert granted == []
    assert record.navigation_blocked is not None
