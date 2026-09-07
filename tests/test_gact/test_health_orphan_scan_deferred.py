"""Regression: /v1/health must not pay the full-box orphan scan inline.

The ``child_parentage`` row is a psutil enumeration of every process on the host
(~9s COLD on Windows). /v1/health is polled, so it serves that one row from a
boot-populated cache — returning a typed 'collecting' placeholder before the scan
completes — while the cheap reaper + child_processes rows stay synchronous. A
route-only fallback fills that cache once. Prior to this fix a cold /v1/health
blocked ~10s on the first call and repeatedly rescanned the whole machine.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import clio_agent.runtime.process_census as process_census
import clio_agent.runtime.process_tree as process_tree
from clio_agent.gact.app import build_app
from clio_agent.gact.routes.system import _folded_orphan_scan_rows
from clio_agent.runtime.status import (
    IntegrationState,
    IntegrationStatus,
    collect_runtime_status,
)


class _Agent:
    def forward(self, question: str, session_id: str):
        return type("P", (), {"answer": "ok", "selected_expert": "", "routing_rationale": ""})()


def _rows(body: dict) -> dict[str, dict]:
    return {r["name"]: r for r in body["integrations"]}


def test_collect_excludes_orphan_scan_but_keeps_child_processes() -> None:
    """include_process_census=False drops only the expensive child_parentage row;
    the cheap reaper + child_processes rows are still collected."""

    names_full = {r.name for r in collect_runtime_status(include_process_census=True).integrations}
    names_lite = {r.name for r in collect_runtime_status(include_process_census=False).integrations}

    assert "child_parentage" in names_full
    assert "child_parentage" not in names_lite
    # The cheap own-subtree census + reaper survive on the lite path.
    assert "child_processes" in names_lite


def test_health_defers_orphan_scan_and_fills_in_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First /v1/health returns a child_parentage 'collecting' placeholder (proving
    it did not run the scan inline); once the background fill completes the real
    row replaces it. child_processes is present and real on every call."""

    release = threading.Event()
    scanned = threading.Event()

    async def _blocked_scan() -> list[IntegrationStatus]:
        # Model the ~9s cold walk: block until the test releases it, so the first
        # poll is guaranteed to see the placeholder (not a fast-filled real row).
        scanned.set()
        await asyncio.to_thread(release.wait, 30)
        return [
            IntegrationStatus(
                name="child_parentage",
                state=IntegrationState.READY,
                summary="SCAN_COMPLETE",
                config_source="runtime:process_census",
                next_action="No action required.",
            )
        ]

    monkeypatch.setattr(process_census, "boot_reap_off_loop", _blocked_scan)

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        first = _rows(client.get("/v1/health").json())
        # The scan was kicked (background) but has NOT been folded inline.
        assert scanned.wait(timeout=3.0), "background orphan scan was never kicked"
        assert "child_processes" in first  # cheap, synchronous, always present
        assert "child_parentage" in first
        assert "collecting" in first["child_parentage"]["detail"].lower()

        # Release the scan; the cache fills, and a later poll serves the real row.
        release.set()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and getattr(app.state, "orphan_scan_rows", None) is None:
            time.sleep(0.02)
        assert getattr(app.state, "orphan_scan_rows", None) is not None

        later = _rows(client.get("/v1/health").json())
        assert later["child_parentage"]["detail"] == "SCAN_COMPLETE"


def test_orphan_scan_refresh_flag_clears_even_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fill that raises must still clear ``orphan_scan_refreshing`` (finally) and
    serve a typed degraded row — never wedge the guard True (which would freeze the
    cache and silently kill orphan detection forever)."""

    async def _failed_scan() -> list[IntegrationStatus]:
        return [
            IntegrationStatus(
                name="child_parentage",
                state=IntegrationState.DEGRADED,
                summary="boot orphan scan collection failed: RuntimeError('psutil exploded')",
                config_source="runtime:process_census",
                next_action="Inspect the gact server logs for the orphan-scan failure.",
            )
        ]

    monkeypatch.setattr(process_census, "boot_reap_off_loop", _failed_scan)

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        client.get("/v1/health")  # kicks the fill (which raises)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and getattr(app.state, "orphan_scan_rows", None) is None:
            time.sleep(0.02)
        # The flag was released despite the error, and a typed degraded row is served.
        assert app.state.orphan_scan_refreshing is False
        row = _rows(client.get("/v1/health").json())["child_parentage"]
        assert row["status"] == "degraded"
        assert "psutil exploded" in row["detail"]


def test_health_does_not_repeat_a_cached_full_box_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached boot census is stable; health polling never starts a timed rescan."""

    def _unexpected_scan() -> list[IntegrationStatus]:
        raise AssertionError("cached health must not rescan the machine")

    monkeypatch.setattr(process_tree, "live_orphan_scan_rows", _unexpected_scan)
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    cached = IntegrationStatus(
        name="child_parentage",
        state=IntegrationState.READY,
        summary="BOOT_SCAN",
        config_source="runtime:process_census",
        next_action="No action required.",
    )
    app.state.orphan_scan_rows = [cached]
    app.state.orphan_scan_at = 0.0

    for _ in range(3):
        assert _folded_orphan_scan_rows(app) == [cached]
