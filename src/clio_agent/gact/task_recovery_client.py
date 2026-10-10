"""MCP reconnect client, imported only when a persisted HTTP task needs recovery."""

from __future__ import annotations

from typing import Any

from clio_agent.tools.task_receipt import TaskAwareClient


class ReconnectingClient(TaskAwareClient):
    """Adopt the original task's negotiation without submitting initialize again."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **{**kwargs, "auto_initialize": False})
