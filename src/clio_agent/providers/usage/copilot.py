"""GitHub Copilot usage, read from the runtime's ``account.getQuota`` after a call.

The runtime reports the signed-in account's monthly quotas (premium requests, chat,
completions) through its ``account.getQuota`` RPC. It is read on the session's own
live runtime after a call finishes, at most once per poll interval, with CLIO's GitHub
credential. A failed read keeps the last good snapshot and records why.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from clio_agent.providers.copilot.errors import teardown_errors
from clio_agent.providers.usage.capture import COPILOT_ACCOUNT
from clio_agent.providers.usage.openrouter import poll_interval_s
from clio_agent.providers.usage.readers import copilot_quota_unrecognised, copilot_snapshot
from clio_agent.providers.usage.snapshot import USAGE_BOARD, unmetered

_TIMEOUT_S = 10.0


def quota_stale() -> bool:
    """Whether the last Copilot quota reading is missing or older than the poll interval."""
    latest = USAGE_BOARD.latest(COPILOT_ACCOUNT)
    if latest is None or not latest.observed_at:
        return True
    try:
        observed = datetime.fromisoformat(latest.observed_at)
    except ValueError:
        return True
    return (datetime.now(UTC) - observed).total_seconds() >= poll_interval_s()


async def read_copilot_quota(client: Any, github_token: str) -> None:
    """Read the account's quotas on ``client`` (a started ``CopilotClient``) when stale."""
    if not quota_stale():
        return
    from copilot.generated.rpc import AccountGetQuotaRequest  # noqa: PLC0415 - optional extra

    try:
        result = await client.rpc.account.get_quota(
            AccountGetQuotaRequest(git_hub_token=github_token), timeout=_TIMEOUT_S
        )
    except teardown_errors() as exc:
        USAGE_BOARD.record_failure(
            COPILOT_ACCOUNT, f"copilot_quota_unreadable: {type(exc).__name__}"
        )
        return
    snapshots = getattr(result, "quota_snapshots", None) or {}
    snapshot = copilot_snapshot(COPILOT_ACCOUNT, snapshots)
    if snapshot is None and (reason := copilot_quota_unrecognised(snapshots)):
        USAGE_BOARD.record_failure(COPILOT_ACCOUNT, reason)
        return
    # None here: every known quota the account has is unlimited, so nothing is metered.
    USAGE_BOARD.record(snapshot or unmetered(COPILOT_ACCOUNT, source="copilot_account_quota"))


def premium_resets_at() -> str | None:
    """When the premium-request allowance resets, from the last quota reading."""
    latest = USAGE_BOARD.latest(COPILOT_ACCOUNT)
    window = next(
        (w for w in (latest.windows if latest else ()) if w.name == "Monthly premium"), None
    )
    return window.resets_at or None if window is not None else None


__all__ = ["premium_resets_at", "quota_stale", "read_copilot_quota"]
