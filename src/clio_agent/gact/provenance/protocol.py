"""Contracts shared by downstream provenance providers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from clio_agent.gact.semantic_events import SemanticEvent


class ProviderReceipt(str, Enum):
    """Immediate dispatcher outcome for one provider delivery."""

    ACCEPTED = "accepted"
    FILTERED = "filtered"
    OVERFLOW = "overflow"
    FAILED = "failed"


@dataclass
class ProviderHealth:
    """Bounded operational state exposed by the provider-neutral API."""

    name: str
    configured: bool = True
    queryable: bool = False
    durable: bool = False
    status: str = "ready"
    queue_depth: int = 0
    accepted: int = 0
    filtered: int = 0
    overflow: int = 0
    failed: int = 0
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the stable wire representation."""
        return asdict(self)


@runtime_checkable
class ProvenanceProvider(Protocol):
    """Synchronous provider called by its dedicated dispatcher worker."""

    name: str
    durable: bool
    queryable: bool

    def emit(self, event: "SemanticEvent") -> ProviderReceipt | None:
        """Publish one event without changing CLIO turn semantics."""

    def close(self) -> None:
        """Drain and close provider-owned resources."""


@runtime_checkable
class ExecutionProvenanceReader(Protocol):
    """Optional provider capability for normalized execution queries."""

    name: str

    def query_execution(
        self,
        *,
        session_id: str,
        child_session_ids: list[str],
        limit: int,
    ) -> dict[str, Any]:
        """Return one normalized execution-provenance snapshot."""
