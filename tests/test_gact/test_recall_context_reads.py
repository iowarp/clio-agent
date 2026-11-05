"""``recall_context`` reads back what the model was given, on real clio-core segments.

Every segment here is the real shape on a real CTE plane: steps appended by the plane,
confirmed input recorded through a real :class:`TaskHistory` (``rendered_content``
holding Core references), compactions folded by ``summarize_segments``. Only clio-core's
search is scripted (which atoms a query hits), since what it ranks is not under test.

These double as the tool's result showcase: a confirmed message, a result still
withheld, a nested compaction, a ranked query, a cut list, and a long recall spilled to
a file.

SABOTAGE (each turns the named test red):

* ``_row``: read ``content["text"]`` only (skip ``model_context_segments``) ->
  ``test_a_confirmed_message_comes_back_as_the_message`` and
  ``test_recall_never_shows_what_the_context_withholds``.
* ``_expand``: stop at the compaction's own ids ->
  ``test_a_nested_compaction_opens_to_its_steps``.
* ``recall_atoms``: sort a query's rows oldest first ->
  ``test_a_query_shows_the_best_match_first_and_matches_whole_words``.
* ``_terms``: keep short and common words ->
  ``test_a_query_shows_the_best_match_first_and_matches_whole_words``.
* ``bounded_recall``: compare estimated tokens instead of characters ->
  ``test_the_recall_size_is_characters_and_a_cut_is_traced``.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.storage import make_arc_store
from clio_agent.gact import recall_context_tool as recall
from clio_agent.gact.task_history import TaskHistory
from tests._confirmed_input import confirm_input

SESSION, SCOPE = "owner", "main"


@pytest.fixture
def arc() -> Iterator[ARCMemory]:
    memory = ARCMemory(store=make_arc_store(backend="cte", namespace=f"rc-{uuid.uuid4().hex[:8]}"))
    yield memory
    memory._store.clear()


def _step(arc: ARCMemory, i: int, said: str, result: str) -> list[str]:
    thought = arc.append_segment(SESSION, SCOPE, "thought", {"text": said, "thinking": []})
    call = {"id": f"c{i}", "name": "search", "args": {"q": str(i)}}
    called = arc.append_segment(SESSION, SCOPE, "tool_call", call)
    obs = {"call_id": f"c{i}", "text": result, "is_error": False}
    observed = arc.append_segment(SESSION, SCOPE, "observation", obs)
    return [thought.id, called.id, observed.id]


def _fold(arc: ARCMemory, ids: list[str], cmp: str) -> None:
    arc.summarize_segments(SESSION, SCOPE, ids, {"text": f"summary {cmp}", "compaction_id": cmp})


class _Searched:
    """The real plane, with clio-core's search scripted to ``chunks`` (atom ids each)."""

    def __init__(self, arc: ARCMemory, chunks: list[list[str]]) -> None:
        self.arc, self.chunks = arc, chunks

    def __getattr__(self, name: str) -> Any:
        return getattr(self.arc, name)

    def search_context(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return [
            SimpleNamespace(atoms=[SimpleNamespace(atom_id=i) for i in chunk])
            for chunk in self.chunks
        ]


def _blocks(text: str) -> dict[str, tuple[str, str]]:
    """``{id: (label, text)}`` of a recall result."""
    out = {}
    for block in text.split("\n--- ")[1:]:
        header, _, body = block.partition("\n")
        step_id, _, label = header.partition(" (")
        out[step_id] = (label.rstrip(")"), body)
    return out


def test_a_confirmed_message_comes_back_as_the_message(arc: ARCMemory) -> None:
    """The person's messages, a child's report and an answer reach the agent as confirmed
    input: Core references, no ``text``. Recall dropped them as bookkeeping."""
    history = TaskHistory(arc)
    confirm_input(history, "ask", "use mm/day and skip campaign B")
    ids = _step(arc, 0, "reading", "HITS-0")
    [receipt] = [s for s in arc.render_working_set(SESSION, SCOPE) if s.kind == "recipient_input"]
    _fold(arc, [receipt.id, *ids], "cmp_a")
    rows, left_out, rest = recall.recall_atoms(arc, SESSION, ids=["cmp_a"], history=history)
    shown = _blocks(recall.recall_text(rows, left_out=left_out, rest=rest))
    assert shown[receipt.id] == ("message, compacted", "use mm/day and skip campaign B")
    assert left_out == 0 and rest == []


def test_recall_never_shows_what_the_context_withholds(arc: ARCMemory) -> None:
    """A result whose input commitment or authorization is pending reads as the context's
    placeholder; input staged but not given to the agent is left out, as in the context."""
    history = TaskHistory(arc)
    ids = _step(arc, 0, "waiting on the child", "")
    arc.append_segment(SESSION, SCOPE, "tool_call", {"id": "c9", "name": "wait", "args": {}})
    pending = {"call_id": "c9", "text": "SECRET child report", "notification_ids": ["n-1"]}
    withheld = arc.append_segment(SESSION, SCOPE, "observation", pending)
    staged = arc.append_segment(
        SESSION, SCOPE, "user", {"text": "STAGED steer", "notification_ids": ["n-2"]}
    )
    every = [*ids, withheld.id, staged.id]
    rows, left_out, _rest = recall.recall_atoms(arc, SESSION, ids=every, history=history)
    text = recall.recall_text(rows, left_out=left_out, rest=[])
    assert "SECRET" not in text and "STAGED" not in text
    assert "authorization is pending" in _blocks(text)[withheld.id][1]
    assert staged.id not in _blocks(text) and left_out >= 1


def test_a_nested_compaction_opens_to_its_steps(arc: ARCMemory) -> None:
    """A second compaction folds the first one's summary; its id returns the steps
    under both, not the first checkpoint's text."""
    first = _step(arc, 0, "step zero", "HITS-0")
    _fold(arc, first, "cmp_1")
    second = _step(arc, 1, "step one", "HITS-1")
    live = [s.id for s in arc.render_working_set(SESSION, SCOPE)]
    _fold(arc, live, "cmp_2")
    rows, _left, _rest = recall.recall_atoms(arc, SESSION, ids=["cmp_2"])
    assert [r["id"] for r in rows] == [*first, *second]  # oldest first
    assert all(r["compacted"] for r in rows) and "summary cmp_1" not in str(rows)


def test_a_query_shows_the_best_match_first_and_matches_whole_words(arc: ARCMemory) -> None:
    """Rows were ranked, re-sorted oldest first, then cut at the head: the best match
    could be only in the spilled file. Short and common words ranked as much as the
    content words, and ``art`` matched inside ``start``."""
    a = _step(arc, 0, "the start of it", "nothing here")
    c = _step(arc, 1, "the catalog is open", "catalog")
    b = _step(arc, 2, "the art catalog lists of it", "art catalog: 12 rows")
    plane = _Searched(arc, [[*a, *c], b])
    query = "the art catalog of it"
    rows, _left, rest = recall.recall_atoms(plane, SESSION, query=query, limit=3)
    # Both content words first (the newest steps), then one; "the start of it" none.
    assert [r["id"] for r in rows] == [b[0], b[2], c[0]] and rest == [c[2]]
    text = recall.recall_text(rows, left_out=0, rest=rest, ranked=True)
    assert text.startswith("3 of 4 steps, best match first.")


def test_the_recall_size_is_characters_and_a_cut_is_traced(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """``tools.recall_result_chars`` is characters, not estimated tokens; a recall past it
    goes to a file the agent can search, and the room that cut it is traced."""
    from clio_agent.runtime import trace
    from clio_agent.tools import result_room

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLIO_TOOLS_RECALL_RESULT_CHARS", "2000")
    big = result_room.ResultRoom(tokens=250_000, chars=1_000_000, reason="test", window=1_000_000)
    monkeypatch.setattr(result_room, "_measure", lambda: big)
    events: list[str] = []
    monkeypatch.setattr(trace, "event", lambda tag, msg, *args: events.append(msg % args))
    # A tokenizer packs repeated text tight: 2,100 characters estimate under 2000 // 4.
    from clio_agent.gact.runtime import context_tokens

    monkeypatch.setattr(context_tokens, "_estimate_text_tokens", lambda text: len(text) // 8)
    fits_by_tokens = "a" * 2_100
    assert recall.bounded_recall("a" * 2_000) == "a" * 2_000 and not events
    shown = recall.bounded_recall(fits_by_tokens)
    assert shown.startswith("[clio: result_spilled]") and len(shown) <= 2_000
    [event] = events
    assert event.startswith("recall_result_spilled reason=recall_result_chars chars=2100")

    ids = [i for n in range(30) for i in _step(arc, n, f"step {n}", f"out {n} " + "x" * 300)]
    rows, left_out, rest = recall.recall_atoms(arc, SESSION, ids=ids)
    full = recall.recall_text(rows, left_out=left_out, rest=rest)
    spilled = recall.bounded_recall(full)
    path = re.search(r"`([^`]+\.txt)`", spilled)
    assert path is not None and open(path.group(1), encoding="utf-8").read() == full
    assert "fs_search_text" in spilled and "50 of 90 steps" in full
