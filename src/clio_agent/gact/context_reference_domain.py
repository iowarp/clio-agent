"""Shared contracts for structured workspace references."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo


class WorkspaceSession(Protocol):
    """Minimum session ownership surface required at reference admission."""

    @property
    def id(self) -> str: ...

    @property
    def workspace_id(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def message_count(self) -> int: ...

    @property
    def updated_at(self) -> str: ...


ContextReferenceKind = Literal[
    "workspace_file",
    "artifact",
    "session",
    "agent_run",
    "evidence_source",
    "context_frame",
    "diff",
    "plan",
]
ReferenceSearchKind = Literal[
    "workspace_file",
    "resource",
    "artifact",
    "session",
    "agent_run",
    "evidence_source",
    "context_frame",
    "diff",
    "plan",
]

CONTEXT_REFERENCE_KINDS: frozenset[str] = frozenset(
    {
        "workspace_file",
        "artifact",
        "session",
        "agent_run",
        "evidence_source",
        "context_frame",
        "diff",
        "plan",
    }
)
REFERENCE_SEARCH_KINDS: frozenset[str] = frozenset({*CONTEXT_REFERENCE_KINDS, "resource"})
CONTEXT_REFERENCE_CAPABILITY: dict[str, Any] = {
    "enabled": True,
    "version": "1",
    "part_type": "context_ref",
    "kinds": sorted(CONTEXT_REFERENCE_KINDS),
    "search_kinds": sorted(REFERENCE_SEARCH_KINDS),
    "search_route": "/v1/workspaces/{workspace_id}/references",
    "revision_pinned_kinds": [
        "workspace_file",
        "resource",
        "artifact",
        "evidence_source",
        "context_frame",
        "diff",
        "plan",
    ],
}


@dataclass(frozen=True)
class ContextReferenceError(Exception):
    """Typed failure raised while resolving a client-supplied reference."""

    status_code: int
    error: str
    message: str
    details: dict[str, Any]
    recoverable: bool = False

    def http_exception(self) -> Exception:
        """Project this domain failure to the server's typed HTTP envelope."""

        from fastapi import HTTPException  # noqa: PLC0415

        return HTTPException(
            status_code=self.status_code,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error=self.error,
                    message=self.message,
                    details=self.details,
                    recoverable=self.recoverable,
                )
            ).model_dump(exclude_none=True),
        )


@dataclass(frozen=True)
class BoundedFileSnapshot:
    """One bounded prefix plus the digest of the exact bytes read."""

    data: bytes
    sha256: str
    size_bytes: int
