"""Long-term memory routes under ``/v1/memory``: the memory page's API and transfer."""

from __future__ import annotations

from fastapi import FastAPI

from clio_agent.gact.routes.memory_documents import register_memory_document_routes
from clio_agent.gact.routes.memory_switches import register_memory_switch_routes
from clio_agent.gact.routes.memory_transfer import register_memory_transfer_routes


def register_memory_routes(app: FastAPI) -> None:
    """Documents, facts, history, search, the inbox, export, import and the switches."""
    register_memory_transfer_routes(app)  # /v1/memory/export|import before the doc catch-alls
    register_memory_document_routes(app)
    register_memory_switch_routes(app)


__all__ = ["register_memory_routes"]
