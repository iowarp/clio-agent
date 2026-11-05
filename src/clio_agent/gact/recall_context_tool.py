"""``recall_context``: the agent gets back what a compaction replaced, as it was recorded.

A compaction changes only the agent's context view; clio-core keeps every step. This
auto-attached tool reads them back from clio-core:

* ``ids`` -- the atoms with those ids, live or compacted. A compaction id (``cmp_...``,
  named on the summary's last line) stands for every atom that compaction replaced; an
  earlier summary among them is opened in turn, so the steps come back, not the
  checkpoint that stood in for them.
* ``query`` -- clio-core's search over every atom the session's agents ever had in
  context, compacted ones included (11a ``search_context``). When clio-core cannot
  search (#905) the typed :class:`~clio_agent.arc.memory_segments.SearchUnavailableError`
  is raised, which the loop shows the agent as a plain tool error.

What comes back is the conversation as the model read it, through the same gate as its
context (:func:`~clio_agent.gact.agents.context_reader.model_context_segments`): what was
said, each call and each result, the person's messages delivered as confirmed input
(typed messages, a child's report, an answer: their Core content resolved), a result
whose input commitment or authorization is pending as its placeholder, and never staged
input the agent has not been given. By ids the steps come oldest first. A search hit is
a companion chunk of up to 16 atoms, so a query keeps only the atoms with the query's
words (each word of 3 or more characters that is not a common word, matched at the
start of a word), best match first, and ``limit`` counts those steps. Records without
text (an input's delivery bookkeeping) and reasoning are left out; a cut is stated with
what to ask for next.

A recall result has a fixed size in characters (``tools.recall_result_chars``), not a
share of the context window: on a 1M-token window a share is 125k tokens, and whole
compactions came back inline (64-195 KB a call, L1R). Past it, or past the window's own
room when that is smaller, the whole text goes to the session's tool-output folder and
the agent gets the head and the file, with the same note as any cut result
(``clio.reminder.result_spilled``), to search with fs_search_text and read on with
fs_read_file; the room that cut it is traced (``recall_result_spilled``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from clio_agent.arc.schema import segment_text
from clio_agent.errors import ClioError
from clio_agent.tools.result_room import ResultRoom

__all__ = [
    "RECALL_RESULT_ROOM",
    "RecallContextError",
    "bounded_recall",
    "build_recall_context_tool",
    "recall_atoms",
    "recall_result_chars",
    "recall_text",
]

_MAX_ATOMS = 50
#: The default ``tools.recall_result_chars``: about 4,000 tokens, a few replies.
_RECALL_RESULT_CHARS = 16_000
#: The room's reason, as recorded with a cut result.
RECALL_RESULT_ROOM = "recall_result_chars"
#: Traced when a recall result is cut to its head and a file.
RECALL_SPILLED_REASON = "recall_result_spilled"
#: How each kind of step is labelled in the text the agent reads.
_LABELS = {"thought": "said", "tool_call": "call", "observation": "result", "user": "message"}
#: Query words too common to rank a step by (with every word under 3 characters).
_STOPWORDS = frozenset(
    "the and for are was were with that this from have has had not but you your all any "
    "can did does its into than then them they what when where which who why how about "
    "there their our out over only also just very more most some such".split()
)


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


def _plane() -> tuple[Any, str, Any]:
    """The session's clio-core plane, its id and the task history that resolves inputs."""
    from clio_agent.gact import context as ctx  # noqa: PLC0415
    from clio_agent.gact.agents.clio_react_record import arc_scope  # noqa: PLC0415

    arc, session, _scope = arc_scope()
    if arc is None or not hasattr(arc, "search_context"):
        raise RecallContextError("recall_context needs the session's clio-core context")
    history = getattr(getattr(ctx.active_app(), "state", None), "task_history", None)
    return arc, session, history


def _history(arc: Any, session: str) -> dict[str, tuple[str, Any]]:
    """Every atom of the session's agent scopes, retired ones included: id -> (scope, atom)."""
    atoms: dict[str, tuple[str, Any]] = {}
    for scope in arc.list_segment_scopes(session):
        if scope.startswith("_"):
            continue
        for seg in arc.list_segments(session, scope, include_tombstoned=True):
            atoms[seg.id] = (scope, seg)
    return atoms


