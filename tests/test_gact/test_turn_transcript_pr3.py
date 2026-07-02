"""#767 PR3 — finalize is a READER of the TurnTranscript ledger.

Defect-flip coverage (design §8.3): each retired reconciliation mechanism keeps
its symptom fixture, with the assertion flipped from "reconciliation removed
the duplicate" to "the producer appended exactly once". These tests FAIL on
``develop`` before PR3 (the finalize region there re-emits, swaps, or hoists)
and pin the flipped behavior here:

- mechanism 5 (``answer_already_present`` / ``reuse_streamed_part_id``): the
  canonical answer is the closed streamed part OR one batch burst — never
  both, never a text swap (#733 / #736's finalize half; the b1b25d2 invariant).
- mechanism 4 (``expert_terminal_answers`` / ``answered_agents``): a terminal
  expert's non-streamed answer lands as ONE batch burst by op identity.
- mechanism 1 (``live_routing_agents``): the ``route:{agent}`` once-key is the
  banner's identity, live or at finalize.
- ``suppressed_thinking_part``: wrap-up thinking is gated by
  ``has_closed_text`` (op identity), not substring matching.

The suite-wide live==reload fold property (design §8.2b) lives in
``conftest.py`` and runs for every turn in ``tests/test_gact``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from .test_turn_transcript_equivalence import (
    _build,
    _complete_turn,
    _PlainAgent,
    _Pred,
)


def _assistant_message(client: TestClient, sid: str) -> dict[str, Any]:
    messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    assistants = [m for m in messages if m["role"] == "assistant"]
    assert assistants, "turn did not persist an assistant message"
    return assistants[-1]


# ---------------------------------------------------------------------------
# mechanism 5: never both, never swapped
# ---------------------------------------------------------------------------


def test_streamed_answer_is_never_swapped_and_the_batch_copy_never_lands(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The answer field streamed live; ``pred.answer`` then differs from the
    streamed buffer (a paraphrase — the #736 finalize-half shape). Before PR3
    the canonical-answer append compared strings (``answer_already_present``)
    and appended a SECOND, responder-authored copy; the reuse path could also
    swap the streamed part's text. Now the batch copy is dropped by op
    identity and the streamed part keeps its own cleaned buffer."""

    async def fake_streamed_forward(
        app: Any, enriched_text: str, sid: str, emit_chunk: Any, **kwargs: Any
    ) -> Any:
        await emit_chunk("The streamed ", None, "answer")
        await emit_chunk("truth.", None, "answer")
        return _Pred(
            answer="A paraphrased batch restatement of the streamed truth.",
            selected_expert="code_expert",
            routing_rationale="matched coding keywords",
            route_source="dspy",
            route_reason="planner selected code expert",
        )

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_streamed_forward)
    app = _build(tmp_path, "noswap", _PlainAgent("unused"))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "s"}).json()["id"]
        _complete_turn(client, sid, "stream then paraphrase")
        assistant = _assistant_message(client, sid)

        text_parts = [p for p in assistant["parts"] if p["type"] == "text"]
        # Exactly ONE answer part: the streamed one, with ITS text (no swap).
        assert len(text_parts) == 1
        assert text_parts[0]["text"] == "The streamed truth."
        assert text_parts[0]["metadata"]["stream_source"] == "live"
        # The paraphrased batch copy never landed anywhere.
        assert all("paraphrased" not in (p.get("text") or "") for p in assistant["parts"])
        # And it never hit the wire either: one added text part, total.
        added_text = [
            e
            for e in app.state.bus._history.get(sid, [])
            if e.type == "message.part.added" and e.payload["part"]["type"] == "text"
        ]
        assert len(added_text) == 1


