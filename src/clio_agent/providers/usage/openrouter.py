"""OpenRouter usage, read from its key endpoint when the snapshot is old.

OpenRouter reports a key's limit, what it has used and what remains at
``GET /api/v1/key`` (documented; checked 2026-10-23). It is read on demand,
when the fleet is read and the last snapshot is older than the poll interval,
with the credential the account was connected with, never another tool's.
A failed read keeps the last good snapshot and records why.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from clio_agent import conf
from clio_agent.providers.catalog import get_provider
from clio_agent.providers.model_discovery.overlay import resolve_cloud_api_key
from clio_agent.providers.usage.readers import openrouter_snapshot
from clio_agent.providers.usage.snapshot import USAGE_BOARD

ACCOUNT = "openrouter"
_TIMEOUT_S = 10.0


def poll_interval_s() -> int:
    return conf.resolve(
        "fleet.usage_poll_seconds",
        env="CLIO_FLEET_USAGE_POLL_SECONDS",
        default=300,
        cast=conf.as_int,
    )


def _fresh(observed_at: str) -> bool:
    try:
        observed = datetime.fromisoformat(observed_at)
    except ValueError:
        return False
    return (datetime.now(UTC) - observed).total_seconds() < poll_interval_s()


async def refresh_openrouter_usage(*, force: bool = False) -> None:
    """Read OpenRouter's key endpoint into the usage board unless the snapshot is fresh."""

    latest = USAGE_BOARD.latest(ACCOUNT)
    if not force and latest is not None and latest.observed_at and _fresh(latest.observed_at):
        return
    preset = get_provider(ACCOUNT)
    key = resolve_cloud_api_key(ACCOUNT)
    if preset is None or not key:
        USAGE_BOARD.record_failure(ACCOUNT, "openrouter_key_missing")
        return
    url = preset.api_base.rstrip("/") + "/key"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {key}"})
        response.raise_for_status()
        USAGE_BOARD.record(openrouter_snapshot(ACCOUNT, response.json()))
    except httpx.HTTPStatusError as exc:
        USAGE_BOARD.record_failure(ACCOUNT, f"openrouter_key_http_{exc.response.status_code}")
    except httpx.HTTPError as exc:
        USAGE_BOARD.record_failure(ACCOUNT, f"openrouter_key_unreachable: {type(exc).__name__}")
    except ValueError as exc:
        USAGE_BOARD.record_failure(ACCOUNT, f"openrouter_key_unreadable: {exc}")


__all__ = ["ACCOUNT", "poll_interval_s", "refresh_openrouter_usage"]
