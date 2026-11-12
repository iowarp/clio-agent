"""Recorded-response fakes for the search tests (no network)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(api: str, case: str) -> dict[str, Any]:
    """One recorded response (``tests/test_search/fixtures/<api>/<case>.json``)."""

    return json.loads((FIXTURES / api / f"{case}.json").read_text(encoding="utf-8"))


def response_of(recorded: dict[str, Any]) -> httpx.Response:
    """An ``httpx.Response`` replaying a recorded fixture."""

    body = recorded["body"]
    headers = dict(recorded.get("headers") or {})
    if isinstance(body, str):
        return httpx.Response(recorded["status"], text=body, headers=headers)
    return httpx.Response(recorded["status"], json=body, headers=headers)


def install(
    monkeypatch: Any, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    """Route every search HTTP call through ``handler``; return the seen requests."""

    from clio_agent.search import http

    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record), follow_redirects=True)
    http.reset_for_tests()
    monkeypatch.setattr(http, "client", lambda: client)
    return seen
