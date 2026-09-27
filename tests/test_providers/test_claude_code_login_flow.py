"""#1454: CLIO drives the Claude Code CLI's own ``claude auth login``.

The fake CLI below replays what Claude Code 2.1.281 prints (verified live): an
OSC 8 hyperlink around the authorize URL, then ``Paste code here if prompted >``
on stdin; a good code exits 0, a bad one prints ``Login failed: ...`` and
exits 1. CLIO relays; it never touches Claude's credential files.
"""

from __future__ import annotations

import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from clio_agent.providers import claude_code_login_flow as login

_URL = (
    "https://claude.com/cai/oauth/authorize?code=true&client_id=abc&response_type=code"
    "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&state=xyz"
)

_FAKE_CLI = textwrap.dedent(
    f"""
    import sys
    assert sys.argv[1:] == ["auth", "login", "--claudeai"], sys.argv
    url = {_URL!r}
    sys.stdout.write("Opening browser to sign in\\u2026\\n")
    sys.stdout.write("If the browser didn't open, visit: \\x1b]8;;" + url + "\\x07" + url + "\\x1b]8;;\\x07\\n")
    sys.stdout.write("Paste code here if prompted > ")
    sys.stdout.flush()
    code = sys.stdin.readline().strip()
    if code == "good-code":
        sys.stdout.write("Login successful.\\n")
        sys.exit(0)
    sys.stdout.write("Login failed: Request failed with status code 400\\n")
    sys.exit(1)
    """
)


@pytest.fixture
def fake_cli(tmp_path: Path) -> str:
    script = tmp_path / "fake_claude.py"
    script.write_text(_FAKE_CLI, encoding="utf-8")
    if os.name == "nt":
        wrapper = tmp_path / "claude.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        wrapper = tmp_path / "claude"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


def test_start_returns_the_cli_authorize_url(fake_cli: str) -> None:
    flow = login.start_login(fake_cli, wait_s=30)
    try:
        assert flow.url == _URL
        assert login.get_flow(flow.flow_id) is flow
        assert flow.state() == ("pending", "")
    finally:
        login.drop_flow(flow.flow_id)


def test_a_good_code_is_handed_to_the_cli_and_it_exits_signed_in(fake_cli: str) -> None:
    flow = login.start_login(fake_cli, wait_s=30)
    flow.submit_code("  good-code \n")
    assert flow.exited.wait(30)

    assert flow.state() == ("exited_ok", "")
    login.drop_flow(flow.flow_id)
    assert login.get_flow(flow.flow_id) is None


def test_a_bad_code_reports_the_cli_own_reason(fake_cli: str) -> None:
    flow = login.start_login(fake_cli, wait_s=30)
    flow.submit_code("bad")
    assert flow.exited.wait(30)

    assert flow.state() == ("failed", "Login failed: Request failed with status code 400")
    login.drop_flow(flow.flow_id)


def test_starting_again_cancels_the_earlier_attempt(fake_cli: str) -> None:
    first = login.start_login(fake_cli, wait_s=30)
    second = login.start_login(fake_cli, wait_s=30)
    try:
        assert first.exited.wait(30)
        assert login.get_flow(first.flow_id) is None
        assert login.get_flow(second.flow_id) is second
    finally:
        login.drop_flow(second.flow_id)


def test_a_cli_that_cannot_start_is_a_typed_error(tmp_path: Path) -> None:
    with pytest.raises(login.ClaudeLoginError, match="could not start its sign-in"):
        login.start_login(str(tmp_path / "missing-claude"), wait_s=5)


def test_the_generic_auth_route_drives_claude_code_sign_in(
    fake_cli: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, floor_sandbox: object
) -> None:
    """The picker's "Log in" for Claude Code used to 405 (no start handler)."""

    from fastapi.testclient import TestClient

    from clio_agent.gact import claude_code_auth_reprobe
    from clio_agent.gact.app import build_app
    from clio_agent.providers.model_discovery import claude_code as cc_discovery

    monkeypatch.setattr(cc_discovery, "_resolve_claude_binary", lambda: fake_cli)
    reverified: list[str] = []

    async def _reprobe(app: object, provider: object, *, trigger: str) -> bool:
        reverified.append(trigger)
        return True

    monkeypatch.setattr(claude_code_auth_reprobe, "reprobe_claude_code_auth", _reprobe)
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        start = client.post("/v1/providers/claude_code/auth", json={"action": "start"})
        assert start.status_code == 200, start.text
        body = start.json()
        assert body["browser"] == {"authorization_url": _URL, "loopback": False}
        bad = client.post(
            "/v1/providers/claude_code/auth",
            json={"action": "complete", "flow_id": body["flow_id"], "paste": "bad"},
        )
        again = client.post("/v1/providers/claude_code/auth", json={"action": "start"}).json()
        good = client.post(
            "/v1/providers/claude_code/auth",
            json={"action": "complete", "flow_id": again["flow_id"], "paste": "good-code"},
        )

    assert bad.status_code == 401
    assert "Login failed: Request failed with status code 400" in bad.text
    assert good.status_code == 200, good.text
    assert good.json()["is_authenticated"] is True
    assert reverified == ["sign_in"]
