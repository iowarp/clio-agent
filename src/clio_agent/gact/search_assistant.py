"""Read-only search of the connected service's registered workspaces and transcripts."""

from __future__ import annotations

import asyncio
import codecs
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import FastAPI, HTTPException

from clio_agent.gact import context
from clio_agent.gact.routes.workspace_file_listing import collect_workspace_file_entries
from clio_agent.gact.routes.workspace_file_read import readable_workspace_file
from clio_agent.gact.runtime.memory_search import (
    _memory_search_excerpt,
    _memory_search_response,
    _memory_search_terms,
)
from clio_agent.gact.side_sessions import is_read_only_side_session

SEARCH_AGENT = "clio/clio-search"
FILE_PREFIX_BYTES = 512 * 1024
CONTENT_BUDGET_BYTES = 8 * 1024 * 1024
FILE_WALK_LIMIT = 5000


def _active() -> tuple[FastAPI, Any]:
    app = context.active_app()
    session = app.state.sessions.get(context.active_session_id()) if app else None
    if (
        app is None
        or session is None
        or not is_read_only_side_session(session)
        or not session.metadata.get("search_session")
    ):
        raise PermissionError(
            "These tools are available only in the read-only CLIO Search session."
        )
    return app, session


def session_href(
    workspace_id: str, session_id: str, message_id: str = "", part_id: str = "", find: str = ""
) -> str:
    """Return an actual conversation or passage link without losing its workspace identity."""
    href = f"session://{quote(workspace_id, safe='')}/{quote(session_id, safe='')}"
    if message_id:
        href += f"#message-{quote(message_id, safe='')}"
        params = {key: value for key, value in {"part": part_id, "find": find}.items() if value}
        if params:
            href += "?" + urlencode(params, quote_via=quote)
    return href


def file_href(workspace_id: str, path: str, *, line: int | None = None, find: str = "") -> str:
    """Return a workspace-scoped file link with an optional exact text destination."""
    href = f"workspace-file://{quote(workspace_id, safe='')}/{quote(path.replace(chr(92), '/'), safe='/')}"
    params = {
        key: value
        for key, value in {"line": str(line) if line is not None else "", "find": find}.items()
        if value
    }
    return href + ("?" + urlencode(params, quote_via=quote) if params else "")


def _read_prefix(app: FastAPI, workspace_id: str, path: str) -> tuple[str | None, bool, int]:
    target = readable_workspace_file(app, workspace_id, path)
    with target.open("rb") as stream:
        raw = stream.read(FILE_PREFIX_BYTES + 1)
    truncated = len(raw) > FILE_PREFIX_BYTES
    raw = raw[:FILE_PREFIX_BYTES]
    encoding = (
        "utf-16" if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8-sig"
    )
    if encoding != "utf-16" and b"\x00" in raw:
        return None, truncated, len(raw)
    try:
        return (
            codecs.getincrementaldecoder(encoding)().decode(raw, final=not truncated),
            truncated,
            len(raw),
        )
    except UnicodeDecodeError:
        return None, truncated, len(raw)


