"""Evidence-board plan/source references come from the recall index, unchanged."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

from clio_agent.arc.live import _MemoryStore
from clio_agent.gact import context_reference_evidence as cre
from clio_agent.gact.context_reference_evidence import (
    EVIDENCE_BUDGET_REASON,
    evidence_reference_snapshots,
    find_evidence_reference_snapshot,
)
from clio_agent.gact.recall_index import RecallIndex
from clio_agent.gact.types import Message, Part

WS = "ws_e"


def _session(sid: str, updated: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=sid, title=f"T {sid}", workspace_id=WS, updated_at=updated, last_interaction_at=""
    )


def _corpus() -> dict[str, list[Message]]:
    def msg(sid: str, mid: str, *parts: Part) -> Message:
        at = "2026-10-01T00:00:00Z"
        return Message(
            id=mid,
            session_id=sid,
            role="assistant",
            created_at=at,
            updated_at=at,
            parts=list(parts),
        )

    return {
        "s1": [
            msg(
                "s1",
                "m1",
                Part(id="p", type="plan", title="Plan A", text="do things"),
                Part(
                    id="r",
                    type="resource_link",
                    uri="https://data.example/x.csv",
                    name="x",
                    metadata={"paper_url": "https://doi.example/1"},
                ),
            ),
            msg("s1", "m2", Part(id="q", type="text", text="see https://doi.example/1")),
        ],
        "s2": [msg("s2", "m3", Part(type="compaction", summary="earlier work", auto=True))],
    }


def _app(corpus: dict[str, list[Message]], *, indexed: bool) -> SimpleNamespace:
    sessions = {
        "s1": _session("s1", "2026-10-01T00:00:00Z"),
        "s2": _session("s2", "2026-10-05T00:00:00Z"),
    }
    index = None
    if indexed:
        index = RecallIndex(_MemoryStore(), lambda sid: corpus[sid], inline=True)
        for sid in corpus:
            index.rebuild(sid)
    registry = SimpleNamespace(list=lambda workspace_id=None: list(sessions.values()))
    return SimpleNamespace(
        state=SimpleNamespace(sessions=registry, messages=corpus, recall_index=index, arc=None)
    )


def test_plan_and_source_rows_keep_their_identity_and_payload() -> None:
    corpus = _corpus()
    rows = evidence_reference_snapshots(_app(corpus, indexed=True), WS, ["plan", "evidence_source"])
    by_kind: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for row, payload in rows:
        by_kind.setdefault(row["kind"], []).append((row, payload))
    plans = by_kind["plan"]
    assert [row["id"] for row, _ in plans] == ["s1:m1:p", "s2:m3:0"]
    assert plans[0][1] == {
        "session_id": "s1",
        "message_id": "m1",
        "part_id": "p",
        "title": "Plan A",
        "detail": "do things",
        "type": "plan",
    }
    assert plans[1][1]["type"] == "summarization" and plans[1][1]["detail"] == "earlier work"
    sources = by_kind["evidence_source"]
    expected_ids = [
        "source:" + hashlib.sha256(f"s1:m1:{source_id}:{value}".encode()).hexdigest()[:20]
        for source_id, value in (
            ("r", "https://data.example/x.csv"),
            ("r:paper_url", "https://doi.example/1"),
        )
    ]
    assert [row["id"] for row, _ in sources] == expected_ids
    assert sources[1][0]["label"] == "Paper"


def test_index_and_transcript_reads_give_the_same_rows() -> None:
    corpus = _corpus()
    kinds = ["plan", "evidence_source"]
    indexed = evidence_reference_snapshots(_app(corpus, indexed=True), WS, kinds)
    plain = evidence_reference_snapshots(_app(corpus, indexed=False), WS, kinds)
    assert indexed == plain
    found = find_evidence_reference_snapshot(_app(corpus, indexed=True), WS, "plan", "s1:m1:p")
    assert found is not None and found[0]["label"] == "Plan A"


def test_over_the_budget_the_cut_is_a_typed_degradation(monkeypatch) -> None:
    corpus = _corpus()
    app = _app(corpus, indexed=True)
    monkeypatch.setattr(cre, "evidence_session_budget", lambda: 1)
    rows = evidence_reference_snapshots(app, WS, ["plan"])
    assert [row["id"] for row, _ in rows] == ["s2:m3:0"]  # the most recently active
    reasons = [d["reason"] for d in app.state.reference_search_degradations[WS]]
    assert reasons == [EVIDENCE_BUDGET_REASON]
