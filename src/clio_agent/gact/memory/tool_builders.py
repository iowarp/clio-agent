"""Native tool builders for the long-term memory tools (the ``memory`` plugin's claims)."""

from __future__ import annotations

from typing import Any

from clio_agent.gact.memory import tools as memory_tools

_CATEGORIES = "profile, preferences, topics, areas, people or environments"


def _native(func: Any, **declaration: Any) -> Any:
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    return native_tool(func, presentation="memory", domain="memory", **declaration)


def build_memory_search_tool() -> Any:
    """``memory_search``: BM25 over the session's memory."""

    def memory_search(query: str, category: str = "", limit: int = 8) -> dict[str, Any]:
        return memory_tools.memory_search(query, category, limit)

    return _native(
        memory_search,
        name="memory_search",
        title="Search memory",
        representation="row",
        desc=(
            "Search long-term memory about the user, their projects and how they work. "
            "Returns matching documents with their description and matching facts."
        ),
        args={
            "query": {"type": "string", "description": "Words to look for."},
            "category": {"type": "string", "description": f"Optional: one of {_CATEGORIES}."},
            "limit": {"type": "integer", "description": "Maximum documents, 1 through 20."},
        },
        read_only=True,
    )


def build_memory_read_tool() -> Any:
    """``memory_read``: one whole document."""

    def memory_read(name: str) -> dict[str, Any]:
        return memory_tools.memory_read(name)

    return _native(
        memory_read,
        name="memory_read",
        title="Read memory",
        representation="row",
        desc="Read one memory document by name, with every fact and its provenance.",
        args={"name": {"type": "string", "description": "Document name from the index or search."}},
        read_only=True,
    )


def build_create_memory_tool() -> Any:
    """``create_memory``: remember a fact the user asked to remember."""

    def create_memory(
        subject: str, fact: str, category: str, scope: str = "user", reason: str = ""
    ) -> dict[str, Any]:
        return memory_tools.create_memory(subject, fact, category, scope, reason)

    return _native(
        create_memory,
        name="create_memory",
        title="Remember",
        representation="row",
        desc="Remember a fact because the user asked you to. It is filed under its subject.",
        args={
            "subject": {"type": "string", "description": "What the fact is about."},
            "fact": {"type": "string", "description": "The fact, as one sentence."},
            "category": {"type": "string", "description": f"One of {_CATEGORIES}."},
            "scope": {
                "type": "string",
                "description": "user (about the user everywhere) or project (this workspace).",
            },
            "reason": {"type": "string", "description": "What the user asked."},
        },
    )


def build_update_memory_tool() -> Any:
    """``update_memory``: correct a remembered fact."""

    def update_memory(fact_id: str, fact: str, reason: str = "") -> dict[str, Any]:
        return memory_tools.update_memory(fact_id, fact, reason)

    return _native(
        update_memory,
        name="update_memory",
        title="Correct memory",
        representation="row",
        desc="Replace a remembered fact with the corrected one the user gave.",
        args={
            "fact_id": {"type": "string", "description": "The id of the fact to replace."},
            "fact": {"type": "string", "description": "The corrected fact."},
            "reason": {"type": "string", "description": "What the user asked."},
        },
    )


def build_forget_memory_tool() -> Any:
    """``forget_memory``: forget a fact or a document."""

    def forget_memory(fact_id: str = "", name: str = "", reason: str = "") -> dict[str, Any]:
        return memory_tools.forget_memory(fact_id, name, reason)

    return _native(
        forget_memory,
        name="forget_memory",
        title="Forget",
        representation="row",
        desc="Forget a fact (by fact_id) or a whole document (by name) the user asked to forget.",
        args={
            "fact_id": {"type": "string", "description": "The fact to forget."},
            "name": {"type": "string", "description": "Or: the document to forget entirely."},
            "reason": {"type": "string", "description": "What the user asked."},
        },
    )


def build_propose_memory_change_tool() -> Any:
    """``propose_memory_change``: the curator's one write (a proposal, never a change)."""
    from clio_agent.gact.memory import proposals  # noqa: PLC0415

    def propose_memory_change(
        target: str,
        change: str,
        evidence: list[str],
        rationale: str,
        facts: list[str] | None = None,
        replaces: list[str] | None = None,
        merge_from: list[str] | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        return proposals.propose_from_curator(
            target=target,
            change=change,
            evidence=evidence,
            rationale=rationale,
            facts=list(facts or []),
            replaces=list(replaces or []),
            merge_from=list(merge_from or []),
            description=description,
        )

    return _native(
        propose_memory_change,
        name="propose_memory_change",
        title="Propose a memory change",
        representation="row",
        desc=(
            "Propose one change to memory; the user or the auto-accept rule decides it. "
            "change: add (facts to target, creating it if new), supersede (replaces one "
            "fact id with one fact), merge (merge_from documents into target), or retire "
            "(target is no longer read)."
        ),
        args={
            "target": {
                "type": "string",
                "description": "Document name, e.g. user/environments/frontier.",
            },
            "change": {"type": "string", "description": "add, supersede, merge or retire."},
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Candidate ids (c1, c2, ...) the change rests on.",
            },
            "rationale": {"type": "string", "description": "One line: why."},
            "facts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Facts to add, one sentence each.",
            },
            "replaces": {
                "type": "array",
                "items": {"type": "string"},
                "description": "For supersede: the fact id replaced.",
            },
            "merge_from": {
                "type": "array",
                "items": {"type": "string"},
                "description": "For merge: documents folded into target and retired.",
            },
            "description": {
                "type": "string",
                "description": "For a new document: one line saying what it holds.",
            },
        },
    )


BUILDERS = {
    "memory_search": build_memory_search_tool,
    "memory_read": build_memory_read_tool,
    "create_memory": build_create_memory_tool,
    "update_memory": build_update_memory_tool,
    "forget_memory": build_forget_memory_tool,
    "propose_memory_change": build_propose_memory_change_tool,
}

__all__ = ["BUILDERS"]
