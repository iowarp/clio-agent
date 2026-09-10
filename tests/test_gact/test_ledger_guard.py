"""The in-memory list ledgers are guarded now that finalize runs off the loop (#1334).

``app.state.pending_diffs`` / ``context_frames`` are plain lists appended by turn
finalize (turn executor) and scanned / popped by routes (loop thread). Every touch must
hold ``runtime.retention.ledger_guard``: the bound enforcement itself, the finalize
diff indexing, and the diff / frame routes. These lock the seams so a site that drops
the guard turns red.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from clio_agent.gact import diff_ledger
from clio_agent.gact.app import build_app
from clio_agent.gact.routes import diffs as diffs_routes
from clio_agent.gact.runtime import retention
from clio_agent.gact.runtime.retention import enforce_list_bound, ledger_guard


def _app() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(pending_diffs={}, context_frames={}))


def test_ledger_guard_is_one_reentrant_lock_per_app() -> None:
    app = _app()
    guard = ledger_guard(app)
    assert guard is ledger_guard(app)
    assert ledger_guard(_app()) is not guard
    with guard:
        with guard:  # re-entrant: a caller holding it may call enforce_list_bound
            pass


def test_enforce_list_bound_holds_the_guard() -> None:
    """A thread holding the guard blocks the bound enforcement until it releases."""

    app = _app()
    ledger = [{"status": "applied"} for _ in range(3)]
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()

    def _holder() -> None:
        with ledger_guard(app):
            entered.set()
            release.wait(timeout=5)

    def _enforcer() -> None:
        enforce_list_bound(app, ledger, "pending_diffs", session_id="s")
        done.set()

    threading.Thread(target=_holder, daemon=True).start()
    assert entered.wait(timeout=5)
    threading.Thread(target=_enforcer, daemon=True).start()
    assert not done.wait(timeout=0.3), "enforce_list_bound ran without the guard"
    release.set()
    assert done.wait(timeout=5)


def test_index_turn_file_diffs_appends_under_the_guard(monkeypatch: Any) -> None:
    app = _app()
    held: list[bool] = []
    real = ledger_guard(app)

    class _Recorder:
        def __enter__(self) -> None:
            real.acquire()
            held.append(True)

        def __exit__(self, *exc: object) -> None:
            real.release()

    monkeypatch.setattr(diff_ledger, "ledger_guard", lambda _app: _Recorder())
    part = SimpleNamespace(
        type="file_diff",
        path="a.py",
        unified_diff="--- a\n+++ b\n",
        new_content="x",
        edit_mode="whole",
        id="p1",
    )
    other = SimpleNamespace(type="text", text="hi")
    n = diff_ledger.index_turn_file_diffs(app, "s", [part, other], message_id="m1")
    assert n == 1 and held == [True]
    rows = diff_ledger.pending_diff_rows(app, "s")
    assert rows == [
        {
            "path": "a.py",
            "unified_diff": "--- a\n+++ b\n",
            "new_content": "x",
            "status": "pending",
            "part_id": "p1",
            "message_id": "m1",
        }
    ]
    assert rows is not app.state.pending_diffs["s"]  # a snapshot, not the live list


def test_diff_routes_take_the_guard(tmp_path: Path, monkeypatch: Any) -> None:
    """GET / apply / reject on the diff ledger enter the guard (seam lock)."""

    entries: list[str] = []
    orig = retention.ledger_guard

    def _recording(app: Any) -> Any:
        entries.append("guard")
        return orig(app)

    monkeypatch.setattr(diffs_routes, "ledger_guard", _recording)
    monkeypatch.setattr(diff_ledger, "ledger_guard", _recording)
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        sid = client.post("/v1/sessions", json={"title": "diffs"}).json()["id"]
        client.app.state.pending_diffs[sid] = [
            {
                "path": "a.py",
                "unified_diff": "",
                "new_content": None,
                "status": "pending",
                "part_id": "p",
                "message_id": "m",
            }
        ]
        assert client.get(f"/v1/sessions/{sid}/diffs").status_code == 200
        n_get = len(entries)
        assert n_get >= 1
        assert (
            client.post(f"/v1/sessions/{sid}/diffs/reject", json={"paths": ["a.py"]}).status_code
            == 200
        )
        assert len(entries) > n_get
        n_reject = len(entries)
        assert client.post(f"/v1/sessions/{sid}/diffs/apply", json={"paths": []}).status_code == 200
        assert len(entries) > n_reject
