"""CLIO-BBBBBBBBBB17: /v1/health integrations[] reflects wired state.

The TUI's /doctor modal reads this table to colour-chip each
subsystem. We pin the shape + the overall_status collapse rule so
the modal never misrepresents what's actually running.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


class _RealishAgent:
    """Stand-in for the real ClioAgent — lives under this test
    module so the health handler's fake-detection heuristic flags
    it as a dev harness. The actual test payload doesn't matter."""

    def forward(self, question: str, session_id: str):
        raise NotImplementedError


class _FakeARC:
    def get_cache_stats(self) -> dict:
        return {"hits": 80, "misses": 20, "hit_rate": 0.80, "capacity": 1000}


class _BrokenARC:
    def get_cache_stats(self) -> dict:
        raise RuntimeError("disk unreachable")


def _health(app) -> dict:
    return TestClient(app).get("/v1/health").json()


def test_no_agent_no_arc_overall_is_unavailable(tmp_path: Path) -> None:
    body = _health(build_app(sessions_path=tmp_path / "s.json"))
    rows = {r["name"]: r for r in body["integrations"]}
    assert rows["agent"]["status"] == "unavailable"
    assert rows["memory"]["status"] == "degraded"
    # `unavailable` beats `degraded` beats `ready`.
    assert body["overall_status"] == "unavailable"
    assert body["healthy"] is False


def test_fake_agent_flagged_degraded(tmp_path: Path) -> None:
    body = _health(build_app(
        sessions_path=tmp_path / "s.json",
        agent=_RealishAgent(),
        arc=_FakeARC(),
    ))
    rows = {r["name"]: r for r in body["integrations"]}
    assert rows["agent"]["status"] == "degraded"  # tests module -> fake
    assert "fake" in rows["agent"]["detail"].lower()
    assert rows["memory"]["status"] == "ready"
    assert "hit rate" in rows["memory"]["detail"]
    # Degraded because the agent is fake; nothing unavailable.
    assert body["overall_status"] == "degraded"
    assert body["healthy"] is True


def test_broken_arc_reports_unavailable(tmp_path: Path) -> None:
    body = _health(build_app(
        sessions_path=tmp_path / "s.json",
        agent=_RealishAgent(),
        arc=_BrokenARC(),
    ))
    rows = {r["name"]: r for r in body["integrations"]}
    assert rows["memory"]["status"] == "unavailable"
    assert "raised" in rows["memory"]["detail"]
