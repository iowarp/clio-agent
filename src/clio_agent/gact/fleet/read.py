"""Read the fleet: accounts with their usage and spend, and the models they serve.

The one owner of the model list (design: ``docs/design/sdk/model-fleet.md``,
"One owner"): ``GET /v1/fleet``, the agent's ``list_models`` and ``usage``
tools and model selection all call :func:`read_fleet`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from clio_agent.gact.fleet.entries import FleetAccount, FleetEntry, project_fleet
from clio_agent.gact.fleet.spend import SpendReport, spend_by_account, workspace_spend
from clio_agent.gact.provider_catalog_snapshot import read_catalog
from clio_agent.providers.usage.openrouter import refresh_openrouter_usage
from clio_agent.providers.usage.snapshot import USAGE_BOARD, UsageSnapshot, now_iso, unmetered

if TYPE_CHECKING:
    from fastapi import FastAPI

#: Accounts whose usage is read from an endpoint when the fleet is read.
_POLLED = {"openrouter": refresh_openrouter_usage}


@dataclass(frozen=True)
class Fleet:
    """One reading of the fleet."""

    accounts: list[FleetAccount]
    entries: list[FleetEntry]
    usage: dict[str, UsageSnapshot | None]
    spend: SpendReport
    observed_at: str

    def entry(self, ref: str) -> FleetEntry | None:
        return next((entry for entry in self.entries if entry.ref == ref), None)

    def account(self, account_id: str) -> FleetAccount | None:
        return next((account for account in self.accounts if account.id == account_id), None)

    def account_wire(self, account: FleetAccount) -> dict[str, Any]:
        snapshot = self.usage.get(account.id)
        spend = spend_by_account(self.spend.by_model).get(account.id)
        return {
            **account.to_dict(),
            "usage": snapshot.to_dict() if snapshot is not None else None,
            # Typed, so a reader never mistakes "not yet observed" for "no limits".
            "usage_missing_reason": "" if snapshot is not None else "no_reading_yet",
            "spend": spend.to_dict() if spend is not None else None,
        }

    def to_wire(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at,
            "accounts": [self.account_wire(account) for account in self.accounts],
            "entries": [entry.to_dict() for entry in self.entries],
            "spend_by_model": {ref: s.to_dict() for ref, s in self.spend.by_model.items()},
            "unreadable_sessions": self.spend.unreadable_sessions,
        }


def _usage(account: FleetAccount) -> UsageSnapshot | None:
    if account.billing == "free":
        return unmetered(
            account.id, source="local_endpoint" if account.endpoint else "site_endpoint"
        )
    return USAGE_BOARD.latest(account.id)


async def read_fleet(app: "FastAPI", *, refresh: bool = False) -> Fleet:
    """The fleet now: catalog accounts and models, usage, and workspace spend."""

    accounts, entries = project_fleet(await read_catalog(app, refresh=refresh))
    for account in accounts:
        if poll := _POLLED.get(account.id):
            await poll(force=refresh)
    return Fleet(
        accounts=accounts,
        entries=entries,
        usage={account.id: _usage(account) for account in accounts},
        spend=workspace_spend(app),
        observed_at=now_iso(),
    )


__all__ = ["Fleet", "read_fleet"]
