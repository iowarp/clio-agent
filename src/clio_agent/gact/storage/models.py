"""Private storage control records; public source identities live in clio-schemas."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from clio_schemas.connected_resources import AccessMode, ConnectedSource
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


def now() -> str:
    """Return a UTC operation timestamp."""
    return datetime.now(timezone.utc).isoformat()


class StorageModel(BaseModel):
    """Reject unknown fields at the trusted storage boundary."""

    model_config = ConfigDict(extra="forbid")


class SourceConfiguration(StorageModel):
    """Nonsecret connection settings; credentials are held by the auth owner."""

    ssh_profile: str = ""
    target_id: str = ""
    ssh_origin: Literal["desktop", "clio"] = "desktop"
    ssh_authentication: Literal["configured", "key", "password"] = "configured"
    collection_id: str = ""
    destination_collection_id: str = ""
    destination_collection_root: str = ""
    destination_local_root: str = ""
    github_ref: str = ""


class SftpCredentials(StorageModel):
    """Trusted sign-in input, excluded from public serialization and diagnostic repr."""

    password: SecretStr | None = Field(default=None, exclude=True, repr=False)
    private_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    passphrase: SecretStr | None = Field(default=None, exclude=True, repr=False)


class CreateSource(StorageModel):
    """Approve one source on the connected CLIO for one workspace."""

    provider: Literal["local", "sftp", "google_drive", "globus", "github"]
    root: str = Field(min_length=1, max_length=4096)
    label: str = Field(min_length=1, max_length=120)
    mode: AccessMode = "read_only"
    configuration: SourceConfiguration = Field(default_factory=SourceConfiguration)
    sftp_credentials: SftpCredentials | None = Field(default=None, exclude=True, repr=False)


class SourceRecord(StorageModel):
    """Durable source, owner-bound settings and its last materialized manifest."""

    source: ConnectedSource
    configuration: SourceConfiguration = Field(default_factory=SourceConfiguration)
    principal: str
    connected: bool = True
    removed: bool = False
    manifest_id: str | None = None
    workspace_root: str = ""
    owns_write_grant: bool = False
    origin: Literal["provider", "desktop_upload"] = "provider"
    target_route: str = ""
    linked_manifest_id: str | None = None
    sign_in_required: bool = False
    link_access: Literal["read_only", "publish_later", "write_through"] | None = None
    download_access: Literal["read_only", "editable"] | None = None

    @property
    def linked_access(self) -> Literal["read_only", "publish_later", "write_through"]:
        """Resolve legacy connections without coupling new link and download choices."""
        if self.link_access is not None:
            return self.link_access
        if self.source.mode == "working_copy":
            return "publish_later"
        return "write_through" if self.source.mode == "write_enabled" else "read_only"

    @property
    def download_read_only(self) -> bool:
        """Downloads carry their own local permission, independent of their remote link."""
        return (
            self.download_access == "read_only"
            if self.download_access
            else self.source.mode == "read_only"
        )


class LinkedEdit(StorageModel):
    """Durable local edit and the original bytes it was based on."""

    before_hash: str | None
    after_hash: str | None
    content_path: str | None
    before_path: str | None = None


class LinkedEdits(StorageModel):
    """A restart-safe publication journal, separate from detached downloads."""

    source_id: str
    baseline_id: str
    changes: dict[str, LinkedEdit] = Field(default_factory=dict)


class FileEntry(StorageModel):
    """A relative source name and a provider revision, never a credential URL."""

    path: str
    kind: Literal["file", "directory"]
    size: int = Field(default=0, ge=0)
    revision: str = ""
    sha256: str | None = None
    locator: str = ""

    @field_validator("path")
    @classmethod
    def safe_relative(cls, value: str) -> str:
        """Reject traversal and cross-platform path aliases before materialization."""
        from pathlib import PurePosixPath, PureWindowsPath

        posix = PurePosixPath(value)
        windows = PureWindowsPath(value)
        if (
            not value
            or value in {".", ".."}
            or posix.is_absolute()
            or windows.drive
            or "\\" in value
            or ".." in posix.parts
            or any(ord(char) < 32 for char in value)
            or any(part.endswith((" ", ".")) for part in posix.parts)
            or any(char in value for char in '<>:"|?*')
        ):
            raise ValueError("Source entry has an unsafe relative path")
        # Reject Windows device names on every host, so a later desktop copy is safe.
        reserved = {"CON", "PRN", "AUX", "NUL"} | {
            f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
        }
        if any(part.split(".", 1)[0].upper() in reserved for part in posix.parts):
            raise ValueError("Source entry has a reserved filename")
        return value


class Manifest(StorageModel):
    """Immutable baseline revision and hashes of the bytes actually copied."""

    id: str
    source_id: str
    revision: str
    created_at: str = Field(default_factory=now)
    entries: list[FileEntry]
    hashes: dict[str, str]


class TransferOperation(StorageModel):
    """Restart-visible transfer state and native provider job identity."""

    id: str
    source_id: str
    kind: Literal["materialize", "refresh", "apply", "native_transfer", "indexing"]
    state: Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"] = (
        "queued"
    )
    bytes_done: int = 0
    bytes_total: int = 0
    native_job_id: str | None = None
    native_request: dict[str, object] | None = None
    selected_paths: list[str] | None = None
    cancel_requested: bool = False
    error: str | None = None
    applied_paths: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    task_handle: str = ""
    owner_session_id: str = ""
    description: str = ""
    invocation_id: str = ""
    entries_done: int = 0
    manifest_id: str | None = None
    index_signature: str = ""


class ReviewedChange(StorageModel):
    """One concrete update with the exact local bytes and upstream revision reviewed."""

    path: str
    kind: Literal["add", "modify", "delete"]
    local_hash: str | None = None
    baseline_revision: str | None = None
    upstream_revision: str | None = None
    conflict: bool = False
    preview: str = ""
    preview_note: str = ""


class ChangeReview(StorageModel):
    """A saved review cannot authorize changes made after the review was shown."""

    id: str
    source_id: str
    manifest_id: str
    created_at: str = Field(default_factory=now)
    changes: list[ReviewedChange]
