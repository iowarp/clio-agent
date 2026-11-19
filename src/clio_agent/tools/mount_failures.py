"""A declared MCP server that did not mount: the root it needs, and why, for the agent.

A stdio server is spawned in the conversation's workspace root, so a root that no longer
exists (deleted between runs, or never created by a client that wrote the store directly)
would refuse every stdio server of the conversation at once. :func:`ensure_root` recreates
it before mounting, as the workspace routes do when a root is set.

Any other refusal is recorded by root and server name (:func:`record`), so the agent reads
the reason where the tools are missing (``search_tools``, ``offer_plugin``) instead of
concluding there is no such server. A later successful mount of the same server clears it.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from clio_agent.tools.workspace_root import canonical_workspace_root

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_FAILED: dict[tuple[str, str], str] = {}

__all__ = ["clear", "ensure_root", "failures", "note", "record"]


def ensure_root(cwd: str | None, specs: Mapping[str, Any]) -> None:
    """Create ``cwd`` when a stdio server among ``specs`` will be spawned in it and it is gone.

    A root that cannot be created is left as it is: each stdio server's own preflight then
    refuses it with the exact reason, which :func:`record` keeps for the agent.
    """

    if not cwd or os.path.isdir(cwd):
        return
    if not any(getattr(spec, "transport", "") == "stdio" for spec in specs.values()):
        return
    from clio_agent.runtime import trace  # noqa: PLC0415

    try:
        Path(cwd).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        trace.event("TOOLS", "workspace_root_unavailable root=%s reason=%s", cwd, exc)
        return
    trace.event("TOOLS", "workspace_root_recreated root=%s reason=missing_before_mount", cwd)


def record(cwd: str | None, name: str, exc: BaseException) -> None:
    """Keep why ``name`` did not mount at ``cwd`` (also logged as before)."""

    logger.warning("failed to mount declared MCP %r: %s", name, exc)
    with _LOCK:
        _FAILED[(canonical_workspace_root(cwd), name)] = str(exc)


def clear(cwd: str | None, name: str) -> None:
    """``name`` mounted at ``cwd``: an earlier refusal there no longer holds."""

    with _LOCK:
        _FAILED.pop((canonical_workspace_root(cwd), name), None)


def failures(root: str | None) -> dict[str, str]:
    """Server name to refusal for the servers that did not mount at ``root`` (every root's
    when ``root`` is empty)."""

    key = canonical_workspace_root(root)
    with _LOCK:
        return {name: why for (at, name), why in _FAILED.items() if not key or at == key}


def note(root: str | None, names: Iterable[str]) -> str:
    """What the agent should know about the servers among ``names`` that did not mount at
    ``root``: a tool name (``<server>_<tool>``), a server name or a search word names one."""

    failed = failures(root)
    asked = [str(name).casefold() for name in names if str(name).strip()]
    hit = sorted(
        server
        for server in failed
        if any(
            word == server.casefold() or word.startswith(f"{server.casefold()}_") for word in asked
        )
    )
    return " ".join(
        f"The {server} server is in this conversation but did not start, so its tools are "
        f"missing: {failed[server]}. Tell the person what failed."
        for server in hit
    )
