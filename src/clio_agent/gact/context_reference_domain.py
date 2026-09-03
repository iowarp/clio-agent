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

#: Kinds delivered to the model as a bounded SUMMARY rather than file bytes. Four
#: of them (evidence_source / context_frame / diff / plan) are snapshots of state
#: that moves inside the very turn that referenced it, which is why delivery falls
#: back to the snapshot recorded at admission.
SUMMARY_REFERENCE_KINDS: frozenset[str] = frozenset(
    {"session", "agent_run", "evidence_source", "context_frame", "diff", "plan"}
)
#: Which message part each SEARCHABLE kind becomes once attached. Eight kinds ride
#: the ``context_ref`` part; a picked ``resource`` is admitted as the ``resource_ref``
#: part the composer already delivers (its own custody record, revision check and
#: per-model delivery planning) rather than as a second, parallel mechanism.
REFERENCE_PART_TYPE_BY_KIND: dict[str, str] = {
    **dict.fromkeys(sorted(CONTEXT_REFERENCE_KINDS), "context_ref"),
    "resource": "resource_ref",
}

#: Names the picker offers that are NOT reference kinds, and the mechanism that
#: actually serves them. Documented rather than invented: choosing which agent runs
#: a turn is an existing field on the message request, so adding an ``agents``
#: reference kind would be a second way to say the same thing.
REFERENCE_ALTERNATE_MECHANISMS: dict[str, dict[str, str]] = {
    "agents": {
        "mechanism": "message_request_field",
        "field": "agent",
        "route": "POST /v1/sessions/{session_id}/messages",
        "detail": (
            "Selecting the agent for a turn is the request's own agent field; it is "
            "not an attachable reference and has no context_ref kind."
        ),
    },
}

CONTEXT_REFERENCE_CAPABILITY: dict[str, Any] = {
    "enabled": True,
    "version": "1",
    "part_type": "context_ref",
    "kinds": sorted(CONTEXT_REFERENCE_KINDS),
    "search_kinds": sorted(REFERENCE_SEARCH_KINDS),
    "search_route": "/v1/workspaces/{workspace_id}/references",
    "part_type_by_kind": dict(REFERENCE_PART_TYPE_BY_KIND),
    "alternate_mechanisms": {
        name: dict(detail) for name, detail in REFERENCE_ALTERNATE_MECHANISMS.items()
    },
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
