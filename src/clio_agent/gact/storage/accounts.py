"""Private reusable OAuth grants, separate from workspace source permissions."""

from __future__ import annotations

import hashlib
import time

from clio_agent.gact.storage.models import SourceRecord

Entries = dict[str, dict[str, object]]
OAUTH_PROVIDERS = {"google_drive", "globus", "github"}


def source_binding(record: SourceRecord) -> dict[str, object]:
    """Describe a source approval without putting credentials in its public record."""
    source = record.source
    return {
        "principal": record.principal,
        "clio_id": source.owner.clio_id,
        "host_id": source.owner.host_id,
        "provider": source.provider,
        "root": source.root,
        "mode": source.mode,
    }


def owned_by(
    entry: dict[str, object], principal: str, clio_id: str, host_id: str, provider: str
) -> bool:
    """Keep saved accounts inside the same user and CLIO host boundary."""
    return all(
        entry.get(key) == value
        for key, value in {
            "principal": principal,
            "clio_id": clio_id,
            "host_id": host_id,
            "provider": provider,
        }.items()
    )


def usable(entry: dict[str, object]) -> bool:
    """A current access token or refreshable grant counts as signed in."""
    tokens = entry.get("tokens")
    return bool(
        isinstance(tokens, dict)
        and tokens.get("access_token")
        and (
            tokens.get("non_expiring")
            or tokens.get("refresh_token")
            or float(tokens.get("expires_at", 0)) > time.time()
        )
        and not entry.get("invalid")
    )


def migrate_accounts(entries: Entries) -> bool:
    """Promote old source logins without merging possibly different provider accounts."""
    changed = False
    for identifier, entry in list(entries.items()):
        if entry.get("kind") == "account" or entry.get("provider") not in OAUTH_PROVIDERS:
            continue
        tokens = entry.get("tokens")
        if not isinstance(tokens, dict):
            continue
        account_id = "account_" + hashlib.sha256(identifier.encode()).hexdigest()
        entries[account_id] = {
            **{
                key: entry.get(key)
                for key in ("principal", "clio_id", "host_id", "provider", "mode")
            },
            "kind": "account",
            "tokens": tokens,
            "saved_at": min(time.time(), float(tokens.get("expires_at", 0)) - 3600),
        }
        entry.pop("tokens")
        entry["account_id"] = account_id
        changed = True
    return changed


def account_for(entries: Entries, record: SourceRecord) -> tuple[str, dict[str, object]] | None:
    """Reuse a login, keeping an already chosen account and its granted access level."""
    source = record.source

    def compatible(entry: dict[str, object]) -> bool:
        return (
            entry.get("kind") == "account"
            and owned_by(
                entry, record.principal, source.owner.clio_id, source.owner.host_id, source.provider
            )
            and usable(entry)
            and (
                source.provider != "google_drive"
                or record.linked_access == "read_only"
                or entry.get("mode") in {"working_copy", "write_enabled"}
            )
        )

    binding = entries.get(source.id, {})
    if binding.get("disconnected") or not record.connected or record.removed:
        return None
    account_id = binding.get("account_id")
    if isinstance(account_id, str):
        account = entries.get(account_id, {})
        if compatible(account):
            return account_id, account
        # Do not silently switch an established source to another provider account.
        return None
    candidates = [(key, value) for key, value in entries.items() if compatible(value)]

    def saved_at(pair: tuple[str, dict[str, object]]) -> float:
        value = pair[1].get("saved_at", 0)
        return float(value) if isinstance(value, (int, float)) else 0

    return max(candidates, key=saved_at, default=None)
