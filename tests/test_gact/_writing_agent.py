"""A host fake whose turn makes real file tool calls through the live tool runtime.

The calls run through the gateway executor inside the turn, so they meet the
session's permission gate, PreToolUse hooks and tool observer exactly as a model's
calls do, and each successful write lands on the change ledger and the transcript.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from clio_agent.tools.execution import create_sync_tool_executor
from clio_agent.tools.gateway import get_gateway


@dataclass
class _Pred:
    answer: str = "done"
    selected_expert: str = "data_expert"
    routing_rationale: str = ""


class WritingAgent:
    """A host fake whose turn runs real file tool calls through the live tool runtime."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.results: list[Any] = []

    def forward(self, question: str, session_id: str) -> _Pred:
        executor = create_sync_tool_executor(get_gateway(), timeout=10.0)
        try:
            for name, args in self.calls:
                try:
                    self.results.append(executor.call_tool(name, args))
                except Exception as exc:  # noqa: BLE001 - the test inspects the refusal
                    self.results.append(exc)
        finally:
            executor.close()
        return _Pred()


def workspace_session(client: TestClient, root: Path, *, approval_mode: str) -> str:
    wid = client.post("/v1/workspaces", json={"name": "w", "root_path": str(root)}).json()["id"]
    sid = client.post("/v1/sessions", json={"title": "t", "workspace_id": wid}).json()["id"]
    client.app.state.sessions.update(sid, approval_mode=approval_mode)
    return sid
