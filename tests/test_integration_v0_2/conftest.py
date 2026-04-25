"""Shared fixtures + helpers for v0.2 integration tests.

Skips the entire suite when CLIO_INTEGRATION_BASE isn't set or
the backend isn't healthy. Each test gets a fresh session so
parallel runs don't trample each other.
"""

from __future__ import annotations

import os
import time
import urllib.request
from collections.abc import Iterator
from typing import Any

import httpx
import pytest


def _backend() -> str:
    base = os.environ.get("CLIO_INTEGRATION_BASE", "").rstrip("/")
    return base


def _backend_alive(base: str) -> bool:
    if not base:
        return False
    try:
        with urllib.request.urlopen(f"{base}/v1/health", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _backend_alive(_backend()),
    reason=(
        "CLIO_INTEGRATION_BASE not set or backend not reachable. "
        "Boot clio-agent-gact (with LM configured) and "
        "export CLIO_INTEGRATION_BASE=http://127.0.0.1:<port>"
    ),
)


@pytest.fixture()
def base() -> str:
    return _backend()


@pytest.fixture()
def http(base: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=base, timeout=30.0) as c:
        yield c


@pytest.fixture()
def session_id(http: httpx.Client) -> str:
    """Fresh session per test, scoped to ws_default."""

    body = http.post("/v1/sessions", json={"title": "integration"}).json()
    return body["id"]


def wait_for_assistant(
    http: httpx.Client,
    sid: str,
    user_id: str,
    *,
    timeout: float = 180.0,
    poll_interval: float = 0.5,
) -> dict[str, Any]:
    """Poll GET /v1/sessions/{sid}/messages until the assistant
    message paired with ``user_id`` lands. Real LM turns can take
    a long time (Haiku ~10s for chat, several minutes for tool
    loops); default 180s timeout covers both.

    Returns the assistant message dict on success; pytest.fail on
    timeout so the test stops fast instead of polluting the run.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msgs = http.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        for i, m in enumerate(msgs):
            if m.get("id") == user_id:
                if i > 0 and msgs[i - 1].get("role") == "assistant":
                    return msgs[i - 1]
                break
        time.sleep(poll_interval)
    pytest.fail(
        f"assistant turn paired with {user_id!r} did not settle "
        f"within {timeout:g}s on session {sid}"
    )


def post_user(http: httpx.Client, sid: str, text: str) -> str:
    """POST a user message; return the user message_id."""

    ack = http.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": text}]},
    )
    assert ack.status_code == 200, ack.text
    return ack.json()["message_id"]


def turn(
    http: httpx.Client, sid: str, text: str, *, timeout: float = 180.0
) -> dict[str, Any]:
    """POST + wait for assistant + return the assistant dict."""

    user_id = post_user(http, sid, text)
    return wait_for_assistant(http, sid, user_id, timeout=timeout)
