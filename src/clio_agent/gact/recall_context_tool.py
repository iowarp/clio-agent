"""``recall_context``: the agent gets back what a compaction replaced, byte-exact.

A compaction changes only the agent's context view; clio-core keeps every step. This
auto-attached tool reads them back from clio-core:

* ``ids`` -- the atoms with those ids, live or compacted, with their stored content
  exactly as recorded. A compaction id (``cmp_...``, named on the summary's last line)
  stands for every atom that compaction replaced.
* ``query`` -- clio-core's search over every atom the session's agents ever had in
  context, compacted ones included (11a ``search_context``). When clio-core cannot
  search (#905) the typed :class:`~clio_agent.arc.memory_segments.SearchUnavailableError`
  is raised, which the loop shows the agent as a plain tool error.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from clio_agent.errors import ClioError

__all__ = ["RecallContextError", "build_recall_context_tool", "recall_atoms"]

_MAX_ATOMS = 50


class RecallContextError(ClioError):
    """A ``recall_context`` call that cannot be answered as asked (shown to the agent)."""

    reason = "recall_context_invalid"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message, error_type=self.reason, details=details)


def _plane() -> tuple[Any, str]:
    from clio_agent.gact.agents.clio_react_record import arc_scope  # noqa: PLC0415

    arc, session, _scope = arc_scope()
    if arc is None or not hasattr(arc, "search_context"):
        raise RecallContextError("recall_context needs the session's clio-core context")
    return arc, session


def _history(arc: Any, session: str) -> dict[str, Any]:
    """Every atom of the session's agent scopes, retired ones included, by id."""
    atoms: dict[str, Any] = {}
    for scope in arc.list_segment_scopes(session):
        if scope.startswith("_"):
            continue
        for seg in arc.list_segments(session, scope, include_tombstoned=True):
            atoms[seg.id] = seg
    return atoms


def _expand(ids: Iterable[str], atoms: dict[str, Any]) -> list[str]:
    """Atom ids, with each compaction id replaced by the ids its summary replaced."""
    out: list[str] = []
    for wanted in ids:
        summaries = [
            seg
            for seg in atoms.values()
            if seg.kind == "summary" and seg.content.get("compaction_id") == wanted
        ]
        if summaries:
            out.extend(i for seg in summaries for i in seg.derived_from)
        else:
            out.append(wanted)
    return out


def _row(seg: Any) -> dict[str, Any]:
    return {
        "id": seg.id,
        "scope": seg.scope,
        "kind": seg.kind,
        "compacted": seg.status != "live",
        "content": seg.content,
    }


def recall_atoms(
    arc: Any, session: str, *, query: str = "", ids: list[str] | None = None, limit: int = 10
) -> list[dict[str, Any]]:
    """The atoms ``ids`` names (compaction ids expanded) or ``query`` finds.

    Raises:
        RecallContextError: neither or both of ``query`` / ``ids``, or an unknown id.
        SearchUnavailableError: clio-core cannot search (``query`` only).
    """
    if bool(query.strip()) == bool(ids):
        raise RecallContextError("give exactly one of `query` or `ids`")
    atoms = _history(arc, session)
    if ids:
        wanted = _expand(ids, atoms)
        missing = [i for i in wanted if i not in atoms]
        if missing:
            raise RecallContextError(f"no such context ids: {', '.join(missing)}", missing=missing)
    else:
        hits = arc.search_context(session, query, k=max(1, min(int(limit), _MAX_ATOMS)))
        wanted = list(dict.fromkeys(a.atom_id for hit in hits for a in hit.atoms))
        wanted = [i for i in wanted if i in atoms]
    return [_row(atoms[i]) for i in wanted[:_MAX_ATOMS]]


def build_recall_context_tool() -> Any:
    """Build the auto-attached, read-only ``recall_context`` tool."""
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    def recall_context(query: str = "", ids: list[str] | None = None, limit: int = 10) -> str:
        """Read back earlier context, compacted steps included, exactly as recorded."""
        arc, session = _plane()
        rows = recall_atoms(arc, session, query=query, ids=ids, limit=limit)
        return json.dumps(rows, ensure_ascii=False)

    return native_tool(
        recall_context,
        name="recall_context",
        presentation="memory",
        domain="memory",
        title="Recall context",
        representation="row",
        desc=(
            "Read back this conversation's earlier steps from the context store, including "
            "ones a summary replaced, exactly as recorded. Pass `ids` (step ids, or the "
            "compaction id a summary names) or a search `query`."
        ),
        args={
            "query": {"type": "string", "description": "Text to search earlier steps for."},
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Step ids, or a summary's compaction id (cmp_...).",
            },
            "limit": {"type": "integer", "description": "Maximum steps (1-50) for a query."},
        },
        read_only=True,
    )
