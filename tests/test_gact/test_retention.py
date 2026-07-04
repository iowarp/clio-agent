"""#770 Wave-C C3: retention bounds on the in-memory GACT ledgers.

Seven in-memory ledgers (command_audit, memory_tool_audit, context_frames,
pending_diffs, permissions, turn_attempts, shared_tokens) grew unbounded for
the life of the process. These tests prove each is now bounded and that every
eviction emits a *structured* reason (no silent drop), mirroring the
stream_fallback reason-catalog contract.

Failing-first: before the fix the ledgers grow to N (unbounded) and the
retention helper module does not exist, so importing it raises ImportError.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = "data_expert"
    routing_rationale: str = "keyword match"


class _FakeAgent:
    def forward(self, question: str, session_id: str):  # noqa: D401
        return _Pred()


@pytest.fixture()
def app_client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=_FakeAgent()))


# --------------------------------------------------------------------------- #
# Unit: the retention helpers (list + dict bounds, structured eviction reason)
# --------------------------------------------------------------------------- #


def _fresh_app(tmp_path: Path):
    return build_app(sessions_path=tmp_path / "s.json", agent=_FakeAgent())


def test_list_bound_fifo_evicts_oldest_with_reason(tmp_path: Path) -> None:
    from clio_agent.gact.runtime import retention

    app = _fresh_app(tmp_path)
    ledger: list[dict] = []
    retention.LEDGER_BOUNDS["_probe_fifo"] = retention.LedgerBound(max_entries=3)
    try:
        for i in range(10):
            ledger.append({"id": i})
            retention.enforce_list_bound(app, ledger, "_probe_fifo")
        assert len(ledger) == 3
        # Oldest evicted, newest retained.
        assert [r["id"] for r in ledger] == [7, 8, 9]
        # A structured eviction reason was recorded for every drop.
        evs = list(app.state.ledger_evictions)
        assert len(evs) == 7
        assert all(e["reason"] == "capacity_fifo" for e in evs)
        assert all(e["ledger"] == "_probe_fifo" for e in evs)
        assert all("description" in e and "category" in e for e in evs)
    finally:
        retention.LEDGER_BOUNDS.pop("_probe_fifo", None)


def test_dict_bound_terminal_first_preserves_pending(tmp_path: Path) -> None:
    from clio_agent.gact.runtime import retention

    app = _fresh_app(tmp_path)
    ledger: dict[str, dict] = {}
    retention.LEDGER_BOUNDS["_probe_perm"] = retention.LedgerBound(
        max_entries=3,
        hard_cap=5,
        is_terminal=lambda r: r.get("status") not in {"pending", "", None},
    )
    try:
        # Insert 3 pending then 3 resolved (interleave so terminal are not all oldest).
        ledger["p0"] = {"status": "pending"}
        retention.enforce_dict_bound(app, ledger, "_probe_perm")
        ledger["r0"] = {"status": "granted"}
        retention.enforce_dict_bound(app, ledger, "_probe_perm")
        ledger["p1"] = {"status": "pending"}
        retention.enforce_dict_bound(app, ledger, "_probe_perm")
        ledger["r1"] = {"status": "denied"}  # len 4 > soft 3 -> evict oldest terminal (r0)
        retention.enforce_dict_bound(app, ledger, "_probe_perm")
        assert "r0" not in ledger
        assert "p0" in ledger and "p1" in ledger  # pending preserved
        evs = list(app.state.ledger_evictions)
        assert evs and evs[-1]["reason"] == "capacity_terminal_first"
    finally:
        retention.LEDGER_BOUNDS.pop("_probe_perm", None)


def test_dict_bound_forced_over_hard_cap_when_all_pending(tmp_path: Path) -> None:
    from clio_agent.gact.runtime import retention

    app = _fresh_app(tmp_path)
    ledger: dict[str, dict] = {}
    retention.LEDGER_BOUNDS["_probe_forced"] = retention.LedgerBound(
        max_entries=3,
        hard_cap=5,
        is_terminal=lambda r: r.get("status") not in {"pending", "", None},
    )
    try:
        for i in range(8):
            ledger[f"p{i}"] = {"status": "pending"}
            retention.enforce_dict_bound(app, ledger, "_probe_forced")
        # No terminal entries exist; forced eviction keeps it at the hard cap.
        assert len(ledger) == 5
        assert list(ledger.keys()) == [f"p{i}" for i in range(3, 8)]
        evs = [e for e in app.state.ledger_evictions if e["ledger"] == "_probe_forced"]
        assert evs and all(e["reason"] == "capacity_forced_pending" for e in evs)
    finally:
        retention.LEDGER_BOUNDS.pop("_probe_forced", None)


def test_eviction_payload_rejects_unknown_reason() -> None:
    from clio_agent.gact.runtime import retention

    with pytest.raises(ValueError):
        retention.ledger_eviction_payload("not_a_reason", ledger="x")


# --------------------------------------------------------------------------- #
# End-to-end: a real write path (share tokens) is bounded through HTTP
# --------------------------------------------------------------------------- #


def test_shared_tokens_bounded_end_to_end(app_client: TestClient, monkeypatch) -> None:
    from clio_agent.gact.runtime import retention

    app = app_client.app
    sess = app_client.post("/v1/sessions", json={"title": "s"}).json()
    sid = sess["id"]

    # Shrink the bound so the test is fast.
    monkeypatch.setitem(
        retention.LEDGER_BOUNDS,
        "shared_tokens",
        retention.LedgerBound(
            max_entries=4,
            hard_cap=6,
            is_terminal=retention.LEDGER_BOUNDS["shared_tokens"].is_terminal,
        ),
    )

    tokens: list[str] = []
    for _ in range(20):
        r = app_client.post(f"/v1/sessions/{sid}/share", json={})
        assert r.status_code == 200
        tokens.append(r.json()["token"])

    # Bounded: never grows to 20.
    assert len(app.state.shared_tokens) <= 6
    # Newest retained, oldest evicted.
    assert tokens[-1] in app.state.shared_tokens
    assert tokens[0] not in app.state.shared_tokens
    # Structured eviction reasons emitted for the share-token ledger.
    evs = [e for e in app.state.ledger_evictions if e["ledger"] == "shared_tokens"]
    assert evs, "expected at least one structured share-token eviction reason"
    assert all("description" in e for e in evs)


def test_context_frames_bounded_per_session_end_to_end(app_client: TestClient, monkeypatch) -> None:
    """Each turn records a context frame; the per-session list must be bounded."""
    from clio_agent.gact.runtime import retention

    from .conftest import complete_turn

    app = app_client.app
    monkeypatch.setitem(
        retention.LEDGER_BOUNDS, "context_frames", retention.LedgerBound(max_entries=3)
    )
    sess = app_client.post("/v1/sessions", json={"title": "c"}).json()
    sid = sess["id"]
    for _ in range(6):
        complete_turn(app_client, sid, "hi")
    frames = app.state.context_frames.get(sid, [])
    assert len(frames) <= 3
    evs = [e for e in app.state.ledger_evictions if e["ledger"] == "context_frames"]
    assert evs, "expected structured context-frame eviction reasons"


# --------------------------------------------------------------------------- #
# Metrics: /v1/metrics must NOT re-walk the whole message map on every poll
# --------------------------------------------------------------------------- #


class _SpyMessages(dict):
    """A dict that counts full-map iterations (values()/__iter__)."""

    walks = 0

    def values(self):  # noqa: D401
        type(self).walks += 1
        return super().values()

    def __iter__(self):
        type(self).walks += 1
        return super().__iter__()


def test_metrics_does_not_rewalk_message_map(app_client: TestClient) -> None:
    from .conftest import complete_turn

    app = app_client.app
    sess = app_client.post("/v1/sessions", json={"title": "m"}).json()
    sid = sess["id"]
    complete_turn(app_client, sid, "hello")

    # Swap in the spy AFTER the turn (counters already hold the aggregate).
    spy = _SpyMessages(app.state.messages)
    app.state.messages = spy
    _SpyMessages.walks = 0

    app_client.get("/v1/metrics")
    after_first = _SpyMessages.walks
    app_client.get("/v1/metrics")
    after_second = _SpyMessages.walks

    # The handler reads running counters, so a poll never iterates the map.
    assert after_first == 0, f"metrics walked the message map {after_first}x on poll 1"
    assert after_second == after_first, "metrics re-walked the map on the second poll"


def test_metrics_values_preserved_after_counter_refactor(app_client: TestClient) -> None:
    """Golden: the reported values must match a from-scratch walk of the map."""
    from clio_agent.gact.types import Message

    from .conftest import complete_turn

    app = app_client.app
    sess = app_client.post("/v1/sessions", json={"title": "g"}).json()
    sid = sess["id"]
    complete_turn(app_client, sid, "hello")

    # Inject a message with tool-call latencies through the real write seam.
    from clio_agent.gact.app import _append_session_message

    _append_session_message(
        app,
        sid,
        Message(
            id="lat1",
            session_id=sid,
            role="assistant",
            created_at="t",
            updated_at="t",
            metadata={
                "tools_called": [
                    {"name": "fs_read_file", "ok": True, "duration_ms": 10.0},
                    {"name": "fs_read_file", "ok": True, "duration_ms": 30.0},
                    {"name": "hdf5.analyze", "ok": True, "duration_ms": 100.0},
                    {"name": "noisy", "ok": True, "duration_ms": 0},
                ]
            },
        ),
    )

    body = app_client.get("/v1/metrics").json()

    # Recompute the message-derived values by an independent full walk.
    exp_total = 0
    exp_roles: dict[str, int] = {}
    exp_samples: dict[str, list[float]] = {}
    for rows in app.state.messages.values():
        exp_total += len(rows)
        for m in rows:
            exp_roles[m.role] = exp_roles.get(m.role, 0) + 1
            for call in (getattr(m, "metadata", None) or {}).get("tools_called") or []:
                dur = call.get("duration_ms")
                if not isinstance(dur, (int, float)) or dur <= 0:
                    continue
                name = str(call.get("name") or call.get("tool") or "tool")
                exp_samples.setdefault(f"tool:{name}", []).append(float(dur))
                exp_samples.setdefault("tool_call", []).append(float(dur))

    assert body["messages"]["total"] == exp_total
    assert body["messages"]["by_role"] == exp_roles
    assert body["latencies"]["tool_call"]["count"] == len(exp_samples["tool_call"])
    assert body["latencies"]["tool_call"]["max_ms"] == max(exp_samples["tool_call"])
    assert body["latencies"]["tool:fs_read_file"]["count"] == 2
    assert "tool:noisy" not in body["latencies"]
