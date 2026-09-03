"""Unified same-workspace reference discovery route."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, Query

from clio_agent.gact.context_reference_domain import ContextReferenceError
from clio_agent.gact.context_reference_search import search_workspace_references


def register_reference_routes(app: FastAPI) -> None:
    """Register the structured context-reference discovery endpoint."""

    @app.get("/v1/workspaces/{wid}/references")
    async def list_workspace_references(
        wid: str,
        q: str = "",
        kinds: Annotated[list[str] | None, Query()] = None,
    ) -> dict[str, Any]:
        """Search files, resources, artifacts, sessions, and agent runs."""

        selected: list[str] | None = None
        if kinds:
            selected = []
            for value in kinds:
                selected.extend(kind.strip() for kind in value.split(",") if kind.strip())
        try:
            results = await search_workspace_references(
                app,
                wid,
                query=q,
                kinds=selected,
            )
        except ContextReferenceError as exc:
            raise exc.http_exception() from exc
        # Same contract the A2UI surface listing keeps: a repository that could not
        # be read, or a row deliberately hidden, rides the response so a short list
        # is never mistaken for an empty workspace.
        return {
            "references": results,
            "degradations": list(getattr(app.state, "reference_search_degradations", []) or []),
        }


__all__ = ["register_reference_routes"]
