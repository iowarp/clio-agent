"""Transcript metadata projection for tool-transform provenance."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_schemas import TransformRecord


def tool_provenance_metadata(record: TransformRecord | None) -> dict[str, Any]:
    """Project declared out-of-workspace inputs and warnings into tool metadata.

    A contained but unregistered workspace file is represented as an external
    leaf in the lineage graph, but it is not a user-supplied external source.
    Only the explicit external-input contract carries the
    ``external_input_not_hashed`` marker and belongs in transcript metadata.
    """

    if record is None:
        return {}
    inputs = [
        {
            "name": edge.name,
            "locator": edge.path,
            "arg": edge.arg,
            "evidence": edge.evidence.value,
            "note": edge.note,
        }
        for edge in record.used
        if edge.external_ref and edge.note == "external_input_not_hashed"
    ]
    warnings = [
        note for note in record.notes if note.get("reason") == "external_input_contract_unknown"
    ]
    metadata: dict[str, Any] = {}
    if inputs:
        metadata["provenance_inputs"] = inputs
    if warnings:
        metadata["provenance_warnings"] = warnings
    return metadata
