"""Resolve Globus receiving storage on the connected CLIO, independently of sources."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import UUID

import globus_sdk
from pydantic import Field, field_validator

from clio_agent.gact.storage.models import StorageModel
from clio_agent.gact.storage.store import SourceStore


class GlobusDestination(StorageModel):
    """One receiving collection mapping owned by this CLIO host."""

    collection_id: str
    collection_root: str = Field(min_length=1, max_length=4096)
    local_root: str = Field(min_length=1, max_length=4096)

    @field_validator("collection_id")
    @classmethod
    def collection_uuid(cls, value: str) -> str:
        """Keep the receiving collection identity unambiguous."""
        return str(UUID(value))

    @field_validator("collection_root")
    @classmethod
    def absolute_collection_root(cls, value: str) -> str:
        """Reject collection-relative paths and traversal."""
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts or "\\" in value:
            raise ValueError("The collection folder must be an absolute Globus path")
        return str(path)

    def validate_storage(self, storage_root: Path) -> None:
        """Require the real namespaced staging directory to be inside this host mapping."""
        local = Path(self.local_root)
        if not local.is_absolute() or not local.is_dir():
            raise ValueError("Choose an existing receiving folder on this CLIO host")
        if not storage_root.resolve().is_relative_to(local.resolve()):
            raise ValueError("The receiving folder must contain this CLIO's source storage")


def personal_collection_path(path: str, *, windows: bool) -> str:
    """Translate GCP's documented Windows drive syntax; POSIX paths retain their identity."""
    if not windows:
        return str(PurePosixPath(path))
    parsed = PureWindowsPath(path)
    if not parsed.is_absolute() or len(parsed.drive) != 2 or parsed.drive[1] != ":":
        raise ValueError("Globus Connect Personal requires a mapped drive for this storage path")
    return "/" + parsed.drive[0].lower() + "/" + "/".join(parsed.parts[1:])


def receiving_destination(store: SourceStore) -> tuple[GlobusDestination | None, str]:
    """Use the host setting or discover its native GCP installation without changing access."""
    try:
        configured = store.get("host-config", "globus-destination", GlobusDestination)
    except KeyError:
        configured = None
    if configured:
        configured.validate_storage(store.root)
        return configured, "configured"
    # SDK discovery is on the Agent machine, never the browser/Desktop machine.
    personal = globus_sdk.LocalGlobusConnectPersonal()
    identifier = personal.endpoint_id
    if not identifier:
        return None, "unavailable"
    destination = GlobusDestination(
        collection_id=identifier,
        local_root=str(store.root),
        collection_root=personal_collection_path(str(store.root), windows=os.name == "nt"),
    )
    destination.validate_storage(store.root)
    return destination, "detected"


def destination_status(store: SourceStore) -> dict[str, object]:
    """Expose host setup status without requiring sign-in or claiming live verification."""
    destination, origin = receiving_destination(store)
    return {
        "destination": destination.model_dump() if destination else None,
        "origin": origin,
        "storage_root": str(store.root),
    }
