"""One source-selection owner for the NDP picker and agent connections."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import Field

from clio_agent.gact.storage.models import (
    CreateSource,
    SourceConfiguration,
    SourceRecord,
    StorageModel,
)
from clio_agent.gact.storage.ndp_catalog import NDPDataset, NDPSelection

if TYPE_CHECKING:
    from clio_agent.gact.storage.service import StorageService


class NDPConnectionSelection(StorageModel):
    """Public catalog identities selected for one approved NDP connection."""

    endpoint_id: str = Field(min_length=1, max_length=120)
    resource_ids: list[str] = Field(min_length=1, max_length=1000)


def selection_summary(service: StorageService, record: SourceRecord) -> dict[str, object] | None:
    """Describe the approved subset without revealing resource URLs or credentials."""
    if record.source.provider != "ndp" or not record.configuration.ndp_selection_id:
        return None
    try:
        selection = service.store.get(
            "ndp-selection", record.configuration.ndp_selection_id, NDPSelection
        )
    except KeyError:
        return None
    return {
        "dataset_id": selection.dataset.id,
        "catalog_id": selection.dataset.endpoint.id,
        "resources": [
            {"id": item.id, "name": item.name, "path": item.path, "format": item.format}
            for item in selection.dataset.resources
        ],
    }


def existing_selection(
    service: StorageService, workspace_id: str, dataset: NDPDataset
) -> SourceRecord | None:
    """Reuse an owner-scoped connection with exactly the approved resource set."""
    for record in service.sources(workspace_id):
        if (
            record.connected
            and record.source.provider == "ndp"
            and record.configuration.ndp_selection_id
            and record.principal == service.principal
            and record.source.owner.clio_id == service.store.clio_id
        ):
            prior = service.store.get(
                "ndp-selection", record.configuration.ndp_selection_id, NDPSelection
            )
            if (
                prior.dataset.endpoint.same_destination(dataset.endpoint)
                and prior.dataset.id == dataset.id
                and {item.id for item in prior.dataset.resources}
                == {item.id for item in dataset.resources}
            ):
                return record
    return None


#: SourceRecord.label's limit; catalog titles (cruise and campaign records) run longer.
MAX_LABEL = 120


def source_label(title: str) -> str:
    """A source label within the limit: the title, shortened at a word with an ellipsis.

    Format only: the selection keeps the dataset's full title. Before this, any
    dataset titled over 120 characters could not be attached at all.
    """
    text = " ".join(title.split()) or "NDP dataset"
    if len(text) <= MAX_LABEL:
        return text
    cut = text[: MAX_LABEL - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return (cut or text[: MAX_LABEL - 1]) + "\u2026"


def select_source(
    service: StorageService, workspace_id: str, dataset: NDPDataset, workspace_root: Path
) -> SourceRecord:
    """Persist selected metadata after the caller's UI or tool approval, without linking."""
    from clio_agent.gact.storage.organization_branches import note_organization  # noqa: PLC0415

    note_organization(dataset.organization)
    with service.ndp_selection_lock:
        existing = existing_selection(service, workspace_id, dataset)
        if existing is not None:
            return existing
        selection = NDPSelection(id="ndp_" + uuid4().hex, dataset=dataset)
        service.store.put("ndp-selection", selection.id, selection)
        return service.create(
            workspace_id,
            CreateSource(
                provider="ndp",
                root=dataset.id,
                label=source_label(dataset.title),
                mode="read_only",
                configuration=SourceConfiguration(ndp_selection_id=selection.id),
            ),
            workspace_root,
        )
