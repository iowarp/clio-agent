"""``GET /v1/sessions/{sid}/work`` reads its messages and schedules off the server loop.

The first read after a restart rehydrates the session's resident ledger from the store,
and the store refuses a wait from the loop thread (``LoopThreadStoreWrite``): the route
answered 500 to the first UI load after every restart (fix ledger FX-F7).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from clio_agent.arc import loop_guard
from clio_agent.gact.app import build_app


class _Rows:
    """Stands in for the resident ledger: records where each read ran."""

    def __init__(self, inner: Any, seen: list[bool]) -> None:
        self.inner, self.seen = inner, seen

    def get(self, sid: str, default: Any = None) -> Any:
        self.seen.append(loop_guard.on_server_loop())
        return self.inner.get(sid, default)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def test_the_work_route_reads_the_ledger_off_the_server_loop(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    seen: list[bool] = []
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "work"}).json()["id"]
        app.state.messages = _Rows(app.state.messages, seen)
        answer = client.get(f"/v1/sessions/{sid}/work")
    assert answer.status_code == 200
    assert seen and not any(seen)  # never on the loop thread
    assert {"todos", "schedules", "plan"} <= set(answer.json())
