"""A view's call reaches the server marked as the view's (FP-D7); the agent's is not."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastmcp import FastMCP

from clio_agent.arc.task_types import CoreReference, argument_digest
from clio_agent.tools import call_origin
from clio_agent.tools.task_client import AcceptedNativeCall, TaskAwareClient, TaskSubmission


def _submission(name: str, arguments: dict[str, Any]) -> TaskSubmission:
    return TaskSubmission(
        handle="task_" + uuid4().hex,
        tool_name=name,
        context=CoreReference(session_id="t", scope="t", content_id="t", sha256="t"),
        tool_version=None,
        output_schema=None,
        argument_digest=argument_digest(arguments),
    )


def test_with_origin_marks_only_inside_a_scope() -> None:
    assert call_origin.with_origin({"a": 1}) == {"a": 1}
    with call_origin.scope("view"):
        assert call_origin.current() == "view"
        assert call_origin.with_origin(None) == {call_origin.ORIGIN_META_KEY: "view"}
    assert call_origin.current() is None


@pytest.mark.asyncio
async def test_the_product_client_sends_the_mark_in_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastmcp.client.mixins.tools import ClientToolsMixin

    sent: list[dict[str, Any] | None] = []

    async def sdk_call(self: Any, name: str, arguments: Any, *args: Any) -> Any:
        sent.append(args[2] if len(args) > 2 else None)
        return None

    async def adopt(call: AcceptedNativeCall) -> Any:
        pytest.fail("No task should be accepted")

    monkeypatch.setattr(ClientToolsMixin, "call_tool_mcp", sdk_call)
    client = TaskAwareClient(FastMCP("origin"), on_task_accepted=adopt)
    with client.submission(_submission("work", {})):
        await client.call_tool_mcp("work", {})
        with call_origin.scope("view"):
            await client.call_tool_mcp("work", {})
    await client.close()
    assert sent == [None, {call_origin.ORIGIN_META_KEY: "view"}]