def _expand(ids: Iterable[str], atoms: dict[str, tuple[str, Any]]) -> list[str]:
    """Atom ids, with each compaction id and each summary atom replaced by the ids its
    summary replaced (an earlier summary among them opened in turn)."""
    by_compaction: dict[str, list[str]] = {}
    for _scope, seg in atoms.values():
        if seg.kind == "summary" and seg.content.get("compaction_id"):
            by_compaction.setdefault(str(seg.content["compaction_id"]), []).append(seg.id)
    out: list[str] = []
    seen: set[str] = set()
    stack = list(reversed(list(ids)))
    while stack:
        wanted = stack.pop()
        if wanted in seen:
            continue
        seen.add(wanted)
        found = atoms.get(wanted)
        if wanted in by_compaction:
            stack.extend(reversed(by_compaction[wanted]))
        elif found is not None and found[1].kind == "summary":
            stack.extend(reversed(list(found[1].derived_from)))
        else:
            out.append(wanted)
    return out


def _readable(seg: Any) -> str:
    """The step's text as the conversation had it; ``""`` for a record with none."""
    if seg.kind == "tool_call":
        return segment_text(seg)
    content = seg.content if isinstance(seg.content, dict) else {}
    text = content.get("text")
    if text is None or text == "":
        return ""
    return text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)


def _row(scope: str, seg: Any, history: Any, session: str) -> dict[str, Any]:
    """One step as the model's context shows it (the context read's own gate)."""
    from clio_agent.gact.agents.context_reader import model_context_segments  # noqa: PLC0415

    shown = model_context_segments([seg], history, session, scope)
    return {
        "id": seg.id,
        "kind": shown[0].kind if shown else seg.kind,
        "compacted": seg.status != "live",
        "text": "\n\n".join(t for t in (_readable(s) for s in shown) if t),
        "logical_time": getattr(seg, "logical_time", 0),
    }


def _terms(query: str) -> list[str]:
    """The query's words that rank a step: 3+ characters and not common (all of them when
    none is)."""
    words = list(dict.fromkeys(re.findall(r"\w+", query.lower())))
    return [w for w in words if len(w) >= 3 and w not in _STOPWORDS] or words


def _matches(text: str, terms: list[str]) -> int:
    lowered = text.lower()
    return sum(bool(re.search(rf"(?<!\w){re.escape(term)}", lowered)) for term in terms)


def recall_atoms(
    arc: Any,
    session: str,
    *,
    query: str = "",
    ids: list[str] | None = None,
    limit: int = 10,
    history: Any = None,
) -> tuple[list[dict[str, Any]], int, list[str]]:
    """The readable steps ``ids`` names (compaction ids expanded; oldest first) or ``query``
    finds (best match first); how many records without text were left out; the ids past
    the cut.

    Args:
        history: The task history that resolves confirmed input and knows which inputs
            and results the agent has been given (the app's ``task_history``).

    Raises:
        RecallContextError: neither or both of ``query`` / ``ids``, or an unknown id.
        SearchUnavailableError: clio-core cannot search (``query`` only).
        ContextReadError: a confirmed input with no ``history`` to resolve it.
    """
    if bool(query.strip()) == bool(ids):
        raise RecallContextError("give exactly one of `query` or `ids`")
    atoms = _history(arc, session)
    if ids:
        wanted = _expand(ids, atoms)
        missing = [i for i in wanted if i not in atoms]
        if missing:
            raise RecallContextError(f"no such context ids: {', '.join(missing)}", missing=missing)
        rows = [_row(*atoms[i], history, session) for i in dict.fromkeys(wanted)]
        readable = sorted((r for r in rows if r["text"]), key=lambda r: r["logical_time"])
        return (
            readable[:_MAX_ATOMS],
            len(rows) - len(readable),
            [r["id"] for r in readable[_MAX_ATOMS:]],
        )
    terms = _terms(query)
    hits = arc.search_context(session, query, k=_MAX_ATOMS)
    found = list(dict.fromkeys(a.atom_id for hit in hits for a in hit.atoms if a.atom_id in atoms))
    rows = [_row(*atoms[i], history, session) for i in found]
    readable = [r for r in rows if r["text"]]
    # Stable: among equal word counts, the search's own ranking stands.
    ranked = sorted(
        (r for r in readable if _matches(r["text"], terms)),
        key=lambda r: -_matches(r["text"], terms),
    )
    cap = max(1, min(int(limit), _MAX_ATOMS))
    return ranked[:cap], len(rows) - len(readable), [r["id"] for r in ranked[cap:]]


