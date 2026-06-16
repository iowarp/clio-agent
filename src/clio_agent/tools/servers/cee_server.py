"""clio-core CEE context-plane MCP server (epic #667; clio-core-integration idea #1).

A small, curated blackboard over clio-core's context store: an expert publishes a
piece of context under an agent *scope*, and another expert (now or later, here or
on another node) lists / retrieves / semantically discovers it. This is the MCP
surface onto the same convergent context plane the live ReAct loop reads from — the
"one agent produces, another finds it" primitive behind the distributed plane (#665).

Backed by an ``ARCStore`` (clio-core CTE in production, LocalFS for a single box /
tests), so the bytes live in clio-core and span nodes on a cluster. Five curated
tools (RULE 5); ``build_cee_server(store)`` injects the backend so the server is
testable in-memory with ``Client(server)``.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

# The blackboard rides ARC's "context" record kind; names are scope-addressed.
_KIND = "context"
_SEP = "::"


def _key(scope: str, name: str) -> str:
    # Reject the reserved separator so distinct (scope, name) pairs can't alias to the
    # same key (e.g. ('a','b::c') vs ('a::b','c') would both be 'a::b::c').
    if _SEP in scope or _SEP in name:
        raise ValueError(f"scope and name must not contain the reserved separator {_SEP!r}")
    return f"{scope}{_SEP}{name}"


def build_cee_server(store: Any) -> FastMCP:
    """Build the CEE context-plane MCP server over ``store`` (an ``ARCStore``)."""
    mcp = FastMCP("cee")

    @mcp.tool()
    def context_publish(scope: str, name: str, content: str) -> dict[str, Any]:
        """Publish a piece of context to the shared clio-core plane under ``scope``.

        Use this when you've produced a result another expert (or a later turn, or a
        worker on another node) should be able to find — a dataset summary, an
        intermediate finding, a handoff note. ``scope`` is the address (e.g.
        ``agentA`` or ``agentA/data``); ``name`` identifies the record within it.
        Overwrites an existing record with the same ``(scope, name)``.
        """
        payload = content.encode("utf-8")
        store.put(_KIND, _key(scope, name), payload, search_text=content)
        return {"published": True, "scope": scope, "name": name, "bytes": len(payload)}

    @mcp.tool()
    def context_list(scope: str) -> dict[str, Any]:
        """List the names of context records published under ``scope``.

        Use this to see what an agent scope already holds before publishing or
        retrieving — the blackboard's table of contents.
        """
        prefix = f"{scope}{_SEP}"
        names = sorted(n[len(prefix):] for n, _ in store.scan(_KIND, prefix))
        return {"scope": scope, "names": names, "count": len(names)}

    @mcp.tool()
    def context_get(scope: str, name: str) -> dict[str, Any]:
        """Retrieve a published context record's content by ``(scope, name)``.

        Use this to pull context another expert published — after ``context_list`` or
        ``context_search`` tells you it's there.
        """
        data = store.get(_KIND, _key(scope, name))
        if data is None:
            return {"found": False, "scope": scope, "name": name}
        return {"found": True, "scope": scope, "name": name, "content": data.decode("utf-8")}

    @mcp.tool()
    def context_search(scope: str, query: str, k: int = 5) -> dict[str, Any]:
        """Find the most relevant published context under ``scope`` for ``query``.

        Use this for discovery — "which record here knows about X" — when you don't
        know the exact name. Ranking is semantic (BM25) on clio-core CTE and a
        degraded word-overlap on the local backend (see ``semantic``).
        """
        prefix = f"{scope}{_SEP}"
        hits = store.search(_KIND, query, name_prefix=prefix, k=k)
        return {
            "scope": scope,
            "semantic": store.supports_search(),
            "hits": [{"name": n[len(prefix):], "score": s} for n, s in hits],
        }

    @mcp.tool()
    def context_drop(scope: str, name: str) -> dict[str, Any]:
        """Remove a published context record by ``(scope, name)``.

        Use this to retract context that's stale or superseded so other experts stop
        discovering it.
        """
        existed = store.exists(_KIND, _key(scope, name))
        store.delete(_KIND, _key(scope, name))
        return {"dropped": existed, "scope": scope, "name": name}

    return mcp