def test_batch_answer_lands_as_exactly_one_burst(tmp_path: Path) -> None:
    """Nothing streamed: the canonical answer lands as ONE added+completed
    batch burst (no synthetic deltas), authored to the responder, carrying the
    turn's stream_fallback + its signature field (design §4 row 7)."""

    app = _build(tmp_path, "burst", _PlainAgent("BATCH_ONLY_ANSWER"))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "b"}).json()["id"]
        _complete_turn(client, sid, "answer without streaming")
        assistant = _assistant_message(client, sid)

        text_parts = [p for p in assistant["parts"] if p["type"] == "text"]
        assert len(text_parts) == 1
        assert text_parts[0]["text"] == "BATCH_ONLY_ANSWER"
        assert text_parts[0]["agent_id"] == "code_expert"
        assert text_parts[0]["metadata"]["stream_source"] == "batch"
        assert text_parts[0]["metadata"]["signature_field_name"] == "answer"
        assert text_parts[0]["metadata"]["stream_fallback"]

        history = app.state.bus._history.get(sid, [])
        added = [
            e
            for e in history
            if e.type == "message.part.added" and e.payload["part"]["type"] == "text"
        ]
        completed = [e for e in history if e.type == "message.part.completed"]
        deltas = [e for e in history if e.type == "message.part.delta"]
        assert len(added) == 1
        assert len(completed) == 1
        assert completed[0].payload["final_text"] == "BATCH_ONLY_ANSWER"
        assert completed[0].payload["stream_source"] == "batch"
        assert deltas == []


# ---------------------------------------------------------------------------
# mechanism 4: terminal-expert answers settle their channel by op identity
# ---------------------------------------------------------------------------


def test_field_stream_call_site_pattern_is_exactly_once() -> None:
    """The delegation call-site pattern: deltas reach the ledger through the
    stream TAP (append_text_delta), the handle is constructed at settle time.
    Its identity must still see the streamed part (seeded from ledger state) —
    the fallback is ignored without any text comparison. A channel that never
    streamed lands one burst; a second finish is an audited no-op."""

    from clio_agent.gact.transcript import TurnTranscript

    events: list[tuple[str, dict[str, Any]]] = []

    class _Pub:
        def publish(self, event_type: str, payload: Any) -> None:
            events.append((event_type, dict(payload)))

    transcript = TurnTranscript(
        session_id="sess_x",
        turn_id="turn_x",
        publisher=_Pub(),
        clean_text=lambda text: text,
    )
    # Child A streamed its answer through the tap; the settle-time handle must
    # treat the channel as already landed.
    transcript.append_text_delta("child_a", "answer", "streamed child answer")
    landed = transcript.field_stream("child_a", "answer").finish(
        fallback_text="a COMPLETELY different batch copy"
    )
    assert landed == "streamed child answer"
    # Child B never streamed: one batch burst.
    burst = transcript.field_stream("child_b", "answer").finish(fallback_text="child b answer")
    assert burst == "child b answer"

    parts = transcript.finalize()
    answer_texts = [(p.agent_id, p.text) for p in parts if p.type == "text"]
    assert answer_texts == [
        ("child_a", "streamed child answer"),
        ("child_b", "child b answer"),
    ]
    added = [payload["part"]["id"] for etype, payload in events if etype == "message.part.added"]
    assert len(added) == 2  # exactly one added per channel — never a duplicate


def test_turn_answer_channel_covers_the_tap_attribution_label() -> None:
    """The chat path labels streamed chunks with the ACTIVE agent while the
    responder is the routed expert — the canonical answer channel covers both
    labels (one LM call, two attributions), so the already-streamed answer
    wins by identity and the responder-attributed fallback is ignored (no
    cross-agent duplicate)."""

    from clio_agent.gact.transcript import TurnTranscript

    class _Pub:
        def publish(self, event_type: str, payload: Any) -> None:
            pass

    transcript = TurnTranscript(
        session_id="sess_y",
        turn_id="turn_y",
        publisher=_Pub(),
        clean_text=lambda text: text,
    )
    transcript.append_text_delta("main", "answer", "the streamed answer")
    channel = transcript.turn_answer_stream("code_expert", "main")
    channel.finish(fallback_text="the streamed answer")

    parts = transcript.finalize()
    text_parts = [p for p in parts if p.type == "text"]
    assert len(text_parts) == 1
    assert text_parts[0].agent_id == "main"


