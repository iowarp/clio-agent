"""Versioned wire and persistence models for CLIO-owned infrastructure."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from clio_agent.gact.infrastructure.server_parameters import ServerParameter

_NULLABLE_SSH_STRING_FIELDS = ("profile", "host", "user", "identity_file")

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
RuntimeName = Literal["docker", "podman", "apptainer"]
RuntimeReason = Literal["not_installed", "unusable", "not_probed"]
#: Why an installed runtime is ``unusable`` (see ``runtime_failure.py``). A
#: separate field, not new ``RuntimeReason`` values, so a client that predates
#: it still decodes the facts.
RuntimeFailure = Literal["not_running", "permission_denied", "timed_out", "unknown"]
ResourceKind = Literal["container", "image", "directory", "parent_directory", "instance_logs"]
EffectiveSource = Literal["server_report", "container_config", "launch_request", "engine_default"]

RUNTIME_LABELS: dict[RuntimeName, str] = {
    "docker": "Docker",
    "podman": "Podman",
    "apptainer": "Apptainer",
}


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

    @field_validator(*_NULLABLE_SSH_STRING_FIELDS, mode="before")
    @classmethod
    def coerce_absent_to_empty(cls, value: str | None) -> str:
        """Treat a missing/`null` optional string the same as "" (#1438).

        The desktop's Rust bridge serializes an unset `Option<String>` (for
        example a host with no key-file override) as JSON `null`, not an
        absent field. Format-only correction, no semantic change: `null` and
        `""` both already mean "not configured" everywhere this is read.
        """

        return "" if value is None else value

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

    @field_validator("install_root", mode="before")
    @classmethod
    def validate_install_root(cls, value: str | None) -> str:
        """Prevent an install root from becoming a shell-control channel.

        Runs in "before" mode so a `null` install_root (the same Rust
        `Option<String>::None` shape as `identity_file`, #1438 — a host left
        at "use the remote user's home directory") is treated as "" instead
        of failing request validation outright.
        """

        if value is None:
            return ""
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


class ContainerRuntimeFact(BaseModel):
    """What one container runtime looks like on a target, as probed."""

    name: RuntimeName
    installed: bool = False
    usable: bool = False
    version: str = ""
    reason: RuntimeReason | None = None
    #: Set only when ``reason`` is ``unusable``.
    failure: RuntimeFailure | None = None
    detail: str = ""
    #: Rootless Docker maps ``--user`` onto a sub-uid the user cannot delete.
    rootless: bool = False

    def explanation(self) -> str:
        """One sentence for the person: why this runtime can or cannot run services."""

        label = RUNTIME_LABELS[self.name]
        if self.usable:
            return f"{label} {self.version}".strip() + " is ready."
        if self.reason == "not_installed":
            return f"{label} is not installed."
        if self.reason == "not_probed":
            return f"{label} was not inspected on this target."
        if self.failure == "not_running":
            return f"{label} is installed but not running."
        if self.failure == "permission_denied":
            return f"{label} is installed but this account may not use it."
        if self.failure == "timed_out":
            return f"{label} is installed but did not respond."
        suffix = f": {self.detail}" if self.detail else "."
        return f"{label} is installed but cannot run containers{suffix}"


class TargetIdentity(BaseModel):
    """The numeric account a target runs commands as (for Docker ``--user``)."""

    uid: int = -1
    gid: int = -1

    @property
    def known(self) -> bool:
        """Whether both ids were probed."""

        return self.uid >= 0 and self.gid >= 0


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
    container_runtimes: list[ContainerRuntimeFact] = Field(default_factory=list)
    identity: TargetIdentity = Field(default_factory=TargetIdentity)
    home: str = ""
    #: The target's short hostname: per-host state (service directories on a
    #: home shared by many nodes, Apptainer instance logs) is namespaced by it.
    hostname: str = ""


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


class OwnedResource(BaseModel):
    """One thing a deployment created on its target and must remove on uninstall."""

    kind: ResourceKind
    ref: str
    runtime: RuntimeName | None = None
    created_at: str = Field(default_factory=utc_now)


class EffectiveParameter(BaseModel):
    """One server parameter as it is actually in force on a running server."""

    id: str
    label: str
    value: str
    source: EffectiveSource
    detail: str = ""


class ServiceAccess(BaseModel):
    """Who can use a running model server, in one plain sentence.

    Attributes:
        mode: ``api_key`` -- the server refuses requests without the key CLIO
            generated for this deployment (CLIO sends it; nobody copies it);
            ``shared`` -- the person chose to run it with no key;
            ``unprotected`` -- it runs with no key though nobody chose that
            (Ollama has no key support, or the server did not refuse a
            request without its key).
        detail: The plain sentence the UI shows.
        verified: CLIO checked the server refuses a request without the key.
    """

    mode: Literal["api_key", "shared", "unprotected"]
    detail: str
    verified: bool = False


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
    #: Tweakable server parameters (model runtimes), rendered verbatim by the UI.
    parameters: list[ServerParameter] = Field(default_factory=list)
    #: What the running server has in force, with where each value came from.
    effective_parameters: list[EffectiveParameter] = Field(default_factory=list)
    #: The installed configuration (the negotiated runtime and chosen parameters).
    configuration: dict[str, str] = Field(default_factory=dict)
    #: Everything this deployment created on its target (removed on uninstall).
    owned_resources: list[OwnedResource] = Field(default_factory=list)
    #: Whether this engine can be protected by an API key (Ollama cannot).
    supports_api_key: bool = False
    #: Who can use the installed deployment (absent when nothing is installed).
    access: ServiceAccess | None = None


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
    owned_resources: list[OwnedResource] = Field(default_factory=list)
    effective_parameters: list[EffectiveParameter] = Field(default_factory=list)
    #: Who can use it; the key itself lives only in the secret store.
    access: ServiceAccess | None = None
    #: Set only for `clio_agent`, only when a claim's `on_conflict: "connect"`
    #: adopted a healthy process under a root other than the owning
    #: infrastructure target's configured `install_root` (from the claim's
    #: own `owner`, i.e. that process's actual install prefix). Empty means
    #: "use the target's configured install_root" -- the common case.
    #: Lifecycle commands (stop/logs/uninstall/start) must act on this root
    #: when set, not the target's, or they miss the process they adopted.
    resolved_root: str = ""
    updated_at: str = Field(default_factory=utc_now)


class ServiceActionRequest(BaseModel):
    """One allowlisted lifecycle action requested from CLIO."""

    model_config = ConfigDict(extra="forbid")

    target_id: str = "local"
    action: Literal["install", "start", "status", "stop", "logs", "reinstall", "uninstall"]
    variant_id: str
    configuration: dict[str, str] = Field(default_factory=dict)


class DesktopExitRequest(BaseModel):
    """The single Desktop instance whose owned remote launches should stop."""

    desktop_id: str = Field(min_length=1, max_length=100)


class VersionConflictDetail(BaseModel):
    """A CLIO-looking process the claim step found but left running untouched.

    Set only when ``error`` is ``clio_deploy_version_conflict``: an
    ``install``/``reinstall``/``start`` on ``clio_agent`` found something on
    the conventional port that is not this exact install and version, and
    the request did not say how to proceed (``configuration`` carries no
    ``on_conflict``). Nothing was stopped or installed. ``health`` is
    ``healthy`` when it answered its own health check (``installed_version``
    is then a real version), or ``unresponsive``/``unknown`` when it did not
    answer or could not be asked -- never a reason to have stopped it. The
    caller re-issues the same action with ``configuration.on_conflict`` set
    to ``"connect"`` (adopt it as-is; only meaningful when ``healthy``) or
    ``"replace"`` (stop it and install this desktop's version).
    """

    model_config = ConfigDict(extra="forbid")

    installed_version: str
    pid: str
    health: Literal["healthy", "unresponsive", "unknown"] = "unknown"
    target_version: str = ""
    owner: str = ""
    port: int = 17800


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
    conflict: VersionConflictDetail | None = None
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
