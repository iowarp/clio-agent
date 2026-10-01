"""The context view is the fold (Phase 11a), on real clio-core.

``FoldingSegmentStore`` keeps each scope's folded live context as a view: appends
extend it, ops rebuild it from the scope's anchor, a summarize that retires everything
before it moves the anchor, and a restarted process reads the session index and only
the chunks from the anchor on. These pin, over random op sequences on clio-core:

* view == full fold: the live render equals the fold of the scope's whole log
  (``render(as_of=<now>)`` folds every atom) and the plain ``SegmentStore`` driven
  with the same ops (kind, content, order);
* a cold read after a restart == the warm read, history (tombstones) included;
* an append keeps the view generation and the previous render is a prefix of the
  next; an op renews the generation;
* a cold read after a compaction fetches only the chunks from the anchor on, never
  scans the store;
* a session stored before the index (unchunked partitions, live-text companions, no
  index) is migrated once on first access and reads the same.

Sabotage (each turns a test here red; restored after):
* ``ContextView.anchors_at`` returning True for a partial summarize -> the property
  test's cold read loses live atoms;
* ``ContextView.append`` skipping ``self.live.append`` -> view != full fold;
* ``note_atom`` not listing a scope's new chunk -> cold != warm;
* ``set_anchor`` keeping every chunk -> the anchor cold-read test fetches pre-anchor
  chunks;
* the migration returning an empty index -> the migration test reads nothing.
"""

from __future__ import annotations

import uuid
from typing import Any

import msgspec
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from clio_agent.arc.context_view import fold_atoms
from clio_agent.arc.lane_index import INDEX_SCOPE, decode_index
from clio_agent.arc.live import _MemoryStore
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import Segment, encode_segments
from clio_agent.arc.storage import ClioCoreStore, make_arc_store
from clio_agent.arc.working_set_fold import FoldingSegmentStore

