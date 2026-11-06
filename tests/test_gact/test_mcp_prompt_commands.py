"""An MCP server's prompts are ``/mcp__<server>__<prompt>`` commands over the agent's connection."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.prompts import Message
from mcp.types import EmbeddedResource, ImageContent, TextResourceContents

from clio_agent.gact import mcp_prompt_commands as prompts
from clio_agent.tools import mcp_prompts

PNG = base64.b64encode(b"\x89PNG fixture").decode()


def _server() -> FastMCP:
    server = FastMCP("fixture-docs")

    @server.prompt()
    def define_term(term: str) -> str:
        """Define one glossary term."""
        return f"Define the term '{term}'."

    @server.prompt()
    def show_figure(caption: str = "Fixture figure") -> list[Message]:
        """Show the fixture figure."""
        return [
            Message(f"Describe this figure. Caption: {caption}"),
            Message(
                EmbeddedResource(
                    type="resource",
                    resource=TextResourceContents(
                        uri="fixture://notes/figure-data", mimeType="text/plain", text="two pixels"
                    ),
                )
            ),
            Message(ImageContent(type="image", data=PNG, mimeType="image/png")),
            Message("It is a fixture.", role="assistant"),
        ]

    return server


def test_words_map_onto_the_declared_arguments_in_order() -> None:
    arguments = [
        {"name": "term", "required": True},
        {"name": "style", "required": False},
        {"name": "note", "required": False},
    ]
    assert prompts.map_arguments(arguments, []) == ({}, ["term"])
    assert prompts.map_arguments(arguments, ["cache"]) == ({"term": "cache"}, [])
    # The last argument takes the rest of the words.
    values, _ = prompts.map_arguments(arguments, ["cache", "short", "for", "the", "methods"])
    assert values == {"term": "cache", "style": "short", "note": "for the methods"}


@pytest.mark.asyncio
async def test_a_fetched_prompt_becomes_the_persons_message() -> None:
    async with Client(_server()) as client:
        result = await client.get_prompt("show_figure", {"caption": "Red"})
    text, images = prompts.prompt_message(result)
    assert text.startswith("Describe this figure. Caption: Red")
    assert '<resource uri="fixture://notes/figure-data">\ntwo pixels\n</resource>' in text
    assert "The prompt includes this assistant message:\n> It is a fixture." in text
    (image,) = images
    assert image.type == "image" and image.media_type == "image/png" and image.data == PNG


@pytest.mark.asyncio
async def test_prompts_are_cached_per_connection_and_relisted_after_a_change() -> None:
    calls: list[int] = []
    cache: dict[str, Any] = {}
    async with Client(_server()) as client:
        real = client.list_prompts

        async def counted() -> Any:
            calls.append(1)
            return await real()

        client.list_prompts = counted  # type: ignore[method-assign]
        first = await mcp_prompts.list_prompts(client, "fixture_docs", cache)
        again = await mcp_prompts.list_prompts(client, "fixture_docs", cache)
        mcp_prompts.mark_prompts_changed("fixture_docs")
        after = await mcp_prompts.list_prompts(client, "fixture_docs", cache)
    assert sorted(p.name for p in first) == ["define_term", "show_figure"]
    assert [p.name for p in again] == [p.name for p in first]
    assert len(after) == 2 and len(calls) == 2


@pytest.mark.asyncio
async def test_a_server_without_the_prompts_capability_offers_none() -> None:
    client = SimpleNamespace(
        initialize_result=SimpleNamespace(capabilities=SimpleNamespace(prompts=None))
    )
    assert await mcp_prompts.list_prompts(client, "tools_only", {}) == []


def test_rows_come_only_from_connected_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    arg = SimpleNamespace(name="term", description="The term.", required=True)
    listed = [SimpleNamespace(name="define_term", title="", description="Define.", arguments=[arg])]
    executor = SimpleNamespace(
        is_namespace_prepared=lambda name: name == "fixture_docs",
        list_namespace_prompts=lambda name: listed,
    )
    monkeypatch.setattr(prompts, "session_executor", lambda app, sid: executor)
    monkeypatch.setattr(prompts, "_servers", lambda app, sid: ["fixture_docs", "cold"])
    (row,) = prompts.prompt_rows(SimpleNamespace(), "sess_1")
    assert row["id"] == "/mcp__fixture_docs__define_term" and row["kind"] == "mcp_prompt"
    assert row["label"] == "fixture_docs" and row["argument_hint"] == "<term>"
    assert row["arguments"] == [{"name": "term", "description": "The term.", "required": True}]
