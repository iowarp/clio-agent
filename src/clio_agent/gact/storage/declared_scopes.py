"""Account scopes that plugins declare (host semantics HS4), and the grants they yield.

A plugin may declare extra scopes for a provider account. The sign-in request adds
them, the account remembers what it asked for, and a scope declared later makes the
account need signing in again. :func:`grant_rows` turns an account's stored tokens
into the rows of the account grant files a plugin's servers read.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

#: Providers whose sign-in request carries scopes; a GitHub App's permissions are fixed
#: on the App, so a plugin's GitHub scopes request nothing.
SCOPED_PROVIDERS = frozenset({"google_drive", "globus", "ndp"})


def declared_scopes(provider: str) -> list[str]:
    """Scopes registered plugins declare for ``provider`` (HS4), for its sign-in request."""

    if provider not in SCOPED_PROVIDERS:
        return []
    from clio_agent.plugins.host_semantics import declared_account_scopes  # noqa: PLC0415

    return declared_account_scopes(provider)


def lacks_declared_scopes(account: dict[str, object]) -> bool:
    """Whether a plugin now declares a scope this account's sign-in did not request."""

    wanted = declared_scopes(str(account.get("provider") or ""))
    if not wanted:
        return False
    granted = account.get("declared_scopes")
    have = {str(scope) for scope in granted} if isinstance(granted, list) else set()
    return any(scope not in have for scope in wanted)


def with_scopes(scope: str, extra: list[str]) -> str:
    """``scope`` with each of ``extra`` appended once."""

    words = scope.split()
    words.extend(item for item in extra if item not in words)
    return " ".join(words)


def globus_scope_tokens(
    candidates: list[dict[str, Any]], resource_server: str
) -> dict[str, dict[str, object]]:
    """Tokens a Globus exchange returned for other servers whose scopes a plugin declared."""

    wanted = set(declared_scopes("globus"))
    return {
        str(row["resource_server"]): {
            "access_token": row["access_token"],
            "refresh_token": row.get("refresh_token", ""),
            "expires_at": time.time() + float(row.get("expires_in", 3600)),
            "scope": str(row.get("scope", "")),
        }
        for row in candidates
        if row.get("resource_server")
        and row.get("resource_server") != resource_server
        and isinstance(row.get("access_token"), str)
        and wanted & set(str(row.get("scope", "")).split())
    }


def _iso(epoch: object) -> str:
    try:
        stamp = float(epoch)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    return datetime.fromtimestamp(stamp, UTC).isoformat().replace("+00:00", "Z")


def grant_rows(
    entry: dict[str, object], tokens: dict[str, object], main_server: str
) -> list[dict[str, object]]:
    """The unexpired grant-file rows of an account: its main token and each scope token.

    Refresh tokens are never included. A main token with no recorded scope reports the
    scopes the account's sign-in declared.
    """

    rows: list[tuple[str, dict[str, object]]] = [(main_server, tokens)]
    extra = tokens.get("scope_tokens")
    if isinstance(extra, dict):
        rows.extend((str(k), v) for k, v in extra.items() if isinstance(v, dict))
    now = time.time()
    out: list[dict[str, object]] = []
    for server, row in rows:
        expires = row.get("expires_at", 0)
        if not row.get("non_expiring") and float(expires or 0) <= now:  # type: ignore[arg-type]
            continue
        scopes = str(row.get("scope") or "").split()
        if not scopes and row is tokens:
            declared = entry.get("declared_scopes")
            scopes = [str(s) for s in declared] if isinstance(declared, list) else []
        out.append(
            {
                "resource_server": server,
                "access_token": str(row.get("access_token") or ""),
                "token_type": "Bearer",
                "expires_at": "" if row.get("non_expiring") else _iso(expires),
                "scopes": scopes,
            }
        )
    return out


__all__ = [
    "SCOPED_PROVIDERS",
    "declared_scopes",
    "globus_scope_tokens",
    "grant_rows",
    "lacks_declared_scopes",
    "with_scopes",
]
