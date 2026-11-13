"""S3 access keys, kept in :class:`StorageAuth`'s private credential file.

A key pair is stored once, when an S3 source that uses keys is created, under its own
vault key and bound to that source's owner, root and mode (the same binding SFTP
credentials use), so it cannot be reused by another source. It is read only to build
the source's host-side S3 client and is removed when the source is disconnected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from clio_agent.gact.storage.models import S3Credentials, SourceRecord

if TYPE_CHECKING:
    from clio_agent.gact.storage.auth import StorageAuth

#: Vault key prefix of an S3 source's key pair (never an OAuth account entry).
PREFIX = "s3-keys:"


def save(auth: StorageAuth, record: SourceRecord, credentials: S3Credentials) -> None:
    """Store ``credentials`` for ``record`` in the private credential file."""
    if record.source.provider != "s3" or record.configuration.s3_access != "keys":
        raise ValueError("Access keys apply only to an S3 source that uses keys")
    key_id = credentials.access_key_id.get_secret_value().strip()
    secret = credentials.secret_access_key.get_secret_value().strip()
    if not key_id or not secret:
        raise ValueError("Enter both the access key ID and the secret access key")
    source = record.source
    with auth._lock:
        entries = auth._entries()
        entries[PREFIX + source.id] = {
            "principal": record.principal,
            "clio_id": source.owner.clio_id,
            "host_id": source.owner.host_id,
            "provider": source.provider,
            "root": source.root,
            "mode": source.mode,
            "access_key_id": key_id,
            "secret_access_key": secret,
        }
        auth._vault.write_entries(entries)


def keys(auth: StorageAuth, record: SourceRecord) -> tuple[str, str] | None:
    """The key pair for ``record``'s S3 client; ``None`` for anonymous or profile access."""
    if record.configuration.s3_access != "keys":
        return None
    with auth._lock:
        stored = auth._entries().get(PREFIX + record.source.id)
    if not stored or not auth._matches(stored, record):
        raise PermissionError("Enter the access key for this S3 source again")
    return str(stored["access_key_id"]), str(stored["secret_access_key"])


def connected(auth: StorageAuth, record: SourceRecord) -> bool:
    """Usable access: always for anonymous and profile sources, else a bound key pair."""
    if record.configuration.s3_access != "keys":
        return record.connected
    with auth._lock:
        stored = auth._entries().get(PREFIX + record.source.id)
    return bool(stored and auth._matches(stored, record))


def forget(auth: StorageAuth, source_id: str) -> None:
    """Remove a source's key pair (called with the auth lock held)."""
    entries = auth._entries()
    if entries.pop(PREFIX + source_id, None) is not None:
        auth._vault.write_entries(entries)
