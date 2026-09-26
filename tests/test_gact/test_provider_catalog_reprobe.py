"""Background provider-catalog re-probe: bounded, and never on a dead-end reason.

A provider stuck on ``argonne_reauthentication_required`` (ALCF's "high-assurance
timeout") cannot be fixed by retrying on a timer -- only a fresh Globus sign-in
does, and sign-in completion already invalidates the provider so the very next
catalog read re-discovers it. These tests pin that the background re-probe loop
in :mod:`clio_agent.gact.provider_catalog_reprobe` excludes that reason from
both scheduling and its own stop condition, while still re-probing an ordinary
stale (last-good) provider as before.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import provider_catalog_reprobe as reprobe
from clio_agent.gact import provider_catalog_snapshot as snapshot
from clio_agent.providers.model_discovery import LAST_GOOD_CATALOG_SOURCE


def _stale_record(provider_id: str, *, failure: str) -> dict[str, Any]:
    return {
        "id": provider_id,
        "freshness": {
            "source": LAST_GOOD_CATALOG_SOURCE,
            "generated_at": "2026-09-22T10:00:00+00:00",
            "staleness": {
                "reason": "last_good_catalog_served",
                "live_failure": failure,
            },
        },
        "failure": failure,
        "models": [],
    }


class _FakeApp:
    def __init__(self, catalog: dict[str, Any]) -> None:
        self.state = SimpleNamespace(provider_catalog=catalog)


@pytest.mark.asyncio
async def test_schedule_stale_reprobe_skips_reauth_required_providers() -> None:
    """A provider stuck on the reauth-required reason gets no background task."""
    payload = {
        "authoritative": "live_handshake",
        "providers": [
            _stale_record(
                "argonne_metis",
                failure=(
                    "argonne_reauthentication_required: Error: Permission denied "
                    "from internal policies. This is likely due to a "
                    "high-assurance timeout."
                ),
            )
        ],
    }
    app = _FakeApp(payload)

    task = reprobe.schedule_stale_reprobe(app, payload)

    assert task is None
    assert getattr(app.state, reprobe.REPROBE_TASK_ATTR, None) is None


@pytest.mark.asyncio
async def test_schedule_stale_reprobe_still_runs_for_ordinary_stale_providers() -> None:
    """An ordinary stale provider (unreachable, expired token, ...) still reprobes."""
    payload = {
        "authoritative": "live_handshake",
        "providers": [
            _stale_record("argonne_metis", failure="connectivity=unreachable auth=missing")
        ],
    }
    app = _FakeApp(payload)

    task = reprobe.schedule_stale_reprobe(app, payload)

    assert task is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_reprobe_until_live_returns_immediately_when_only_reauth_required_remains() -> None:
    """The loop's own stop condition must not keep it alive on a dead-end reason."""
    payload = {
        "authoritative": "live_handshake",
        "providers": [
            _stale_record(
                "argonne_metis", failure="argonne_reauthentication_required: high-assurance timeout"
            )
        ],
    }
    app = _FakeApp(payload)

    # Must return with no sleep at all -- the while condition is already false.
    # A real bug here would hang for REPROBE_BACKOFF_S[0] == 5s and this
    # timeout would fail the test.
    await asyncio.wait_for(reprobe.reprobe_until_live(app), timeout=1.0)


@pytest.mark.asyncio
async def test_attempt_marks_the_provider_as_checking_only_while_the_probe_is_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client-visible ``checking`` overlay (#1446 follow-up) must be true
    for exactly the duration of the real handshake -- neither before it starts
    nor after it (or a failed attempt) settles."""
    payload = {
        "authoritative": "live_handshake",
        "providers": [
            _stale_record("argonne_metis", failure="connectivity=unreachable auth=missing")
        ],
    }
    app = _FakeApp(payload)
    seen_during: frozenset[str] = frozenset()

    async def fake_discover(app_arg: Any, ids: list[str], *, refresh: bool = False) -> list[Any]:
        nonlocal seen_during
        seen_during = snapshot.checking_provider_ids(app_arg)
        return []

    monkeypatch.setattr(reprobe.snapshot, "discover", fake_discover)

    assert snapshot.checking_provider_ids(app) == frozenset()
    await reprobe._attempt(app, 1, ["argonne_metis"])

    assert seen_during == frozenset({"argonne_metis"})
    assert snapshot.checking_provider_ids(app) == frozenset()


@pytest.mark.asyncio
async def test_attempt_clears_checking_even_when_the_probe_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed attempt must never leave a provider permanently "checking"."""
    payload = {
        "authoritative": "live_handshake",
        "providers": [
            _stale_record("argonne_metis", failure="connectivity=unreachable auth=missing")
        ],
    }
    app = _FakeApp(payload)

    async def failing_discover(app_arg: Any, ids: list[str], *, refresh: bool = False) -> list[Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(reprobe.snapshot, "discover", failing_discover)

    with pytest.raises(RuntimeError):
        await reprobe._attempt(app, 1, ["argonne_metis"])

    assert snapshot.checking_provider_ids(app) == frozenset()


@pytest.mark.asyncio
async def test_reprobe_target_mixes_a_retryable_provider_alongside_a_blocked_one() -> None:
    """A mixed snapshot still schedules -- only the blocked provider is excluded."""
    payload = {
        "authoritative": "live_handshake",
        "providers": [
            _stale_record(
                "argonne_metis", failure="argonne_reauthentication_required: high-assurance timeout"
            ),
            _stale_record("lm_studio", failure="connectivity=unreachable auth=not_required"),
        ],
    }
    app = _FakeApp(payload)

    task = reprobe.schedule_stale_reprobe(app, payload)

    assert task is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
