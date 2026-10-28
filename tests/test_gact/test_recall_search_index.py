"""Recall answers from the index exactly as it answered from whole transcripts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from clio_agent.arc.live import _MemoryStore
from clio_agent.gact.recall_index import RecallIndex
from clio_agent.gact.runtime import session_recall_search as srs
from clio_agent.gact.runtime.session_recall_search import (
    SESSION_BUDGET_REASON,
    _recall_search_excerpt,
    _recall_search_phrase,
    _recall_search_response,
    _recall_search_terms,
)
from clio_agent.gact.types import Message, Part, SessionRecallHit
from clio_agent.gact.workspace_scope import session_scope_label

WS = "ws_a"


def _session(sid: str, *, updated: str = "2026-10-01T00:00:00Z") -> SimpleNamespace:
    return SimpleNamespace(
        id=sid,
        title=f"title {sid}",
        workspace_id=WS,
        metadata={},
        updated_at=updated,
        last_interaction_at=updated,
    )


class _Sessions:
    def __init__(self, sessions: list[SimpleNamespace]) -> None:
        self._rows = {s.id: s for s in sessions}

    def get(self, sid: str) -> Any:
        return self._rows.get(sid)

    def list(self, *, workspace_id: str | None = None) -> list[Any]:
        return [
            s for s in self._rows.values() if not workspace_id or s.workspace_id == workspace_id
        ]


def _msg(sid: str, mid: str, at: str, *texts: str, role: str = "assistant") -> Message:
    return Message(
        id=mid,
        session_id=sid,
        role=role,
        created_at=at,
        updated_at=at,
        parts=[Part(id=f"{mid}-{i}", type="text", text=text) for i, text in enumerate(texts)],
    )


def _corpus() -> dict[str, list[Message]]:
    t1, t2 = "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z"
    long = "filler " * 200 + "the survey dataset results " + "tail " * 200
    return {
        "s1": [
            _msg("s1", "a", t1, "survey dataset 17", "a survey of things", role="user"),
            _msg("s1", "b", t2, "dataset only", long),
        ],
        "s2": [
            _msg("s2", "c", t1, "Survey **dataset** 17 again"),  # rendered phrase
            _msg("s2", "d", t1, "survey here too", "and survey"),  # ties on created_at
        ],
        "s3": [_msg("s3", "e", t2, "unrelated words")],
        "s4": [_msg("s4", "f", t1, "17 surveys and a dataset-ish")],
    }


def _reference(app: Any, query: str, session_ids: list[str], limit: int) -> list[dict]:
    """The pre-index algorithm: every transcript read, every hit built, then sorted."""

    terms = _recall_search_terms(query)
    hits: list[SessionRecallHit] = []
    for sid in session_ids:
        sess = app.state.sessions.get(sid)
        for message in app.state.messages[sid]:
            for part in message.parts:
                text = part.text.strip()
                lowered = text.lower()
                matched = [term for term in terms if term in lowered]
                if not text or not matched:
                    continue
                phrase = _recall_search_phrase(text, query) is not None
                if not phrase and len(matched) * 2 < len(terms):
                    continue
                score = 1.0 if phrase else 0.9 * len(set(matched)) / len(set(terms))
                label = session_scope_label(
                    active_workspace_id=WS,
                    target_workspace_id=sess.workspace_id,
                    target_session_id=sid,
                    active_session_id="",
                )
                hits.append(
                    SessionRecallHit(
                        session_id=sid,
                        session_title=sess.title,
                        workspace_id=sess.workspace_id,
                        message_id=message.id,
                        part_id=part.id,
                        role=message.role,
                        created_at=message.created_at,
                        updated_at=message.updated_at,
                        text=_recall_search_excerpt(text, matched, query=query),
                        score=round(score, 4),
                        match_terms=sorted(set(matched)),
                        metadata={
                            "cross_session": True,
                            "source": "gact_transcript",
                            "exact_phrase": phrase,
                            "scope": label,
                            "workspace_boundary": label,
                        },
                    )
                )
    hits.sort(key=lambda hit: (hit.score, hit.created_at), reverse=True)
    return [hit.model_dump() for hit in hits[:limit]]


def _app(corpus: dict[str, list[Message]], *, indexed: bool) -> SimpleNamespace:
    sessions = _Sessions([_session(sid) for sid in corpus])
    index = None
    if indexed:
        index = RecallIndex(_MemoryStore(), lambda sid: corpus[sid], inline=True)
        for sid in corpus:
            assert index.rebuild(sid)
    return SimpleNamespace(
        state=SimpleNamespace(sessions=sessions, messages=corpus, recall_index=index, arc=None)
    )


def _search(app: Any, query: str, limit: int) -> Any:
    return _recall_search_response(
        app, query=query, workspace_id=WS, include_cross_session=True, limit=limit
    )


def test_index_and_transcripts_give_the_reference_hits() -> None:
    corpus = _corpus()
    for query in ("survey", "survey dataset 17", "dataset", "survey dataset results", "zz"):
        for limit in (1, 2, 3, 20):
            expected = _reference(_app(corpus, indexed=False), query, list(corpus), limit)
            for indexed in (True, False):
                got = _search(_app(corpus, indexed=indexed), query, limit)
                assert [hit.model_dump() for hit in got.hits] == expected, (query, limit)
                assert got.searched_sessions == list(corpus)


def test_the_response_says_where_digests_came_from() -> None:
    corpus = _corpus()
    indexed = _search(_app(corpus, indexed=True), "survey", 5).metadata["index"]
    assert indexed == {"from_index": 4, "from_resident": 0, "from_transcript": 0}
    plain = _search(_app(corpus, indexed=False), "survey", 5).metadata["index"]
    assert plain["from_transcript"] == 4
    assert plain["transcript_reasons"] == {"recall_index_unavailable": 4}


def test_an_excluded_message_is_never_a_hit() -> None:
    corpus = _corpus()
    got = _recall_search_response(
        _app(corpus, indexed=True),
        query="survey",
        workspace_id=WS,
        include_cross_session=True,
        exclude_message_id="a",
    )
    assert got.hits and all(hit.message_id != "a" for hit in got.hits)


def test_over_the_session_budget_the_most_recent_are_read(monkeypatch) -> None:
    corpus = _corpus()
    app = _app(corpus, indexed=True)
    app.state.sessions.get("s3").last_interaction_at = "2026-10-09T00:00:00Z"
    app.state.sessions.get("s4").last_interaction_at = "2026-10-08T00:00:00Z"
    monkeypatch.setattr(srs, "recall_session_budget", lambda: 2)
    got = _search(app, "survey", 20)
    assert got.searched_sessions == ["s3", "s4"]
    assert {hit.session_id for hit in got.hits} == {"s4"}
    assert got.metadata["truncated"] == {
        "reason": SESSION_BUDGET_REASON,
        "searched": 2,
        "eligible": 4,
        "kept": "most_recently_active",
    }
