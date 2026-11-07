"""GitHub Copilot quota reads: ``account.getQuota`` after a call, at most once per interval.

An account whose quotas are all unlimited is unmetered (not "unknown"); a failed read
keeps the last good snapshot and records why; a fresh reading is not re-read.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.providers.usage import copilot as usage
from clio_agent.providers.usage.capture import COPILOT_ACCOUNT
from clio_agent.providers.usage.snapshot import USAGE_BOARD
from tests import _fake_copilot_sdk as fake

_PREMIUM = {
    "premium_interactions": {
        "entitlement_requests": 300,
        "used_requests": 120,
        "remaining_percentage": 60.0,
        "is_unlimited_entitlement": False,
        "reset_date": "2026-12-01",
    }
}


@pytest.fixture(autouse=True)
def _board(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    fake.install(monkeypatch)  # provides ``copilot.generated.rpc.AccountGetQuotaRequest``
    USAGE_BOARD.clear()
    yield
    USAGE_BOARD.clear()


def _client(*results: Any) -> SimpleNamespace:
    calls: list[tuple[str, float]] = []
    replies = iter(results)

    async def get_quota(request: Any, *, timeout: float) -> Any:
        calls.append((request.git_hub_token, timeout))
        reply = next(replies)
        if isinstance(reply, BaseException):
            raise reply
        return SimpleNamespace(quota_snapshots=reply)

    return SimpleNamespace(
        rpc=SimpleNamespace(account=SimpleNamespace(get_quota=get_quota)), calls=calls
    )


async def test_premium_requests_are_the_metered_window_and_balance() -> None:
    client = _client(_PREMIUM)
    await usage.read_copilot_quota(client, "ghu_token")
    snapshot = USAGE_BOARD.latest(COPILOT_ACCOUNT)
    assert snapshot is not None and snapshot.state == "metered"
    assert client.calls == [("ghu_token", usage._TIMEOUT_S)]
    assert snapshot.balance is not None
    assert (snapshot.balance.limit, snapshot.balance.used, snapshot.balance.remaining) == (
        300,
        120,
        180,
    )
    assert snapshot.balance.currency == "premium requests"
    assert usage.premium_resets_at() == "2026-12-01"


async def test_a_fresh_reading_is_not_read_again() -> None:
    client = _client(_PREMIUM, _PREMIUM)
    await usage.read_copilot_quota(client, "ghu_token")
    assert usage.quota_stale() is False
    await usage.read_copilot_quota(client, "ghu_token")
    assert len(client.calls) == 1


async def test_all_unlimited_quotas_are_unmetered_not_unknown() -> None:
    client = _client({"chat": {"is_unlimited_entitlement": True}})
    await usage.read_copilot_quota(client, "ghu_token")
    snapshot = USAGE_BOARD.latest(COPILOT_ACCOUNT)
    assert snapshot is not None and snapshot.state == "unmetered"
    assert snapshot.source == "copilot_account_quota"
    assert usage.premium_resets_at() is None


@pytest.mark.parametrize(
    ("reply", "keys"),
    [
        ({}, "none"),
        ({"premiumInteractions": {"isUnlimitedEntitlement": False}}, "premiumInteractions"),
        ({"premium_interactions": {"is_unlimited_entitlement": False}}, "premium_interactions"),
    ],
)
async def test_a_quota_reply_it_cannot_read_is_never_called_unmetered(
    reply: dict[str, Any], keys: str
) -> None:
    """SABOTAGE: record ``unmetered`` whenever no window was read -> state unmetered -> red."""
    await usage.read_copilot_quota(_client(reply), "ghu_token")
    snapshot = USAGE_BOARD.latest(COPILOT_ACCOUNT)
    assert snapshot is not None and snapshot.state != "unmetered"
    assert snapshot.read_failure == (
        f"copilot_quota_unreadable: no metered or unlimited quota (keys: {keys})"
    )


async def test_a_failed_read_keeps_the_last_reading_and_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await usage.read_copilot_quota(_client(_PREMIUM), "ghu_token")
    monkeypatch.setattr(usage, "quota_stale", lambda: True)
    await usage.read_copilot_quota(_client(fake.JsonRpcError("account.getQuota: 500")), "t")
    snapshot = USAGE_BOARD.latest(COPILOT_ACCOUNT)
    assert snapshot is not None
    assert snapshot.state == "metered"  # the last good value stays
    assert snapshot.read_failure == "copilot_quota_unreadable: JsonRpcError"
    assert snapshot.read_failed_at


def test_no_reading_or_an_unparseable_time_is_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    assert usage.quota_stale() is True
    broken = SimpleNamespace(observed_at="not-a-time")
    monkeypatch.setattr(USAGE_BOARD, "latest", lambda _account: broken)
    assert usage.quota_stale() is True
