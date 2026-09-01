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
    """Bounded operational state exposed by the provider-neutral API.

    ``flush_durable``/``flush_note`` are recorded ONCE, at worker
    construction (:class:`clio_agent.gact.provenance.dispatcher._ProviderWorker`),
    from the provider's own ``flush_durable``/``flush_note`` instance
    attributes (default ``True``/``""`` when a provider does not set them —
    the honest default for a provider whose ``emit`` is already
    synchronous). They state whether a RETURNING ``flush()`` call is a real
    synchronous-persistence barrier for this provider, surfaced verbatim at
    ``GET /v1/provenance/providers`` so a caller relying on ``flush()`` can
    discover a residual race (e.g. Flowcept's buffered delivery) instead of
    assuming one never exists.
    """

    name: str
    configured: bool = True
    queryable: bool = False
    durable: bool = False
    status: str = "ready"
    queue_depth: int = 0
    #: Events handed to this provider's queue. Counted at SUBMIT time, so this
    #: is the hand-off number and nothing more.
    queued: int = 0
    #: Events the provider CONFIRMED it wrote -- its ``emit`` returned without
    #: raising and did not report the event filtered. Deliberately not counted
    #: at submit time: a queue-time counter can never disagree with reality, so
    #: it reported 26 accepted / 0 failed for a CMF lane whose store had
    #: received nothing for half of them (live qualification, sess_3c2660f69bd5).
    #: ``queued - (accepted + filtered + failed)`` is what is still in flight.
    accepted: int = 0
    filtered: int = 0
    overflow: int = 0
    failed: int = 0
    last_error: str = ""
    flush_durable: bool = True
    flush_note: str = ""

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

    def flush(self) -> None:
        """Block until every already-accepted event is genuinely persisted —
        or, when that cannot be promised, return anyway and say so.

        REQUIRED (not duck-typed): :meth:`ProvenanceDispatcher.flush` calls
        this unconditionally (contained per-provider, mirroring how
        :class:`_ProviderWorker` already contains a raising ``emit``), so
        every provider states its own position rather than silently opting
        out. Three honest shapes:

        1. ``emit`` is already synchronous — ``flush()`` returns immediately
           and that IS a complete barrier (e.g. the native/CMF artifact
           providers, whose ``emit`` already blocks on a request/response
           protocol with the downstream store).
        2. ``emit`` hands off to the provider's OWN async writer — ``flush()``
           must proxy through to a real drain (e.g. ``JsonlProvenanceProvider``,
           which hands off to ``FileSemanticTraceBackend``'s shared writer
           thread).
        3. No drain hook exists at all (e.g. Flowcept's buffered MQ/DB
           delivery, or an arbitrary ``CLIO_SEMANTIC_TRACE_FACTORY`` backend
           with no ``flush``) — ``flush()`` returns immediately WITHOUT
           faking a barrier, and the provider sets the optional
           ``flush_durable = False`` / ``flush_note`` instance attributes so
           the gap is discoverable via :class:`ProviderHealth` rather than
           silently assumed away.
        """

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
