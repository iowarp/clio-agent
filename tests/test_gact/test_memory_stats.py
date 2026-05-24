"""CLIO-BBBBBBBBBB11: tests for /v1/memory/stats.

Drives the app with a FakeARC so we don't need a real ARC instance
(which would touch disk + spin up indexes). Covers:

- ARC wired -> cache + global stats reported
- ARC not wired -> zeros (per SPEC §6.19 'zeros are a valid signal')
- ?session_id with known session -> session block populated
- ?session_id with unknown session -> empty session block (NOT a 404)
- Wire shape uses 'global' (not 'global_') as the JSON key
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


class FakeARC:
    """Stand-in for ARCMemory exposing the get_cache_stats() shape
    the GACT layer actually depends on."""

    def __init__(
        self,
        *,
        hits: int = 0,
        misses: int = 0,
        capacity: int = 1000,
        conv_index_size: int = 0,
        inv_index_size: int = 0,
    ) -> None:
        total = hits + misses
        self._stats = {
            "hits": hits,
            "misses": misses,
            "hit_rate": (hits / total) if total else 0.0,
            "capacity": capacity,
            "conv_index_size": conv_index_size,
            "inv_index_size": inv_index_size,
        }

    def get_cache_stats(self) -> dict[str, Any]:
        return dict(self._stats)


@pytest.fixture()
def client_with_arc(tmp_path: Path) -> TestClient:
    arc = FakeARC(hits=80, misses=20, conv_index_size=12, inv_index_size=42)
    return TestClient(build_app(sessions_path=tmp_path / "s.json", arc=arc))


def test_memory_stats_reports_cache_counters(client_with_arc: TestClient) -> None:
    resp = client_with_arc.get("/v1/memory/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cache"]["hits"] == 80
    assert body["cache"]["misses"] == 20
    assert body["cache"]["hit_rate"] == pytest.approx(0.8)
    assert body["cache"]["capacity"] == 1000


def test_memory_stats_reports_global_counters(client_with_arc: TestClient) -> None:
    body = client_with_arc.get("/v1/memory/stats").json()
    # 'global' is the JSON key (Python keyword aliased server-side).
    assert "global" in body, f"missing global key: {body}"
    g = body["global"]
    assert g["conversations_total"] == 12
    assert g["invocations_total"] == 42


def test_memory_stats_session_block_populated_when_session_id_set(
    client_with_arc: TestClient,
) -> None:
    sid = client_with_arc.post("/v1/sessions", json={"title": "x"}).json()["id"]
    body = client_with_arc.get(f"/v1/memory/stats?session_id={sid}").json()
    assert body["session"] is not None
    s = body["session"]
    assert s["session_id"] == sid
    assert s["tokens_budget"] == 4000


def test_memory_stats_unknown_session_returns_empty_block_not_404(
    client_with_arc: TestClient,
) -> None:
    """SPEC §6.19 implies the endpoint always 200s — the TUI's
    footer chip can poll continuously without spamming 404s on a
    session that's about to be created."""

    resp = client_with_arc.get("/v1/memory/stats?session_id=sess_nope")
    assert resp.status_code == 200
    s = resp.json()["session"]
    assert s["session_id"] == "sess_nope"
    assert s["messages_retained"] == 0


def test_memory_stats_no_session_query_returns_no_session_block(
    client_with_arc: TestClient,
) -> None:
    body = client_with_arc.get("/v1/memory/stats").json()
    assert body["session"] is None, (
        f"session block should be absent without ?session_id; got {body['session']}"
    )


def test_memory_stats_without_arc_returns_zeros(tmp_path: Path) -> None:
    """When ARC isn't wired (smoke-boot scenarios, scaffold tests),
    the endpoint returns 200 with zero counters rather than crashing
    or 503ing — so the chip just renders 'cache --' instead of
    breaking."""

    app = build_app(sessions_path=tmp_path / "s.json", arc=None)
    with TestClient(app) as c:
        body = c.get("/v1/memory/stats").json()
        assert body["cache"]["hits"] == 0
        assert body["cache"]["misses"] == 0
        assert body["cache"]["hit_rate"] == 0.0
        assert body["global"]["conversations_total"] == 0
