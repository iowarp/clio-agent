"""Small visual-capture input derived from the actual recorded transcript."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI


def review_summary(app: FastAPI, transcript: dict[str, Any]) -> dict[str, Any]:
    """Project saved A2UI parts using the production validator, without reading ARC again."""
    from clio_agent.gact.a2ui import project_a2ui_parts
    from clio_agent.gact.a2ui_catalogs.activation import session_catalog_resolver
    from clio_agent.gact.types import Part

    def summary(row: dict[str, Any]) -> dict[str, Any]:
        parts = [
            Part.model_validate(
                {**part, "metadata": {"message_id": message["id"], **(part.get("metadata") or {})}}
            )
            for message in row["messages"]
            for part in message["parts"]
            if part["type"] == "a2ui"
        ]
        stamped: list[tuple[str, int, Any]] = []
        carried = ""
        seen: set[str] = set()
        for part in parts:
            if part.id in seen:
                continue
            seen.add(part.id)
            carried = str((part.metadata or {}).get("recorded_at") or carried)
            stamped.append((carried, len(stamped), part))
        ordered = [part for _, _, part in sorted(stamped, key=lambda item: (item[0], item[1]))]
        surfaces, degradations = (
            project_a2ui_parts(
                ordered,
                row["session"]["id"],
                catalogs=session_catalog_resolver(app, row["session"]["id"]),
            )
            if ordered
            else ({}, [])
        )
        return {
            "session": row["session"],
            "surfaces": [surface.to_wire() for surface in surfaces.values()],
            "degradations": degradations,
            "messages": [
                {
                    "id": message["id"],
                    "parts": [
                        {key: part[key] for key in ("type", "text", "url") if key in part}
                        for part in message["parts"]
                        if part["type"] in {"text", "image"}
                    ],
                }
                for message in row["messages"]
            ],
        }

    return {**summary(transcript), "children": [summary(row) for row in transcript["children"]]}
