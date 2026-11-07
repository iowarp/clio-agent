"""Each prompt the session agent's MCP servers offer is ``/mcp__<server>__<prompt>``.

The prompts are listed over the agent's own connection to each server (the
executor's ``list_namespace_prompts``), only for servers that connection already
reaches: listing never starts a server. Running one maps the typed words onto the
prompt's declared arguments in order (the last argument takes the rest), refuses a
missing required argument by name, and fetches the prompt over that same connection.
Its messages become the person's message: text as text, an embedded text resource
as a ``<resource>`` block, an image as an image part, and an ``assistant`` message
as quoted context, since the turn starts from the person.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from mcp.shared.exceptions import MCPError

from clio_agent.gact.agents.resolution import SessionAgentUnavailable, session_resolution
from clio_agent.gact.parts import Part
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

PREFIX = "/mcp__"


def command_id(server: str, prompt: str) -> str:
    return f"{PREFIX}{server}__{prompt}"


def session_executor(app: "FastAPI", sid: str) -> Any:
    """The session agent's MCP executor (its workspace and server set bound)."""

    from clio_agent.gact.agents.tool_executor_resolution import (  # noqa: PLC0415
        resolve_active_base_agent_tool_executor,
    )

    agent = getattr(app.state, "agent", None)
    if agent is None:
        return None
    return resolve_active_base_agent_tool_executor(
        agent, app=app, session_id=sid, emit_downgrade=lambda _executor: None
    )


def _servers(app: "FastAPI", sid: str) -> list[str]:
    try:
        return sorted(session_resolution(app, sid).mcp_servers or {})
    except SessionAgentUnavailable:
        return []


def _argument_rows(prompt: Any) -> list[dict[str, Any]]:
    return [
        {
            "name": str(getattr(arg, "name", "")),
            "description": str(getattr(arg, "description", "") or ""),
            "required": bool(getattr(arg, "required", False)),
        }
        for arg in getattr(prompt, "arguments", None) or []
    ]


def prompt_rows(app: "FastAPI", sid: str) -> list[dict[str, Any]]:
    """One ``mcp_prompt`` row per prompt of each server the agent is connected to."""

    executor = session_executor(app, sid)
    lister = getattr(executor, "list_namespace_prompts", None)
    prepared = getattr(executor, "is_namespace_prepared", None)
    if not callable(lister) or not callable(prepared):
        return []
    rows = []
    for server in _servers(app, sid):
        if not prepared(server):
            continue  # not connected yet: listing never starts a server
        try:
            prompts = lister(server)
        except (MCPError, OSError, TimeoutError, RuntimeError, ValueError) as exc:
            # A server that cannot answer now lists no prompts; typed, never silent.
            trace.event("TOOLS", "mcp_prompts_list_failed namespace=%s error=%r", server, exc)
            continue
        for prompt in prompts:
            name = str(getattr(prompt, "name", "") or "")
            arguments = _argument_rows(prompt)
            ident = command_id(server, name)
            rows.append(
                {
                    "kind": "mcp_prompt",
                    "id": ident,
                    "name": ident,
                    "qualified_id": ident,
                    "plugin": "",
                    "source": "mcp_prompt",
                    "label": server,
                    "title": str(getattr(prompt, "title", "") or name),
                    "description": str(getattr(prompt, "description", "") or ""),
                    "argument_hint": " ".join(
                        f"<{a['name']}>" if a["required"] else f"[{a['name']}]" for a in arguments
                    ),
                    "arguments": arguments,
                    "aliases": [],
                    "status": "available",
                    "enabled": True,
                    "disabled_reason": "",
                    "disabled_detail": "",
                    "unsupported_fields": [],
                    "agent": "",
                    "handler": "",
                    "path": "",
                    "server": server,
                    "prompt": name,
                    "prompt_template": "",
                    "root": "",
                }
            )
    return rows


def map_arguments(
    arguments: Sequence[Mapping[str, Any]], words: Sequence[str]
) -> tuple[dict[str, str], list[str]]:
    """``(values, missing required names)`` for ``words`` laid onto ``arguments`` in order."""

    values: dict[str, str] = {}
    names = [str(a["name"]) for a in arguments]
    for index, name in enumerate(names):
        if index >= len(words):
            break
        last = index == len(names) - 1
        values[name] = " ".join(words[index:]) if last else words[index]
    missing = [
        str(a["name"]) for a in arguments if a.get("required") and str(a["name"]) not in values
    ]
    return values, missing


def _block(content: Any) -> tuple[str, Part | None]:
    """One content block as model text, or an image part."""

    kind = getattr(content, "type", "")
    if kind == "text":
        return str(getattr(content, "text", "")), None
    if kind == "image":
        return "", Part(
            type="image",
            data=str(getattr(content, "data", "")),
            media_type=str(getattr(content, "mimeType", "") or getattr(content, "mime_type", "")),
        )
    if kind == "resource":
        resource = getattr(content, "resource", None)
        uri = str(getattr(resource, "uri", ""))
        mime = str(getattr(resource, "mimeType", "") or "")
        if getattr(resource, "text", None) is not None:
            return f'<resource uri="{uri}">\n{resource.text}\n</resource>', None
        if mime.startswith("image/"):
            return "", Part(type="image", data=str(getattr(resource, "blob", "")), media_type=mime)
        return f'<resource uri="{uri}" mime="{mime}">(binary content not shown)</resource>', None
    if kind == "resource_link":
        return (
            f'<resource_link uri="{getattr(content, "uri", "")}" name="{getattr(content, "name", "")}"/>',
            None,
        )
    return f"(a {kind or 'content'} block the conversation cannot show)", None


def prompt_message(result: Any) -> tuple[str, list[Part]]:
    """The fetched prompt's messages as the person's model text and image parts."""

    texts: list[str] = []
    images: list[Part] = []
    for message in getattr(result, "messages", None) or []:
        blocks = getattr(message, "content", None)
        blocks = blocks if isinstance(blocks, list) else [blocks]
        for block in blocks:
            text, image = _block(block)
            if image is not None:
                images.append(image)
            if not text:
                continue
            if str(getattr(message, "role", "user")) == "assistant":
                quoted = "\n".join(f"> {line}" for line in text.splitlines())
                text = f"The prompt includes this assistant message:\n{quoted}"
            texts.append(text)
    return "\n\n".join(texts), images


def fetch_prompt(
    app: "FastAPI", sid: str, row: Mapping[str, Any], values: Mapping[str, str]
) -> Any:
    executor = session_executor(app, sid)
    getter = getattr(executor, "get_namespace_prompt", None)
    if not callable(getter):
        raise RuntimeError("this agent has no MCP connection for prompts")
    return getter(str(row["server"]), str(row["prompt"]), dict(values))


__all__ = [
    "PREFIX",
    "command_id",
    "fetch_prompt",
    "map_arguments",
    "prompt_message",
    "prompt_rows",
]