async def search_connected_service(
    app: FastAPI,
    *,
    query: str,
    kind: str = "all",
    workspace_id: str = "",
    offset: int = 0,
    limit: int = 20,
    content: bool = False,
) -> dict[str, Any]:
    """Search bounded real sources; expose omissions and pagination instead of claiming completeness."""
    query = query.strip()
    if not query or len(query) > 2000 or kind not in {"all", "sessions", "messages", "files"}:
        raise ValueError(
            "Provide a query up to 2000 characters and kind all, sessions, messages or files."
        )
    limit = max(1, min(limit, 50))
    offset = max(0, min(offset, 10000))
    workspaces = [
        workspace
        for workspace in app.state.workspaces.list()
        if not workspace_id or workspace.id == workspace_id
    ]
    if workspace_id and not workspaces:
        raise ValueError(f"Unknown workspace: {workspace_id}")
    hits: list[dict[str, Any]] = []
    limitations: list[dict[str, Any]] = []
    needle = query.casefold()
    if kind in {"all", "sessions"}:
        for session in app.state.sessions.list(workspace_id=workspace_id or None):
            if session.metadata.get("search_session") or session.metadata.get("side_session"):
                continue
            if needle in session.title.casefold():
                hits.append(
                    {
                        "kind": "session",
                        "title": session.title,
                        "workspace_id": session.workspace_id,
                        "session_id": session.id,
                        "href": session_href(session.workspace_id, session.id),
                        "archived": session.archived,
                    }
                )
    if kind in {"all", "messages"}:
        response = await asyncio.to_thread(
            _memory_search_response,
            app,
            query=query,
            workspace_id=workspace_id,
            include_cross_session=True,
            limit=100,
        )
        for hit in response.hits:
            session = app.state.sessions.get(hit.session_id)
            if (
                session
                and not session.metadata.get("search_session")
                and not session.metadata.get("side_session")
            ):
                hits.append(
                    {
                        "kind": "message",
                        **hit.model_dump(mode="json"),
                        "href": session_href(
                            hit.workspace_id,
                            hit.session_id,
                            hit.message_id,
                            hit.part_id or "",
                            query if hit.metadata.get("exact_phrase") else "",
                        ),
                    }
                )
        if len(response.hits) == 100:
            limitations.append(
                {
                    "kind": "messages",
                    "reason": "Top 100 message matches; narrow the query or workspace for more.",
                }
            )
    if kind in {"all", "files"}:
        budget = CONTENT_BUDGET_BYTES
        for workspace in workspaces:
            root = Path(workspace.root_path or os.getcwd()).expanduser().resolve()
            walk = await collect_workspace_file_entries(
                app, workspace.id, root, limit=FILE_WALK_LIMIT, exclude_service_storage=True
            )
            if walk.truncated:
                limitations.append(
                    {
                        "workspace_id": workspace.id,
                        "reason": "File listing reached 5000 entries; unlisted files were not searched.",
                    }
                )
            prefix_count = 0
            denied_count = 0
            unsupported_count = 0
            for entry in walk.entries:
                if entry.get("type") != "file":
                    continue
                path = str(entry["path"])
                text = ""
                name_match = needle in path.casefold()
                if content and budget > 0:
                    try:
                        value, truncated, consumed = await asyncio.to_thread(
                            _read_prefix, app, workspace.id, path
                        )
                        budget -= consumed
                        prefix_count += int(truncated)
                        unsupported_count += int(value is None)
                        text = value or ""
                    except (HTTPException, OSError):
                        denied_count += 1
                if not name_match and needle not in text.casefold():
                    continue
                position = text.casefold().find(needle)
                line = text[:position].count("\n") + 1 if position >= 0 else None
                hits.append(
                    {
                        "kind": "file",
                        "workspace_id": workspace.id,
                        "path": path,
                        "line": line,
                        "text": _memory_search_excerpt(
                            text, _memory_search_terms(query), query=query
                        )
                        if position >= 0
                        else "",
                        "href": file_href(
                            workspace.id, path, line=line, find=query if position >= 0 else ""
                        ),
                    }
                )
            if prefix_count or denied_count or unsupported_count:
                limitations.append(
                    {
                        "workspace_id": workspace.id,
                        "prefix_only_files": prefix_count,
                        "unreadable_or_policy_denied_files": denied_count,
                        "binary_or_unsupported_encoding_files": unsupported_count,
                    }
                )
        if content and budget <= 0:
            limitations.append(
                {
                    "kind": "files",
                    "reason": "8 MiB content budget reached; remaining files searched by path only.",
                }
            )
    page = hits[offset : offset + limit]
    return {
        "query": query,
        "scope": "connected_service",
        "workspaces": [
            {"id": workspace.id, "title": workspace.to_wire()["display_name"]}
            for workspace in workspaces
        ],
        "hits": page,
        "next_offset": offset + limit if offset + limit < len(hits) else None,
        "matched_count": len(hits),
        "limitations": limitations,
    }