def test_turn_answer_channel_does_not_cover_a_delegated_childs_answer() -> None:
    """A delegated child's landed answer is ITS deliverable — the responder's
    distinct final answer must still land (the root_review regression: before
    the covers-set fix a field-wide channel swallowed the parent's answer)."""

    from clio_agent.gact.transcript import TurnTranscript

    class _Pub:
        def publish(self, event_type: str, payload: Any) -> None:
            pass

    transcript = TurnTranscript(
        session_id="sess_z",
        turn_id="turn_z",
        publisher=_Pub(),
        clean_text=lambda text: text,
    )
    # The child's non-streamed answer settled at its LM-call site.
    transcript.field_stream("schema_review", "answer").finish(fallback_text="SCHEMA_OK")
    channel = transcript.turn_answer_stream("root_review", "root_review")
    landed = channel.finish(fallback_text="ROOT_FINAL")

    assert landed == "ROOT_FINAL"
    parts = transcript.finalize()
    answers = [(p.agent_id, p.text) for p in parts if p.type == "text"]
    assert answers == [("schema_review", "SCHEMA_OK"), ("root_review", "ROOT_FINAL")]


# ---------------------------------------------------------------------------
# mechanism 1: the route banner's identity is its once-key
# ---------------------------------------------------------------------------


def test_route_banner_lands_exactly_once_live_or_finalize(tmp_path: Path) -> None:
    """When the live tool observer already appended ``route:{agent}``, the
    finalize append is blocked by the SAME once-key — one banner per turn,
    without scanning live parts."""

    class _RoutedToolAgent(_PlainAgent):
        def _selected_expert_for_tool(self, tool_name: str) -> str:
            return "code_expert"

        def _parent_route_for_child(self, owner: str) -> str:
            return ""

        def forward(self, question: str, session_id: str) -> Any:
            from clio_agent.tools.execution import _GLOBAL_TOOL_OBSERVER

            observer = _GLOBAL_TOOL_OBSERVER
            assert observer is not None
            observer("fs_read_file", {"path": "README.md"}, "started", None)
            observer(
                "fs_read_file",
                {"path": "README.md"},
                "completed",
                None,
                result={"ok": True},
            )
            return _Pred(
                answer="ROUTED_TOOL_DONE",
                selected_expert="code_expert",
                routing_rationale="matched coding keywords",
                route_source="dspy",
                route_reason="planner selected code expert",
            )

    app = _build(tmp_path, "routeonce", _RoutedToolAgent("unused"))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "r"}).json()["id"]
        _complete_turn(client, sid, "call a routed tool")
        assistant = _assistant_message(client, sid)

        routing_parts = [p for p in assistant["parts"] if p["type"] == "routing_decision"]
        assert len(routing_parts) == 1
        # The LIVE banner won (it arrived first); finalize's once-key was blocked.
        assert routing_parts[0]["metadata"]["route_source"] == "live_tool_observer"
        added_routing = [
            e
            for e in app.state.bus._history.get(sid, [])
            if e.type == "message.part.added" and e.payload["part"]["type"] == "routing_decision"
        ]
        assert len(added_routing) == 1


# ---------------------------------------------------------------------------
# suppressed_thinking_part -> has_closed_text (op identity)
# ---------------------------------------------------------------------------


