"""Workspace-owned NDP API connections and bounded operation inputs."""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from pydantic import Field, model_validator

from clio_agent.gact.infrastructure.models import SshRoute
from clio_agent.gact.storage.models import StorageModel
from clio_agent.gact.storage.ndp_catalog import NDPEndpoint
from clio_agent.gact.storage.ndp_streaming_models import StreamingRuntime


class EndpointConnectionInput(StorageModel):
    """Connect to an existing Endpoint or a published service, without installing it."""

    name: str = Field(min_length=1, max_length=120)
    url: str = Field(max_length=4096)
    ssh_profile: str = ""
    ssh_route: SshRoute | None = None
    authentication: Literal["none", "ndp"] = "none"
    streaming_runtime: StreamingRuntime | None = None

    @model_validator(mode="after")
    def check_destination(self) -> EndpointConnectionInput:
        """Bind authentication to a reviewed destination and SSH to a trusted local port."""
        self.url = NDPEndpoint.validate_url(self.url)
        parsed = urlsplit(self.url)
        if self.ssh_profile and self.ssh_route is not None:
            raise ValueError("Choose one SSH route, not both a route and a legacy profile")
        if self.ssh_route is not None and not (self.ssh_route.profile or self.ssh_route.host):
            raise ValueError("Choose an SSH destination")
        if self.ssh_profile or self.ssh_route is not None:
            if self.ssh_profile and not re.fullmatch(
                r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,159}", self.ssh_profile
            ):
                raise ValueError("Choose an existing SSH profile on the connected CLIO")
            if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("For SSH, use the Endpoint's remote loopback HTTP URL")
        elif self.authentication == "ndp" and parsed.scheme != "https":
            raise ValueError("NDP authentication requires HTTPS or a verified SSH connection")
        if any(part in {".", ".."} for part in unquote(parsed.path).split("/")):
            raise ValueError("Use an Endpoint base URL without path traversal")
        return self


class EndpointConnection(EndpointConnectionInput):
    """Persist an approved destination with its workspace and API contract."""

    id: str
    workspace_id: str
    principal: str
    schema_document: dict[str, Any]
    observed_at: str
    hidden: bool = False


class EndpointConnectionUpdate(StorageModel):
    """Edit a saved destination or its visibility without discarding captured data."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    url: str | None = Field(default=None, max_length=4096)
    ssh_profile: str | None = None
    ssh_route: SshRoute | None = None
    authentication: Literal["none", "ndp"] | None = None
    hidden: bool | None = None
    streaming_runtime: StreamingRuntime | None = None


class EndpointRequest(StorageModel):
    """Invoke one discovered operation with explicit finite capture limits."""

    operation_id: str = Field(min_length=1, max_length=500)
    parameters: dict[str, Any] = Field(default_factory=dict)
    body: dict[str, Any] | list[Any] | None = None
    max_seconds: float = Field(default=30, ge=1, le=30)
    max_events: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def private_authentication(self) -> EndpointRequest:
        """Keep credentials on the private account path rather than request/audit bodies."""
        pending: list[Any] = [self.parameters, self.body]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                for key, item in value.items():
                    normalized = key.casefold().replace("-", "_")
                    if item and (
                        normalized
                        in {"password", "token", "secret", "authorization", "api_key", "access_key"}
                        or normalized.endswith(("_password", "_token", "_secret"))
                    ):
                        raise ValueError(
                            "Use private connection authentication instead of credentials in operation inputs"
                        )
                    pending.append(item)
            elif isinstance(value, list):
                pending.extend(value)
        return self


class EndpointContinuation(StorageModel):
    """Control metadata observed in a complete, retained stream response."""

    stream_id: str = Field(min_length=1, max_length=160)
    next_cursor: str = Field(min_length=1, max_length=4096)
    count: int = Field(ge=0)
    next_positions: dict[str, int]


class EndpointCapture(StorageModel):
    """A durable receipt for actual bytes, distinct from catalog metadata."""

    id: str
    endpoint_id: str
    workspace_id: str
    operation_id: str
    operation_summary: str = ""
    method: str
    parameters: dict[str, Any]
    body: dict[str, Any] | list[Any] | None = None
    started_at: str
    completed_at: str
    status_code: int
    content_type: str
    bytes_received: int
    stop_reason: str
    resource_id: str
    sha256: str
    events: int = 0
    workspace_path: str = ""
    source_artifact_id: str = ""
    endpoint_name: str = ""
    endpoint_url: str = ""
    ssh_profile: str = ""
    ssh_route: SshRoute | None = None
    continuation: EndpointContinuation | None = None
