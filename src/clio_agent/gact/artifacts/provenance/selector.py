"""Selection and bounded delivery of the artifact substream."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from clio_agent.gact.provenance.dispatcher import ProvenanceDispatcher

if TYPE_CHECKING:
    from clio_agent.gact.artifacts.provenance.protocol import (
        ArtifactProvenanceProvider,
        ArtifactStore,
    )
    from clio_agent.gact.semantic_events import SemanticEvent


DEFAULT_ARTIFACT_EVENTS = frozenset(
    {
        "artifact.created",
        "artifact.version.added",
        "artifact.alias.moved",
        "artifact.used",
        "artifact.transform.recorded",
        "artifact.enriched",
    }
)


class ArtifactProvenanceDispatcher:
    """One selected artifact provider fed by an extensible event selector."""

    name = "artifact-provenance"

    def __init__(
        self,
        provider: "ArtifactProvenanceProvider",
        *,
        include_events: frozenset[str] = DEFAULT_ARTIFACT_EVENTS,
        queue_size: int = 4096,
    ) -> None:
        self.provider = provider
        self.store: ArtifactStore = provider.store
        self.include_events = include_events
        self._dispatcher = ProvenanceDispatcher([provider], queue_size=queue_size)

    @property
    def provider_name(self) -> str:
        """Return the selected artifact provider name."""
        return self.provider.name

    def emit(self, event: "SemanticEvent") -> None:
        """Offer only selected events; the parent agentic stream remains unchanged."""
        if event.event_type in self.include_events:
            self._dispatcher.emit(event)

    def flush(self) -> None:
        """Wait until every accepted artifact event has reached the provider."""
        self._dispatcher.flush()

    def close(self) -> None:
        """Drain and close the selected artifact provider."""
        self._dispatcher.close()

    def health(self) -> dict[str, Any]:
        """Return the selected provider's bounded operational state."""
        rows = self._dispatcher.health()
        return rows[0] if rows else {"name": self.provider.name, "status": "unavailable"}

    def lineage(
        self,
        artifact_id: str,
        *,
        direction: str,
        depth: int,
        complete: bool = False,
    ) -> dict[str, Any] | None:
        """Flush writes and query the selected provider's normalized graph."""
        self.flush()
        return self.provider.lineage(
            artifact_id,
            direction=direction,
            depth=depth,
            complete=complete,
        )


__all__ = ["ArtifactProvenanceDispatcher", "DEFAULT_ARTIFACT_EVENTS"]
