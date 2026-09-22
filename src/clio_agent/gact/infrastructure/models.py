"""Versioned wire and persistence models for CLIO-owned infrastructure."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

TargetKind = Literal["local", "ssh", "direct"]
TransportState = Literal[
    "connected",
    "reconnecting",
    "reauthentication_required",
    "disconnected",
    "state_unknown",
]
ServiceState = Literal["running", "stopped", "not_installed", "unknown"]
OperationState = Literal["queued", "running", "succeeded", "failed", "cancelled"]
ConnectionStrategy = Literal["loopback", "direct", "ssh_forward", "external"]


def utc_now() -> str:
    """Return a stable UTC timestamp for persisted infrastructure records."""

    return datetime.now(timezone.utc).isoformat()


class SshRoute(BaseModel):
    """Non-secret OpenSSH route metadata owned by CLIO."""

    model_config = ConfigDict(extra="forbid")

    profile: str = ""
    host: str = ""
    user: str = ""
    port: int = Field(default=22, ge=1, le=65535)
    jump_hosts: list[str] = Field(default_factory=list)
    identity_file: str = ""
    platform: Literal["auto", "linux", "windows"] = "auto"

    @field_validator("profile", "host", "user", "identity_file")
    @classmethod
    def reject_control_characters(cls, value: str) -> str:
        """Reject values that cannot be safely represented in OpenSSH arguments."""

        if any(character in value for character in ("\0", "\r", "\n")):
            raise ValueError("SSH values cannot contain control characters")
        return value.strip()

    @field_validator("jump_hosts")
    @classmethod
    def validate_jump_hosts(cls, value: list[str]) -> list[str]:
        """Keep jump chains ordered while rejecting duplicates and control data."""

        cleaned = [item.strip() for item in value]
        if any(
            not item or any(character in item for character in ("\0", "\r", "\n"))
            for item in cleaned
        ):
            raise ValueError("Jump hosts must be non-empty OpenSSH profile names")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Jump hosts cannot contain duplicates")
        return cleaned


class InfrastructureTarget(BaseModel):
    """One execution target owned by this CLIO instance."""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    kind: TargetKind
    install_root: str = ""
    ssh: SshRoute | None = None
    transport_state: TransportState = "disconnected"
    auto_reconnect: bool = True
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


class CreateTargetRequest(BaseModel):
    """Validated request for a CLIO-owned target."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=160)
    kind: TargetKind
    install_root: str = ""
    ssh: SshRoute | None = None
    auto_reconnect: bool = True

    @field_validator("install_root")
    @classmethod
    def validate_install_root(cls, value: str) -> str:
        """Prevent an install root from becoming a shell-control channel."""

        if any(character in value for character in ("\0", "\r", "\n")):
            raise ValueError("Install location cannot contain control characters")
        cleaned = value.strip()
        if not cleaned:
            return ""
        if len(cleaned) > 1024:
            raise ValueError("Install location is too long")
        if cleaned.startswith("/"):
            posix_path = PurePosixPath(cleaned)
            if str(posix_path) == "/" or ".." in posix_path.parts:
                raise ValueError("Choose a dedicated absolute install directory")
            return cleaned
        windows_path = PureWindowsPath(cleaned)
        if (
            not windows_path.is_absolute()
            or str(windows_path) == windows_path.anchor
            or ".." in windows_path.parts
        ):
            raise ValueError("Choose a dedicated absolute install directory")
        return cleaned


class UpdateTargetRequest(CreateTargetRequest):
    """Replacement metadata for an existing non-local target."""


class TransportStateRequest(BaseModel):
    """Desktop-observed state for an interactive SSH transport."""

    model_config = ConfigDict(extra="forbid")

    state: TransportState


class TargetFacts(BaseModel):
    """Observed host capabilities, never inferred from a profile label."""

    target_id: str
    label: str
    os: str
    arch: str
    accelerator: str = "none"
    docker_available: bool = False
    docker_installed: bool = False
    uv_available: bool = False
    transport_state: TransportState = "disconnected"


class ServiceConfigurationField(BaseModel):
    """One driver-declared setup input."""

    id: str
    label: str
    placeholder: str = ""
    required: bool = False
    options: list[str] = Field(default_factory=list)


class ServiceVariant(BaseModel):
    """One pinned installation variant exposed by a service driver."""

    id: str
    label: str
    version: str
    install_type: str
    artifact: str
    compatible: bool
    reason: str = ""


class ManagedServiceDefinition(BaseModel):
    """Catalog projection for one CLIO-managed service."""

    id: str
    category: Literal["model_runtime", "scientific_service", "remote_access"]
    label: str
    description: str
    recommended_variant: str
    variants: list[ServiceVariant]
    configuration_fields: list[ServiceConfigurationField] = Field(default_factory=list)
    supports_stop: bool = True
    state: ServiceState = "unknown"
    connection_url: str | None = None
    connection_strategy: ConnectionStrategy | None = None


class ManagedServiceCatalog(BaseModel):
    """Facts and services for one target."""

    facts: TargetFacts
    services: list[ManagedServiceDefinition]


class ServiceRecord(BaseModel):
    """Durable desired and last-observed state for a managed service."""

    id: str
    service_id: str
    target_id: str
    variant_id: str
    configuration: dict[str, str] = Field(default_factory=dict)
    state: ServiceState = "unknown"
    connection_url: str | None = None
    connection_strategy: ConnectionStrategy | None = None
    updated_at: str = Field(default_factory=utc_now)


class ServiceActionRequest(BaseModel):
    """One allowlisted lifecycle action requested from CLIO."""

    model_config = ConfigDict(extra="forbid")

    target_id: str = "local"
    action: Literal["install", "start", "status", "stop", "logs", "reinstall", "uninstall"]
    variant_id: str
    configuration: dict[str, str] = Field(default_factory=dict)


class InfrastructureOperation(BaseModel):
    """Durable operation state returned immediately to callers."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    service_id: str
    target_id: str
    action: str
    state: OperationState = "queued"
    progress: str = "Queued"
    logs: str = ""
    error: str | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


class ExternalServiceConnectionRequest(BaseModel):
    """A reachable service endpoint whose lifecycle CLIO does not own."""

    model_config = ConfigDict(extra="forbid")

    service_id: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=160)
    url: HttpUrl
    credential_ref: str = ""


class ExternalServiceConnection(BaseModel):
    """Durable connection-only service record."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    service_id: str
    label: str
    url: str
    credential_ref: str = ""
    managed: Literal[False] = False
    reachable: bool | None = None
    checked_at: str | None = None
    created_at: str = Field(default_factory=utc_now)


class CommandSpec(BaseModel):
    """A server-generated command executed locally or over an attached transport."""

    model_config = ConfigDict(extra="forbid")

    program: str
    args: list[str] = Field(default_factory=list)
    scope: Literal["target", "controller"] = "target"
    timeout_seconds: float = Field(default=120.0, gt=0, le=1800)
    settle_seconds: float = Field(default=0.0, ge=0, le=30)
    stdin: str = ""
    allowed_exit_codes: list[int] = Field(default_factory=lambda: [0])

    @field_validator("program", "stdin")
    @classmethod
    def reject_nul(cls, value: str) -> str:
        """Reject NUL because process and transport boundaries cannot represent it."""

        if "\0" in value:
            raise ValueError("Command fields cannot contain NUL")
        return value


class CommandResult(BaseModel):
    """Bounded result returned by a local or Desktop SSH executor."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