async def read_search_source(
    app: FastAPI,
    *,
    workspace_id: str,
    session_id: str = "",
    message_id: str = "",
    path: str = "",
    start_line: int = 1,
    max_lines: int = 120,
) -> dict[str, Any]:
    """Read a bounded source using verified IDs or the shared workspace file policy."""
    if app.state.workspaces.get(workspace_id) is None:
        raise ValueError("Unknown workspace")
    if bool(path) == bool(session_id):
        raise ValueError("Supply a file path or a session identity, exactly one source at a time.")
    start_line = max(1, start_line)
    max_lines = max(1, min(max_lines, 200))
    if path and not session_id:
        text, prefix_truncated, _ = await asyncio.to_thread(_read_prefix, app, workspace_id, path)
        if text is None:
            raise ValueError(
                "This source is not UTF-8 text; open the returned file link in the file viewer."
            )
        lines = text.splitlines()
        selected = "\n".join(lines[start_line - 1 : start_line - 1 + max_lines])
        return {
            "kind": "file",
            "href": file_href(workspace_id, path, line=start_line),
            "start_line": start_line,
            "text": selected[:16000],
            "truncated": prefix_truncated
            or len(selected) > 16000
            or start_line - 1 + max_lines < len(lines),
            "next_line": start_line + max_lines
            if start_line - 1 + max_lines < len(lines)
            else None,
        }
    session = app.state.sessions.get(session_id)
    if (
        session is None
        or session.workspace_id != workspace_id
        or session.metadata.get("search_session")
        or session.metadata.get("side_session")
    ):
        raise ValueError("Conversation is unavailable in this workspace")
    messages = app.state.messages.get(session_id, [])
    selected_messages = (
        [message for message in messages if message.id == message_id]
        if message_id
        else messages[-20:]
    )
    if message_id and not selected_messages:
        raise ValueError("Message is unavailable in this conversation")
    result = []
    for message in selected_messages:
        text = "\n".join(
            part.text for part in message.parts if part.type in {"text", "thinking", "error"}
        )
        lines = text.splitlines()
        selected = "\n".join(lines[start_line - 1 : start_line - 1 + max_lines])
        result.append(
            {
                "message_id": message.id,
                "role": message.role,
                "text": selected[:16000],
                "truncated": len(selected) > 16000 or start_line - 1 + max_lines < len(lines),
                "href": session_href(workspace_id, session_id, message.id),
            }
        )
    return {
        "kind": "conversation",
        "title": session.title,
        "messages": result,
        "earlier_messages_omitted": not message_id and len(messages) > 20,
    }


def build_clio_search_tool() -> Any:
    """Build the dedicated assistant's read-only discovery tool."""
    from clio_agent.gact.agents.tool_instrumentation import native_tool

    async def clio_search(
        query: str,
        kind: str = "all",
        workspace_id: str = "",
        offset: int = 0,
        limit: int = 20,
        content: bool = False,
    ) -> dict[str, Any]:
        app, _ = _active()
        return await search_connected_service(
            app,
            query=query,
            kind=kind,
            workspace_id=workspace_id,
            offset=offset,
            limit=limit,
            content=content,
        )

    return native_tool(
        clio_search,
        name="clio_search",
        presentation="memory",
        domain="memory",
        title="Search CLIO",
        representation="row",
        read_only=True,
        desc="Find actual sessions, message passages and files across this connected service. Optional bounded file-content search. Follow next_offset and limitations; hrefs are real internal links.",
        args={
            "query": {"type": "string"},
            "kind": {"type": "string", "enum": ["all", "sessions", "messages", "files"]},
            "workspace_id": {"type": "string"},
            "offset": {"type": "integer"},
            "limit": {"type": "integer"},
            "content": {"type": "boolean"},
        },
    )


def build_clio_search_read_tool() -> Any:
    """Build the dedicated assistant's bounded, policy-aware source reader."""
    from clio_agent.gact.agents.tool_instrumentation import native_tool

    async def clio_search_read(
        workspace_id: str,
        session_id: str = "",
        message_id: str = "",
        path: str = "",
        start_line: int = 1,
        max_lines: int = 120,
    ) -> dict[str, Any]:
        app, _ = _active()
        return await read_search_source(
            app,
            workspace_id=workspace_id,
            session_id=session_id,
            message_id=message_id,
            path=path,
            start_line=start_line,
            max_lines=max_lines,
        )

    return native_tool(
        clio_search_read,
        name="clio_search_read",
        presentation="memory",
        domain="memory",
        title="Read search source",
        representation="row",
        read_only=True,
        desc="Read real message or file evidence before answering. Supply workspace_id and session_id/message_id OR path from a search hit. Text is bounded; inspect truncation.",
        args={
            "workspace_id": {"type": "string"},
            "session_id": {"type": "string"},
            "message_id": {"type": "string"},
            "path": {"type": "string"},
            "start_line": {"type": "integer"},
            "max_lines": {"type": "integer"},
        },
    )
