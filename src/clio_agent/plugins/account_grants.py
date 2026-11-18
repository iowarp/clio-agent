"""Account grant files a plugin's server reads (HS4).

A plugin declares ``accounts`` (a provider and its scopes). For each, CLIO keeps a
private JSON file with the user's current grant and names it in the server's
environment as ``CLIO_ACCOUNT_<PROVIDER>_FILE``. The token is never in the
environment or in tool arguments. The file exists only while a grant does.

Format, version 1::

    {"version": 1, "provider": "globus", "written_at": "<iso>",
     "tokens": [{"resource_server": "...", "access_token": "...",
                 "token_type": "Bearer", "expires_at": "<iso>", "scopes": [...]}]}

Refresh tokens are never written. Writes are atomic (temporary file, then rename),
in a directory only the user can read.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GRANT_FILE_VERSION = 1
_TOKEN_FIELDS = ("resource_server", "access_token", "token_type", "expires_at", "scopes")


def grants_root() -> Path:
    """The private directory holding every plugin's grant files."""

    from clio_agent.plugins.service import plugin_store_root  # noqa: PLC0415

    return plugin_store_root() / "grants"


def grant_path(plugin: str, provider: str) -> Path:
    """Where ``plugin``'s grant for ``provider`` is written (it may not exist)."""

    return grants_root() / plugin / f"{provider}.json"


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        for folder in (grants_root(), path):
            if folder.exists():
                folder.chmod(0o700)


def grant_document(provider: str, tokens: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The version 1 grant document for ``tokens`` (only the documented fields)."""

    rows = []
    for token in tokens:
        row = {key: token.get(key) for key in _TOKEN_FIELDS}
        row["resource_server"] = str(row["resource_server"] or "")
        row["access_token"] = str(row["access_token"] or "")
        row["token_type"] = str(row["token_type"] or "Bearer")
        row["expires_at"] = str(row["expires_at"] or "")
        row["scopes"] = [str(scope) for scope in row["scopes"] or []]
        if row["access_token"]:
            rows.append(row)
    return {
        "version": GRANT_FILE_VERSION,
        "provider": provider,
        "written_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "tokens": rows,
    }


def write_grant(plugin: str, provider: str, tokens: Sequence[Mapping[str, Any]]) -> Path | None:
    """Atomically write ``plugin``'s grant for ``provider``; remove it when no token remains.

    Returns the path written, or ``None`` when the grant was removed.
    """

    path = grant_path(plugin, provider)
    document = grant_document(provider, tokens)
    if not document["tokens"]:
        remove_grant(plugin, provider)
        return None
    _private_dir(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=".grant-", dir=path.parent)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, separators=(",", ":"))
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return path


def remove_grant(plugin: str, provider: str) -> bool:
    """Delete ``plugin``'s grant file for ``provider``; ``True`` when one existed."""

    path = grant_path(plugin, provider)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def read_grant(plugin: str, provider: str) -> dict[str, Any] | None:
    """The grant document as written, or ``None`` when there is none."""

    try:
        return json.loads(grant_path(plugin, provider).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


__all__ = [
    "GRANT_FILE_VERSION",
    "grant_document",
    "grant_path",
    "grants_root",
    "read_grant",
    "remove_grant",
    "write_grant",
]