def recall_text(
    rows: list[dict[str, Any]], *, left_out: int, rest: list[str], ranked: bool = False
) -> str:
    """The steps as the agent reads them: one block per step, oldest first (a query's
    ``ranked`` rows best match first)."""
    total = len(rows) + len(rest)
    head = [f"{len(rows)} of {total} steps, {'best match' if ranked else 'oldest'} first."]
    if left_out:
        head.append(
            f"{left_out} records without text left out (input delivery bookkeeping, input "
            "not given to the agent yet, or reasoning only)."
        )
    if rest:
        head.append(f"Not shown, by id: recall_context(ids={json.dumps(rest)})")
    blocks = [
        f"--- {r['id']} ({_LABELS.get(r['kind'], r['kind'])}"
        + (", compacted)" if r["compacted"] else ")")
        + f"\n{r['text']}"
        for r in rows
    ]
    return "\n".join([" ".join(head), *blocks]) if rows else " ".join(head)


def recall_result_chars() -> int:
    """Characters of a recall result shown inline (``tools.recall_result_chars``)."""
    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "tools.recall_result_chars",
        env="CLIO_TOOLS_RECALL_RESULT_CHARS",
        default=_RECALL_RESULT_CHARS,
        cast=conf.as_int,
    )


@dataclass(frozen=True)
class _RecallRoom(ResultRoom):
    """The recall size: a bound in characters (``tools.recall_result_chars``)."""

    def fits(self, text: str) -> bool:
        """Whether ``text`` is shown whole: at most ``chars`` characters."""
        return len(text) <= self.chars


def bounded_recall(text: str) -> str:
    """``text`` whole when it fits the recall size (characters) and the window's room,
    else its head and the file that holds all of it (the cutting room traced)."""
    from clio_agent.runtime import trace  # noqa: PLC0415
    from clio_agent.tools.mcp_result_projection import (  # noqa: PLC0415
        bounded_model_tool_result,
    )
    from clio_agent.tools.result_room import CHARS_PER_TOKEN, result_room  # noqa: PLC0415

    window = result_room()
    chars = max(CHARS_PER_TOKEN, recall_result_chars())
    own = _RecallRoom(
        tokens=chars // CHARS_PER_TOKEN,
        chars=chars,
        reason=RECALL_RESULT_ROOM,
        window=window.window,
    )
    room = own if own.chars <= window.chars else window
    if room.fits(text):
        return text
    trace.event(
        "RECALL",
        "%s reason=%s chars=%d room_chars=%d",
        RECALL_SPILLED_REASON,
        room.reason,
        len(text),
        room.chars,
    )
    return bounded_model_tool_result(text, room=room)


def build_recall_context_tool() -> Any:
    """Build the auto-attached, read-only ``recall_context`` tool."""
    from clio_agent.arc.history_plane import plane_for  # noqa: PLC0415
    from clio_agent.gact import context as ctx  # noqa: PLC0415
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    search_enabled = context_search_available(plane_for(ctx.active_app()))

    def recall_context(query: str = "", ids: list[str] | None = None, limit: int = 10) -> str:
        """Read back earlier context, compacted steps included, as it was recorded."""
        arc, session, history = _plane()
        rows, left_out, rest = recall_atoms(
            arc, session, query=query, ids=ids, limit=limit, history=history
        )
        text = recall_text(rows, left_out=left_out, rest=rest, ranked=not ids)
        return bounded_recall(text)

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
            "ones a summary replaced: what was said, each call and each result, and the "
            "messages you were given, as recorded (reasoning is not included; a long recall "
            "comes back as its start and a file to read on). Pass `ids` (step ids, or the "
            "compaction id a summary names; its steps come back oldest first). "
            + (
                "Alternatively pass a search query: the steps with the most of its words come "
                "back, best match first. Give exactly one of ids or query, never both."
                if search_enabled
                else "Text search is unavailable in this context store; only ID recall is supported. "
                "Use the compaction id named in the summary to retrieve its original steps."
            )
        ),
        args=args,
        read_only=True,
    )
