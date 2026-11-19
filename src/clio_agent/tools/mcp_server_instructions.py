"""What each MCP server says about itself (``instructions``), kept per namespace.

A server may send ``instructions`` when a client connects. CLIO keeps them per
namespace from the live listing (:func:`record_listing_facts`) and from every connection
that serves calls (:func:`record_connection_instructions`), so the latest connection's
text is the one in use, and each turn asks the kept-alive connections again
(:func:`rediscover_instructions`), so a server whose text changes while it runs is
described as it is now. They persist with the listing cache entry and are replayed on a
cache hit until a live connection describes the server, so every MCP server the agent has
is described in its prompt by its own words, including a server deferred behind
``search_tools`` (:mod:`clio_agent.gact.agents.deferred_tools`), and ``search_tools``
finds a deferred server's tools by the words of its instructions
(:mod:`clio_agent.gact.tool_catalog`).

The prompt carries a bounded excerpt (:func:`instructions_excerpt`, up to
``tools.instructions_chars``) and says when it is shortened; the whole instructions
reach the model with the server's tools when ``search_tools`` loads them
(:func:`clio_agent.gact.search_tools.search_result`).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Mapping, Sequence
from typing import Any

from clio_agent import conf

__all__ = [
    "forget_instructions",
    "instructions_chars",
    "instructions_excerpt",
    "instructions_for",
    "instructions_refresh_s",
    "record_connection_instructions",
    "record_instructions",
    "record_listing_facts",
    "rediscover_instructions",
]

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_BY_NAMESPACE: dict[str, str] = {}
#: Namespaces whose instructions came from a live connection in this process.
_LIVE: set[str] = set()


def instructions_chars() -> int:
    """How much of a server's instructions the prompt carries (``tools.instructions_chars``).

    A bound on the prompt's excerpt only: ``search_tools`` gives the whole text.

    Raises:
        ValueError: The configured length is below 2 (one character and the ellipsis).
    """
    value = conf.resolve(
        "tools.instructions_chars",
        env="CLIO_TOOLS_INSTRUCTIONS_CHARS",
        default=1000,
        cast=conf.as_int,
    )
    if value < 2:
        raise ValueError(f"tools.instructions_chars must be at least 2, not {value}")
    return value


def record_instructions(namespace: str, instructions: str | None, *, cached: bool = False) -> None:
    """Keep (or, with nothing, forget) a namespace's server instructions.

    ``cached``: the text comes from the listing cache, not the server. It fills a
    namespace no live connection has described yet, and never replaces what one did.
    """
    text = str(instructions or "").strip()
    with _LOCK:
        if cached and namespace in _LIVE:
            return
        if not cached:
            _LIVE.add(namespace)
        if text:
            _BY_NAMESPACE[namespace] = text
        else:
            _BY_NAMESPACE.pop(namespace, None)


def forget_instructions(namespace: str) -> None:
    """Forget a namespace's instructions and where they came from (as in a new process)."""
    with _LOCK:
        _BY_NAMESPACE.pop(namespace, None)
        _LIVE.discard(namespace)


def instructions_for(namespace: str, *, summary: bool = False) -> str:
    """The namespace's instructions; ``summary`` gives :func:`instructions_excerpt`'s text."""
    if summary:
        return instructions_excerpt(namespace)[0]
    with _LOCK:
        return _BY_NAMESPACE.get(namespace, "")


def instructions_excerpt(namespace: str) -> tuple[str, bool]:
    """``(text, shortened)``: the prompt's bounded form of the namespace's instructions.

    Every non-blank line, in order, up to ``tools.instructions_chars`` characters; a
    longer text is cut at a word and ends with an ellipsis, and ``shortened`` says so.
    Later paragraphs are kept (a server often lists what it reaches there: its sites,
    datasets or packages), so the bound, not the first paragraph, limits the excerpt.
    """
    with _LOCK:
        text = _BY_NAMESPACE.get(namespace, "")
    lines = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
    limit = instructions_chars()
    if len(lines) <= limit:
        return lines, False
    cut = lines[: limit - 1]
    space = max(cut.rfind(" "), cut.rfind("\n"))
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip() + "…", True


def record_listing_facts(namespace: str, client: Any, tools: Sequence[Any]) -> None:
    """Everything a live listing establishes: the task capability and the instructions."""
    from clio_agent.tools.mcp_task_routing import record_definitive_capability  # noqa: PLC0415

    record_definitive_capability(namespace, client, tools)
    record_instructions(namespace, getattr(client, "instructions", None))


def record_connection_instructions(namespace: str, client: Any) -> None:
    """Keep the instructions a server sent when this connection initialized.

    A server may say different things at each connection (what it reaches now), so
    every new connection, such as a conversation's own server process, refreshes the
    text the next prompt and ``search_tools`` read. A client that is not connected
    leaves the recorded text as it is.
    """
    if not _connected(client):
        return
    record_instructions(namespace, getattr(client, "instructions", None))


def instructions_refresh_s() -> float:
    """How long a turn waits for its servers' current instructions
    (``tools.instructions_refresh_s``; 0 turns the refresh off)."""
    return conf.resolve(
        "tools.instructions_refresh_s",
        env="CLIO_TOOLS_INSTRUCTIONS_REFRESH_S",
        default=2.0,
        cast=conf.as_float,
    )


async def rediscover_instructions(namespace: str, client: Any, *, timeout: float) -> bool:
    """Ask a connected server what it says about itself now, over its open connection.

    A modern (``server/discover``) connection sends ``server/discover`` again and keeps
    the answer's ``instructions``; the connection adopts nothing from it. A server's text
    may change while it runs (what it reaches now), and a kept-alive connection would
    otherwise keep the text it had when it opened. A handshake-era connection only has
    its ``initialize`` text, so it is left as it is. Returns whether the text was read;
    a failure or a slow answer keeps the recorded text.
    """
    if not _connected(client) or getattr(client, "initialize_result", None) is not None:
        return False
    version = getattr(client, "protocol_version", None)
    send = getattr(getattr(client, "session", None), "send_discover", None)
    if not version or not callable(send):
        return False
    try:
        async with asyncio.timeout(timeout):
            raw = await send(version)
    except Exception as exc:  # noqa: BLE001 - the recorded text stays; the turn goes on
        logger.info(
            "mcp_instructions_rediscover_failed namespace=%s error=%s",
            namespace,
            type(exc).__name__,
        )
        return False
    text = raw.get("instructions") if isinstance(raw, Mapping) else None
    record_instructions(namespace, text if isinstance(text, str) else None)
    return True


def _connected(client: Any) -> bool:
    connected = getattr(client, "is_connected", None)
    return callable(connected) and bool(connected())
