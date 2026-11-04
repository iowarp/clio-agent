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

What comes back is the conversation as it read: what was said, each call and each result,
oldest first, each text exactly as recorded. A search hit is a companion chunk of up to
16 atoms, so a query keeps only the atoms whose text has the query's words, ranked by
how many, and ``limit`` counts those steps. Records without text (an input's delivery
bookkeeping: ids and content hashes) and reasoning are left out; a cut is stated with
what to ask for next.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from clio_agent.arc.schema import segment_text
from clio_agent.errors import ClioError

__all__ = ["RecallContextError", "build_recall_context_tool", "recall_atoms", "recall_text"]

_MAX_ATOMS = 50
#: How each kind of step is labelled in the text the agent reads.
_LABELS = {"thought": "said", "tool_call": "call", "observation": "result", "user": "message"}


def context_search_available(arc: Any) -> bool:
    """Expose only supported recall modes; a missing app describes the full catalog."""
    if arc is None:
        return True
    supports = getattr(arc, "segment_search_is_semantic", None)
    return (
        bool(supports()) if callable(supports) else callable(getattr(arc, "search_context", None))
    )


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


def _readable(seg: Any) -> str:
    """The step's text as the conversation had it; ``""`` for a record with none."""
    if seg.kind == "tool_call":
        return segment_text(seg)
    text = seg.content.get("text")
    if text is None or text == "":
        return ""
    return text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)


def _row(seg: Any) -> dict[str, Any]:
    return {
        "id": seg.id,
        "kind": seg.kind,
        "compacted": seg.status != "live",
        "text": _readable(seg),
        "logical_time": getattr(seg, "logical_time", 0),
    }


def _matches(text: str, terms: list[str]) -> int:
    lowered = text.lower()
    return sum(term in lowered for term in terms)


def recall_atoms(
    arc: Any, session: str, *, query: str = "", ids: list[str] | None = None, limit: int = 10
) -> tuple[list[dict[str, Any]], int, list[str]]:
    """The readable steps ``ids`` names (compaction ids expanded) or ``query`` finds, oldest
    first; how many records without text were left out; the ids past the cut.

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
        rows = [_row(atoms[i]) for i in dict.fromkeys(wanted)]
        readable = [r for r in rows if r["text"]]
        rest = [r["id"] for r in readable[_MAX_ATOMS:]]
        shown = readable[:_MAX_ATOMS]
    else:
        terms = list(dict.fromkeys(w.lower() for w in re.findall(r"\w+", query)))
        hits = arc.search_context(session, query, k=_MAX_ATOMS)
        found = list(
            dict.fromkeys(a.atom_id for hit in hits for a in hit.atoms if a.atom_id in atoms)
        )
        rows = [_row(atoms[i]) for i in found]
        readable = [r for r in rows if r["text"]]
        # Stable: among equal word counts, the search's own ranking stands.
        ranked = sorted(
            (r for r in readable if _matches(r["text"], terms)),
            key=lambda r: -_matches(r["text"], terms),
        )
        rest = [r["id"] for r in ranked[max(1, min(int(limit), _MAX_ATOMS)) :]]
        shown = ranked[: max(1, min(int(limit), _MAX_ATOMS))]
    shown.sort(key=lambda r: r["logical_time"])
    return shown, len(rows) - len(readable), rest


def recall_text(rows: list[dict[str, Any]], *, left_out: int, rest: list[str]) -> str:
    """The steps as the agent reads them: one block per step, oldest first."""
    total = len(rows) + len(rest)
    head = [f"{len(rows)} of {total} steps, oldest first."]
    if left_out:
        head.append(f"{left_out} records without text left out (input delivery bookkeeping, or reasoning only).")
    if rest:
        head.append(f"Not shown, by id: recall_context(ids={json.dumps(rest)})")
    blocks = [
        f"--- {r['id']} · {_LABELS.get(r['kind'], r['kind'])}"
        + (" · compacted" if r["compacted"] else "")
        + f"\n{r['text']}"
        for r in rows
    ]
    return "\n".join([" ".join(head), *blocks]) if rows else " ".join(head)


def build_recall_context_tool() -> Any:
    """Build the auto-attached, read-only ``recall_context`` tool."""
    from clio_agent.arc.history_plane import plane_for  # noqa: PLC0415
    from clio_agent.gact import context as ctx  # noqa: PLC0415
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    search_enabled = context_search_available(plane_for(ctx.active_app()))

    def recall_context(query: str = "", ids: list[str] | None = None, limit: int = 10) -> str:
        """Read back earlier context, compacted steps included, exactly as recorded."""
        arc, session = _plane()
        rows, left_out, rest = recall_atoms(arc, session, query=query, ids=ids, limit=limit)
        return recall_text(rows, left_out=left_out, rest=rest)

    def recall_ids(ids: list[str]) -> str:
        """Read retained step or compaction IDs without an unavailable search mode."""
        return recall_context(ids=ids)

    args = {
        "ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Step ids, or a summary's compaction id (cmp_...).",
        }
    }
    if search_enabled:
        args.update(
            {
                "query": {
                    "type": "string",
                    "description": "Text to search earlier steps for; omit ids.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Most matching steps to show (1-50), for a query.",
                },
            }
        )
    return native_tool(
        recall_context if search_enabled else recall_ids,
        name="recall_context",
        presentation="memory",
        domain="memory",
        title="Recall context",
        representation="row",
        desc=(
            "Read back this conversation's earlier steps from the context store, including "
            "ones a summary replaced: what was said, each call and each result, oldest "
            "first, each text exactly as recorded (reasoning is not included). Pass `ids` "
            "(step ids, or the compaction id a summary names). "
            + (
                "Alternatively pass a search query: the steps with the most of its words come "
                "back. Give exactly one of ids or query, never both."
                if search_enabled
                else "Text search is unavailable in this context store; only ID recall is supported. "
                "Use the compaction id named in the summary to retrieve its original steps."
            )
        ),
        args=args,
        read_only=True,
    )