def test_wrap_up_thinking_gated_by_streamed_reasoning_identity(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Reasoning streamed live as a text part; ``pred.reasoning`` carries the
    same content. The wrap-up thinking part must NOT land a second copy — and
    the gate is the (responder, "reasoning") channel identity, not substring
    matching on prose."""

    async def fake_streamed_forward(
        app: Any, enriched_text: str, sid: str, emit_chunk: Any, **kwargs: Any
    ) -> Any:
        await emit_chunk("Thinking it through. ", "code_expert", "reasoning")
        await emit_chunk("Simple.", "code_expert", "reasoning")
        await emit_chunk("Answer: 42.", "code_expert", "answer")
        return _Pred(
            answer="Answer: 42.",
            reasoning="Thinking it through. Simple.",
            selected_expert="code_expert",
            routing_rationale="matched coding keywords",
            route_source="dspy",
            route_reason="planner selected code expert",
        )

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_streamed_forward)
    app = _build(tmp_path, "thinkgate", _PlainAgent("unused"))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "g"}).json()["id"]
        _complete_turn(client, sid, "think then answer")
        assistant = _assistant_message(client, sid)

        thinking_parts = [p for p in assistant["parts"] if p["type"] == "thinking"]
        assert thinking_parts == []  # already streamed as the reasoning text part
        reasoning_parts = [
            p
            for p in assistant["parts"]
            if p["type"] == "text" and p["metadata"].get("signature_field_name") == "reasoning"
        ]
        assert len(reasoning_parts) == 1
        assert reasoning_parts[0]["text"] == "Thinking it through. Simple."


def test_chat_path_streamed_reasoning_with_batch_only_answer_lands_once(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """CHAT path (no ``selected_expert`` -> no routing-banner append): the
    reasoning field streams live and the answer arrives batch-only. The
    streamed reasoning part is still OPEN when the wrap-up thinking gate runs,
    so ``has_closed_text`` (closed-state only) saw "nothing landed" and a
    verbatim batch ``thinking`` duplicate landed next to the live reasoning
    text part — a strict regression of the #732 duplicate class (on develop
    the raw-buffer probe suppressed it regardless of open/closed state). The
    turn must persist AND stream exactly [text reasoning, text answer]."""

    async def fake_streamed_forward(
        app: Any, enriched_text: str, sid: str, emit_chunk: Any, **kwargs: Any
    ) -> Any:
        await emit_chunk("Weighing the question. ", None, "reasoning")
        await emit_chunk("It is simple.", None, "reasoning")
        return _Pred(
            answer="CHAT_BATCH_ONLY_ANSWER",
            reasoning="Weighing the question. It is simple.",
            selected_expert="",
        )

    monkeypatch.setattr("clio_agent.gact.app._try_streamed_forward", fake_streamed_forward)
    app = _build(tmp_path, "chatgate", _PlainAgent("unused"))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "c"}).json()["id"]
        _complete_turn(client, sid, "think out loud then answer in batch")
        assistant = _assistant_message(client, sid)

        # The full persisted shape: the live reasoning text part, then the
        # batch answer text part — and NOTHING else (no thinking duplicate).
        shape = [
            (p["type"], (p.get("metadata") or {}).get("signature_field_name", ""))
            for p in assistant["parts"]
        ]
        assert shape == [("text", "reasoning"), ("text", "answer")], (
            f"expected exactly [text reasoning, text answer], got {shape} "
            f"(parts: {[(p['type'], (p.get('text') or '')[:40]) for p in assistant['parts']]})"
        )
        reasoning_part, answer_part = assistant["parts"]
        assert reasoning_part["text"] == "Weighing the question. It is simple."
        assert reasoning_part["metadata"]["stream_source"] == "live"
        assert answer_part["text"] == "CHAT_BATCH_ONLY_ANSWER"
        assert answer_part["metadata"]["stream_source"] == "batch"

        # The wire agrees: exactly ONE reasoning-bearing part ever streamed —
        # no batch ``thinking`` twin of the live reasoning part was added.
        history = app.state.bus._history.get(sid, [])
        reasoning_added = [
            e
            for e in history
            if e.type == "message.part.added"
            and e.payload["part"]["type"] in {"text", "thinking"}
            and "Weighing the question." in (e.payload["part"].get("text") or "")
        ]
        added_by_type = [
            e.payload["part"]["type"]
            for e in history
            if e.type == "message.part.added" and e.payload["part"]["type"] in {"text", "thinking"}
        ]
        assert added_by_type == ["text", "text"], added_by_type
        # A live part is ADDED empty and filled by deltas; only a batch twin
        # would be added already carrying the reasoning verbatim.
        assert reasoning_added == [], (
            "the streamed reasoning part gained a verbatim batch thinking twin"
        )


def test_wrap_up_thinking_lands_when_reasoning_did_not_stream(tmp_path: Path) -> None:
    """No reasoning streamed: the wrap-up thinking part lands once, after the
    live spine (arrival order), authored to the responder."""

    class _ReasoningAgent(_PlainAgent):
        def forward(self, question: str, session_id: str) -> Any:
            return _Pred(
                answer="BATCH_ANSWER",
                reasoning="Batch-only reasoning trace.",
                selected_expert="code_expert",
                routing_rationale="matched coding keywords",
                route_source="dspy",
                route_reason="planner selected code expert",
            )

    app = _build(tmp_path, "thinkland", _ReasoningAgent("unused"))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        _complete_turn(client, sid, "reason silently")
        assistant = _assistant_message(client, sid)

        types = [p["type"] for p in assistant["parts"]]
        assert types == ["routing_decision", "thinking", "text"]
        thinking = assistant["parts"][1]
        assert thinking["text"] == "Batch-only reasoning trace."
        assert thinking["agent_id"] == "code_expert"
        assert [p["sequence"] for p in assistant["parts"]] == [1, 2, 3]
