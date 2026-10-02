"""Legacy id-less tool calls get ids in the one-time session migration, on real clio-core.

Before the agent-loop rebuild the loop wrote each executed call as a ``tool_call``
segment ``{name, args}`` with no id, immediately followed by its own ``observation``
``{text}`` with no ``call_id``. The fold matches results to calls by id and refuses an
id-less call, so such a session opened on the rebuilt loop failed its next turn. The
migration of a session stored without the lane index (:mod:`clio_agent.arc.lane_migration`)
gives each such adjacent pair one deterministic id (``legacy_<call segment id>``) through
append-only ``replace`` atoms. These pin, on clio-core with the real fold
(:class:`~clio_agent.gact.agents.context_reader.ContextReader` over ``ARCMemory``):

* a migrated legacy session folds into the conversation it was, each result matched to
  its own call, and a restarted process reads the same ids without migrating again;
* the original atoms are never rewritten: the as-of read before the migration still
  shows the id-less pair;
* a legacy call with no adjacent observation (a cancelled turn) gets its id but no
  invented output: the fold fails typed ``unanswered_call``;
* an id-less call that is not a legacy pair (written to an indexed session) still fails
  typed ``malformed_call``.

Sabotage (each turns a test here red; restored after):
* the migration skipping the id pass -> the legacy fold fails ``malformed_call``;
* the observation keeping no ``call_id`` -> the legacy fold fails ``orphan_observation``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from dspy.lm15 import TextPart, ToolCallPart, ToolResultPart

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import Segment, encode_segments
from clio_agent.arc.storage import ClioCoreStore, make_arc_store
from clio_agent.gact.agents.clio_react_record import ContextFoldError
from clio_agent.gact.agents.context_reader import ContextReader

SCOPE = "main"


def _store() -> ClioCoreStore:
    store = make_arc_store(backend="cte", namespace=f"lc-{uuid.uuid4().hex[:10]}")
    assert isinstance(store, ClioCoreStore)
    return store


class _OldSession:
    """Writes atoms in the old develop layout: ``_events/w/<span>`` chunks, no index."""

    def __init__(self, store: ClioCoreStore) -> None:
        self.store = store
        self.sid = "old_" + uuid.uuid4().hex[:8]
        self.chunks: dict[str, list[Segment]] = {}
        self._lt = 0
        self._order = 0.0

    def atom(self, kind: str, content: dict[str, Any], *, span: str = "span1") -> Segment:
        self._lt += 1
        if kind != "ws_op":
            self._order += 1.0
        seg = Segment(
            scope=SCOPE,
            kind=kind,  # type: ignore[arg-type]
            content=content,
            session_id=self.sid,
            step=0,
            order=self._order if kind != "ws_op" else 0.0,
            logical_time=self._lt,
            expert_span_id=span,
        )
        self.chunks.setdefault(f"_events/w/{span}", []).append(seg)
        return seg

    def put(self) -> None:
        for scope, atoms in self.chunks.items():
            name = f"{self.sid}__{scope.replace('/', '~')}"
            self.store.put("segments", name, encode_segments(atoms))


def _read(arc: ARCMemory, sid: str) -> list[Any]:
    return ContextReader(arc, sid, SCOPE).read()


def test_a_legacy_session_folds_with_paired_ids_after_migration() -> None:
    """Two old turns (the first reset by the old loop's delete) fold, after the
    migration, into the second turn's step: both calls with their own results."""
    store = _store()
    try:
        old = _OldSession(store)
        # Turn 1 (the old loop tombstoned it when turn 2 started).
        t1 = [
            old.atom("thought", {"text": "look"}),
            old.atom("tool_call", {"name": "fs_read_file", "args": {"path": "a"}}),
            old.atom("observation", {"text": "A"}),
            old.atom("thought", {"text": "done 1"}),
        ]
        old.atom("ws_op", {"op": "delete", "targets": [s.id for s in t1]})
        # Turn 2: one step with two calls, then the answer step.
        old.atom("thought", {"text": "compare"}, span="span2")
        c1 = old.atom("tool_call", {"name": "shell_bash", "args": {"cmd": "x"}}, span="span2")
        old.atom("observation", {"text": "X"}, span="span2")
        c2 = old.atom("tool_call", {"name": "shell_bash", "args": {"cmd": "y"}}, span="span2")
        old.atom("observation", {"text": "Y"}, span="span2")
        old.atom("thought", {"text": "east dropped most"}, span="span2")
        old.put()
        migrated_at = old._lt

        arc = ARCMemory(store=store)
        messages = _read(arc, old.sid)
        assert [m.role for m in messages] == ["assistant", "tool", "assistant"]
        calls = [p for p in messages[0].parts if isinstance(p, ToolCallPart)]
        assert [(c.id, c.name, c.input) for c in calls] == [
            (f"legacy_{c1.id}", "shell_bash", {"cmd": "x"}),
            (f"legacy_{c2.id}", "shell_bash", {"cmd": "y"}),
        ]
        results = [p for p in messages[1].parts if isinstance(p, ToolResultPart)]
        assert [(r.id, r.content[0].text) for r in results] == [  # type: ignore[union-attr]
            (f"legacy_{c1.id}", "X"),
            (f"legacy_{c2.id}", "Y"),
        ]
        assert messages[2].parts == (TextPart(text="east dropped most"),)

        # Append-only: the clock before the migration still reads the id-less pair.
        before = arc.render_segments(old.sid, SCOPE, as_of=migrated_at)
        assert [s.content.get("id") for s in before if s.kind == "tool_call"] == [None, None]

        # A second process reads the persisted ids; it does not migrate again.
        again = ARCMemory(store=store)
        assert _read(again, old.sid) == messages
        assert len(again.render_segments(old.sid, SCOPE, as_of=10**12)) == 6
    finally:
        store.clear()


def test_a_legacy_call_without_its_observation_fails_typed_unanswered() -> None:
    """A cancelled old turn left a call with no observation: it gets its id, and the
    fold refuses it as unanswered instead of the model seeing an invented result."""
    store = _store()
    try:
        old = _OldSession(store)
        old.atom("thought", {"text": "start"})
        call = old.atom("tool_call", {"name": "shell_bash", "args": {"cmd": "z"}})
        old.atom("thought", {"text": "next"})
        old.put()

        arc = ARCMemory(store=store)
        with pytest.raises(ContextFoldError) as info:
            _read(arc, old.sid)
        assert info.value.details["problem"] == "unanswered_call"
        assert f"legacy_{call.id}" in str(info.value)
    finally:
        store.clear()


def test_an_idless_call_in_an_indexed_session_still_fails_typed() -> None:
    """Only the one-time migration assigns ids: an id-less call written to a session
    that already has its index is not a legacy pair, and the fold refuses it."""
    store = _store()
    try:
        arc = ARCMemory(store=store)
        sid = "new_" + uuid.uuid4().hex[:8]
        arc.append_segment(sid, SCOPE, "thought", {"text": "t"})
        arc.append_segment(sid, SCOPE, "tool_call", {"name": "shell_bash", "args": {}})
        arc.append_segment(sid, SCOPE, "observation", {"text": "out"})
        with pytest.raises(ContextFoldError) as info:
            _read(arc, sid)
        assert info.value.details["problem"] == "malformed_call"
        again = ARCMemory(store=store)  # a restart does not migrate an indexed session
        with pytest.raises(ContextFoldError) as info:
            _read(again, sid)
        assert info.value.details["problem"] == "malformed_call"
    finally:
        store.clear()