SCOPES = ("agentA", "agentB/sub")
SPANS = ("", "span1", "span2")
NOW = 10**12


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Small chunks so every sequence rolls chunks and companion chunks."""
    monkeypatch.setenv("CLIO_ARC_WS_CHUNK_SEGMENTS", "3")
    monkeypatch.setenv("CLIO_ARC_SEARCH_CHUNK_ATOMS", "2")


def _store() -> ClioCoreStore:
    store = make_arc_store(backend="cte", namespace=f"cv-{uuid.uuid4().hex[:10]}")
    assert isinstance(store, ClioCoreStore)
    return store


def _shape(segs: list[Segment]) -> list[tuple[str, str, Any, float]]:
    return [(s.kind, s.status, s.content, s.order) for s in segs]


def _ids(segs: list[Segment]) -> list[str]:
    return [s.id for s in segs]


# One op: (name, scope index, span index, a seed for which ids it picks).
OPS = st.lists(
    st.tuples(
        st.sampled_from(
            ["append"] * 6
            + ["delete", "replace", "summarize_all", "summarize_some", "insert"]
            + ["restart"]
        ),
        st.integers(0, len(SCOPES) - 1),
        st.integers(0, len(SPANS) - 1),
        st.integers(0, 10**6),
    ),
    min_size=4,
    max_size=28,
)


def _apply(arc: ARCMemory, ref: ARCMemory, sid: str, op: tuple[str, int, int, int], n: int) -> None:
    """Apply one op to the fold (``arc``) and to the plain reference store (``ref``)."""
    name, si, pi, seed = op
    scope, span = SCOPES[si], SPANS[pi]
    live = arc.render_segments(sid, scope)
    ref_live = ref.render_segments(sid, scope)
    assert len(live) == len(ref_live)
    pick = [i for i in range(len(live)) if (seed >> (i % 20)) & 1] or ([0] if live else [])
    if name == "append" or not live:
        kind = "observation" if seed % 2 else "thought"
        for store in (arc, ref):
            store.append_segment(sid, scope, kind, {"text": f"t{n}"}, step=n, expert_span_id=span)
    elif name == "delete":
        arc.delete_segments(sid, scope, [live[i].id for i in pick])
        ref.delete_segments(sid, scope, [ref_live[i].id for i in pick])
    elif name == "replace":
        i = seed % len(live)
        arc.replace_segment(sid, scope, live[i].id, {"text": f"r{n}"}, expert_span_id=span)
        ref.replace_segment(sid, scope, ref_live[i].id, {"text": f"r{n}"}, expert_span_id=span)
    elif name in ("summarize_all", "summarize_some"):
        chosen = range(len(live)) if name == "summarize_all" else pick
        arc.summarize_segments(sid, scope, [live[i].id for i in chosen], {"text": f"S{n}"})
        ref.summarize_segments(sid, scope, [ref_live[i].id for i in chosen], {"text": f"S{n}"})
    elif name == "insert":
        pos = seed % (len(live) + 1)
        for store in (arc, ref):
            store.insert_segment(sid, scope, pos, "user", {"text": f"i{n}"}, expert_span_id=span)


def _check(arc: ARCMemory, ref: ARCMemory, sid: str) -> None:
    for scope in SCOPES:
        view = arc.render_segments(sid, scope)
        full = arc.render_segments(sid, scope, as_of=NOW)
        assert _ids(view) == _ids(full), "view != fold of the whole log"
        assert _shape(view) == _shape(ref.render_segments(sid, scope)), "view != plain store"
        assert _shape(arc.list_segments(sid, scope, include_tombstoned=True)) == _shape(
            ref.list_segments(sid, scope, include_tombstoned=True)
        )


@settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(ops=OPS)
def test_view_equals_full_fold_over_random_ops(ops: list[tuple[str, int, int, int]]) -> None:
    store = _store()
    try:
        arc = ARCMemory(store=store)
        ref = ARCMemory(store=_MemoryStore(), working_set_fold=False)  # the plain reference
        sid = "cv_" + uuid.uuid4().hex[:10]
        for n, op in enumerate(ops):
            if op[0] == "restart":
                warm = {s: _ids(arc.render_segments(sid, s)) for s in SCOPES}
                history = {
                    s: _shape(arc.list_segments(sid, s, include_tombstoned=True)) for s in SCOPES
                }
                arc = ARCMemory(store=store)  # a new process: no views, no index in memory
                assert {s: _ids(arc.render_segments(sid, s)) for s in SCOPES} == warm
                assert {
                    s: _shape(arc.list_segments(sid, s, include_tombstoned=True)) for s in SCOPES
                } == history
                continue
            _apply(arc, ref, sid, op, n)
            _check(arc, ref, sid)
        cold = ARCMemory(store=store)
        for scope in SCOPES:
            assert _ids(cold.render_segments(sid, scope)) == _ids(arc.render_segments(sid, scope))
    finally:
        store.clear()


def test_append_keeps_the_prefix_and_the_generation_an_op_renews_it() -> None:
    store = _store()
    try:
        arc = ARCMemory(store=store)
        sid = "cv_" + uuid.uuid4().hex[:10]
        renders = []
        generations = set()
        for i in range(7):
            arc.append_segment(sid, "agentA", "thought", {"text": f"t{i}"}, step=i)
            snap = arc.context_view(sid, "agentA")
            renders.append(_ids(list(snap.segments)))
            generations.add(snap.generation)
        assert len(generations) == 1, "an append renewed the generation"
        for before, after in zip(renders, renders[1:], strict=False):
            assert after[: len(before)] == before and len(after) == len(before) + 1
        arc.delete_segments(sid, "agentA", [renders[-1][2]])
        after_op = arc.context_view(sid, "agentA")
        assert after_op.generation not in generations
        assert _ids(list(after_op.segments)) == [x for x in renders[-1] if x != renders[-1][2]]
    finally:
        store.clear()


def test_a_cold_read_after_compaction_fetches_only_chunks_from_the_anchor() -> None:
    store = _store()
    try:
        arc = ARCMemory(store=store)
        sid = "cv_" + uuid.uuid4().hex[:10]
        for i in range(10):
            arc.append_segment(
                sid, "agentA", "observation", {"text": f"old{i}"}, expert_span_id="s1"
            )
        live = arc.render_working_set(sid, "agentA")
        summary = arc.summarize_segments(sid, "agentA", [s.id for s in live], {"text": "SUM"})
        for i in range(4):
            arc.append_segment(
                sid, "agentA", "observation", {"text": f"new{i}"}, expert_span_id="s2"
            )
        warm = _ids(arc.render_segments(sid, "agentA"))

        raw = store.get("segments", f"{sid}__{INDEX_SCOPE.replace('/', '~')}")
        assert raw is not None
        index = decode_index(sid, raw)
        entry = index.scopes["agentA"]
        assert entry.anchor is not None and entry.anchor.atom_id == summary.id
        pre_anchor = {c for c in index.chunks if c not in entry.chunks}
        assert pre_anchor, "the old atoms' chunks are behind the anchor"

        fetched: list[str] = []
        scans: list[str] = []
        real_get, real_scan = store.get, store.scan

        def get(kind: str, name: str) -> bytes | None:
            fetched.append(name)
            return real_get(kind, name)

        def scan(kind: str, prefix: str = "") -> Any:
            scans.append(prefix)
            return real_scan(kind, prefix)

        store.get = get  # type: ignore[method-assign]
        store.scan = scan  # type: ignore[method-assign]
        try:
            cold = ARCMemory(store=store)
            assert _ids(cold.render_segments(sid, "agentA")) == warm
        finally:
            store.get, store.scan = real_get, real_scan  # type: ignore[method-assign]
        index_name = f"{sid}__{INDEX_SCOPE.replace('/', '~')}"
        assert index_name in fetched
        lane = [n for n in fetched if "___events~w~" in n and n != index_name]
        chunk_names = {n.split("__", 1)[1].replace("~", "/") for n in lane}
        assert chunk_names == set(entry.chunks), "a cold read touched chunks behind the anchor"
        assert not scans, "a cold read scanned the store"
    finally:
        store.clear()


def test_a_session_stored_without_the_index_is_migrated_once() -> None:
    """The pre-11a layout: unchunked ``_events/w/<span>`` partitions holding the atoms
    (logical scope, order, clock), a ``ws_op`` delete, a summary, a zero-segment
    live-text companion per scope, and no index record."""
    store = _store()
    try:
        sid = "legacy_" + uuid.uuid4().hex[:8]

        def atom(kind: str, text: str, order: float, lt: int, **kw: Any) -> Segment:
            content = {"text": text} if kind != "ws_op" else kw.pop("content")
            return Segment(
                scope=kw.pop("scope", "agentA"),
                kind=kind,  # type: ignore[arg-type]
                content=content,
                session_id=sid,
                step=0,
                order=order,
                logical_time=lt,
                **kw,
            )

        a1 = atom("thought", "a1", 1.0, 1)
        a2 = atom("observation", "a2", 2.0, 2)
        a3 = atom("observation", "a3", 3.0, 3)
        b1 = atom("thought", "b1", 1.0, 4, scope="agentB")
        delete = atom("ws_op", "", 0.0, 5, content={"op": "delete", "targets": [a3.id]})
        summary = atom("summary", "SUM", 1.0, 6, derived_from=[a1.id, a2.id])
        a4 = atom("observation", "a4", 4.0, 7)
        legacy = {
            "_events/w/span1": [a1, a2, a3, b1],
            "_events/w/_": [delete],
            "_events/w/span2": [summary, a4],
        }
        for scope, atoms in legacy.items():
            store.put("segments", f"{sid}__{scope.replace('/', '~')}", encode_segments(atoms))
        for scope in ("agentA", "agentB"):  # the old live-text companions
            store.put("segments", f"{sid}__{scope}", encode_segments([]), search_text="old text")

        arc = ARCMemory(store=store)
        assert [s.content["text"] for s in arc.render_segments(sid, "agentA")] == ["SUM", "a4"]
        assert [s.content["text"] for s in arc.render_segments(sid, "agentB")] == ["b1"]
        expected = fold_atoms(
            [a for atoms in legacy.values() for a in atoms],
            "agentA",
            as_of=None,
            include_tombstoned=True,
        )
        assert _shape(arc.list_segments(sid, "agentA", include_tombstoned=True)) == _shape(expected)

        raw = store.get("segments", f"{sid}__{INDEX_SCOPE.replace('/', '~')}")
        assert raw is not None, "the migration wrote the index"
        index = decode_index(sid, raw)
        anchor = index.scopes["agentA"].anchor
        assert anchor is not None and anchor.atom_id == summary.id
        assert store.get("segments", f"{sid}__agentA") is None, "the live-text companion stays"
        names = [n for n, _ in store.scan("segments", prefix=f"{sid}___search~")]
        assert names, "the append-only companion was written"

        # Appends continue the migrated scope with the plain store's order.
        nxt = arc.append_segment(sid, "agentA", "observation", {"text": "a5"})
        assert nxt.order == 5.0

        # A second process reads the index: no scan.
        scans: list[str] = []
        real_scan = store.scan

        def scan(kind: str, prefix: str = "") -> Any:
            scans.append(prefix)
            return real_scan(kind, prefix)

        store.scan = scan  # type: ignore[method-assign]
        try:
            again = ARCMemory(store=store)
            texts = [s.content["text"] for s in again.render_segments(sid, "agentA")]
        finally:
            store.scan = real_scan  # type: ignore[method-assign]
        assert texts == ["SUM", "a4", "a5"]
        assert not scans
    finally:
        store.clear()


def test_an_undecodable_index_is_a_typed_error() -> None:
    from clio_agent.arc.lane_index import ContextIndexError

    store = _store()
    try:
        sid = "bad_" + uuid.uuid4().hex[:8]
        store.put("segments", f"{sid}__{INDEX_SCOPE.replace('/', '~')}", b"\x00not-msgpack")
        arc = ARCMemory(store=store)
        with pytest.raises(ContextIndexError):
            arc.render_segments(sid, "agentA")
    finally:
        store.clear()


def test_the_index_is_plain_msgpack() -> None:
    """The index record decodes without the fold (a stable on-disk shape)."""
    store = _store()
    try:
        arc = ARCMemory(store=store)
        sid = "cv_" + uuid.uuid4().hex[:10]
        arc.append_segment(sid, "agentA", "thought", {"text": "x"})
        raw = store.get("segments", f"{sid}__{INDEX_SCOPE.replace('/', '~')}")
        assert raw is not None
        plain = msgspec.msgpack.decode(raw)
        assert plain["scopes"]["agentA"]["chunks"] == ["_events/w/_"]
        assert isinstance(arc._segments, FoldingSegmentStore)
    finally:
        store.clear()
