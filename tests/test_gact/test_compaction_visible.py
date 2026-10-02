"""Phase 11b: compaction is visible and lossless, on the REAL composition.

Every test drives the real :class:`ClioReAct` loop inside a real gact turn
(``POST /v1/sessions/{sid}/messages``) on real clio-core, with one scripted engine that
records every request the loop sends. The summarizer LM is the app agent's
``_run_chat_agent`` (scripted here: it is a separate LM call, not the loop).

What is pinned:

* after an auto-compaction mid-turn the next request is system + summary + the user
  question verbatim + the new step, and the transcript reads steps, the summarization
  injection, later steps, answer -- the same live and after a reload;
* ``compaction.started`` then ``compaction.completed`` (trigger ``auto``) on the
  highway and the v3 wire; a record that cannot be written emits ``compaction.failed``
  and folds nothing;
* ``recall_context`` returns a compacted step byte-exact from clio-core;
* a manual/panel compaction (``POST /compact?scope=``) gives the same events and
  record; ``/context/compact`` is gone;
* a stored legacy ``compaction`` part renders as the summarization injection;
* the policy alternative (keep the last 2 turns) changes the request accordingly.

SABOTAGE (run manually, each turns the named test red):

* ``compaction._fold``: drop ``position=0`` -> the summary renders after the question:
  ``test_auto_compaction_mid_turn_...`` (request order).
* ``compaction_policy.post_compaction_context``: drop the ``keep_head`` branch -> the
  question is summarized: ``test_auto_compaction_mid_turn_...``.
* ``compaction._summarize_record_fold``: fold before ``write_record`` -> the failed
  record leaves a folded context: ``test_a_record_that_cannot_be_written_...``.
* ``compaction_record._in_open_turn``: append to the transcript at the end of the
  turn instead of at the step boundary -> transcript order:
  ``test_auto_compaction_mid_turn_...``.
* ``protocol.v3.message.part_to_v3_block``: drop the legacy projection -> the old
  part renders as an unknown block: ``test_a_stored_compaction_part_...``.
* ``recall_context_tool._history``: read only live segments -> the compacted id is
  missing: ``test_recall_context_returns_a_compacted_step_byte_exact``.
* ``compaction_policy``: ignore ``keep_last_turns`` -> the earlier turns are
  summarized: ``test_keeping_the_last_two_turns_...``.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import dspy
import pytest
from dspy.lm15 import Message as LMMessage
from fastapi.testclient import TestClient

import clio_agent.gact.runtime.context_tokens as context_tokens
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact import context as ctx
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.agents.tool_instrumentation import instrument_tools
from clio_agent.gact.app import build_app
from clio_agent.gact.protocol.v3.event import event_to_v3
from clio_agent.gact.protocol.v3.message import part_to_v3_block
from clio_agent.gact.recall_context_tool import build_recall_context_tool
from clio_agent.gact.summarization_record import as_summarization
from tests._harness import install_scripted_module
from tests._scripted_engine import Reply, ScriptedEngine, calls, scripted_lm, summarize
from tests.turn_signals import post_turn_and_wait

SCOPE = "main"
WINDOW = 1000
SUMMARY = "SUMMARY: searched q0 and q1; hits HITS-q0, HITS-q1."


class SummarizerAgent:
    """The app's LM agent: only its summarizer call is used (scripted, recorded)."""

    def __init__(self, summary: str = SUMMARY) -> None:
        self.summary = summary
        self.prompts: list[str] = []

    def forward(self, question: str, session_id: str) -> Any:  # pragma: no cover - unused
        raise AssertionError("the turn runs the scripted ClioReAct module")

    def _run_chat_agent(self, prompt: str, _session_id: str) -> str:
        self.prompts.append(prompt)
        return self.summary


class _PromptTokens:
    """``_last_prompt_tokens`` reads: over the threshold only on the chosen reads."""

    def __init__(self, high_on: set[int]) -> None:
        self.high_on = high_on
        self.reads = 0
        self._lock = threading.Lock()

    def __call__(self) -> int:
        with self._lock:
            read, self.reads = self.reads, self.reads + 1
        return 950 if read in self.high_on else 10


def _search(q: str) -> str:
    return f"HITS-{q}"


def _tools() -> list[Any]:
    return instrument_tools([dspy.Tool(_search, name="search"), build_recall_context_tool()])


def _install_loop(
    monkeypatch: pytest.MonkeyPatch, engines: list[ScriptedEngine], scripts: list[list[Reply]]
) -> None:
    """Every turn runs a real ClioReAct over the next script (requests recorded)."""

    def run(question: str, session_id: str, **_kwargs: Any) -> Any:
        lm, engine = scripted_lm(scripts.pop(0))
        engines.append(engine)
        tokens = [
            ctx.set_react_scope(SCOPE),
            ctx.set_react_session(session_id),
            ctx.set_react_window(WINDOW),
        ]
        try:
            with dspy.context(lm=lm):
                return ClioReAct("question -> answer", tools=_tools())(question=question)
        finally:
            for token in reversed(tokens):
                ctx.reset(token)

    install_scripted_module(monkeypatch, run)


@pytest.fixture
def app_env(tmp_path: Path) -> Iterator[tuple[Any, TestClient, SummarizerAgent, list[Any]]]:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    agent = SummarizerAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent, arc=arc)
    events: list[Any] = []
    real_emit = app.state.semantic_event_sink.emit
    app.state.semantic_event_sink.emit = lambda e: (events.append(e), real_emit(e))[1]
    with TestClient(app) as client:
        yield app, client, agent, events


def _session(client: TestClient) -> str:
    return str(client.post("/v1/sessions", json={"title": "compaction"}).json()["id"])


def _turn(client: TestClient, sid: str, text: str) -> None:
    status = post_turn_and_wait(client, sid, {"parts": [{"type": "text", "text": text}]})
    assert status == "idle", status


def _compaction_events(events: list[Any]) -> list[Any]:
    return [e for e in events if e.event_type.startswith("compaction.")]


def _texts(messages: tuple[LMMessage, ...] | list[LMMessage]) -> list[tuple[str, list[Any]]]:
    return summarize(list(messages))


def _transcript(client: TestClient, sid: str) -> list[dict[str, Any]]:
    rows = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    return list(reversed(rows))  # served newest-first


def _reload(app: Any, client: TestClient, sid: str) -> list[dict[str, Any]]:
    """GET /messages after evicting the resident ledger (rehydrated from storage)."""
    app.state.messages.clear()
    return _transcript(client, sid)


def _is_record(part: Any) -> bool:
    return as_summarization(part) is not None


def _kinds(message: dict[str, Any]) -> list[str]:
    out = []
    for part in message["parts"]:
        kind = part["type"]
        if kind == "injection":
            kind = f"injection:{part['source']}"
        if kind in {"tool_call", "tool_result", "text", "injection:summarization", "notice"}:
            out.append(kind)
    return out


def _patch_tokens(monkeypatch: pytest.MonkeyPatch, high_on: set[int]) -> _PromptTokens:
    reads = _PromptTokens(high_on)
    monkeypatch.setattr(context_tokens, "_last_prompt_tokens", reads)
    return reads


def test_auto_compaction_mid_turn_shows_summary_then_question_and_reloads_the_same(
    app_env: tuple[Any, TestClient, SummarizerAgent, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client, summarizer, events = app_env
    sid = _session(client)
    engines: list[ScriptedEngine] = []
    _install_loop(
        monkeypatch,
        engines,
        [
            [
                calls(("search", {"q": "q0"}), text="look up q0"),
                calls(("search", {"q": "q1"}), text="look up q1"),
                calls(("search", {"q": "q2"}), text="look up q2"),
                Reply(text="FINAL: the answer"),
            ]
        ],
    )
    _patch_tokens(monkeypatch, high_on={2})  # the step-2 boundary crosses the threshold
    _turn(client, sid, "which stations are near LA?")
    [engine] = engines
    requests = engine.requests
    assert len(requests) == 4

    head = requests[0].messages[-1]
    assert head.role == "user" and "which stations are near LA?" in head.parts[0].text
    [cmp] = [e for e in _compaction_events(events) if e.event_type == "compaction.completed"]
    recall = (
        f'[The steps this summary replaced are kept in full: recall_context(ids=["'
        f'{cmp.payload["compaction_id"]}"]) returns them byte-exact; '
        'recall_context(query="...") searches them.]'
    )
    [record_part] = [p for m in app.state.messages.get(sid, []) for p in m.parts if _is_record(p)]
    assert record_part.text.startswith(SUMMARY) and record_part.text.endswith("\n\n" + recall)
    summary_text = f"[earlier context]\n{record_part.text}"
    # The request right after the fold: system + summary + the question verbatim.
    assert requests[2].system == requests[0].system
    assert _texts(requests[2].messages) == [
        ("user", [("text", summary_text)]),
        _texts([head])[0],
    ]
    # ... and the next one adds the new step (and nothing compacted).
    assert _texts(requests[3].messages)[:2] == _texts(requests[2].messages)
    assert _texts(requests[3].messages)[2:] == [
        ("assistant", [("text", "look up q2"), ("call", "call_2_0", "search", {"q": "q2"})]),
        ("tool", [("result", "call_2_0", "HITS-q2", False)]),
    ]
    assert "HITS-q1" in summarizer.prompts[0] and "HITS-q2" not in summarizer.prompts[0]

    # Events: started then completed, auto, mid-turn, paired by id.
    seq = _compaction_events(events)
    assert [e.event_type for e in seq] == ["compaction.started", "compaction.completed"]
    started, completed = (e.payload for e in seq)
    assert started["trigger"] == completed["trigger"] == "auto"
    assert started["compaction_id"] == completed["compaction_id"]
    assert started["scope"] == SCOPE and started["turn_id"]
    assert completed["replaced_count"] > 0

    # The transcript: steps, the summarization injection, later steps, the answer.
    live = [m.model_dump(exclude_none=True) for m in app.state.messages.get(sid, [])]
    assistant = [m for m in live if m["role"] == "assistant"]
    assert len(assistant) == 1
    order = _kinds(assistant[0])
    step = ["text", "tool_call", "tool_result"]  # the step's visible text, call, result
    assert order == [*step, *step, "injection:summarization", *step, "text"], order
    [record] = [p for p in assistant[0]["parts"] if _is_record(p)]
    assert record["text"] == summary_text.removeprefix("[earlier context]\n")
    assert (record["trigger"], record["compaction_id"]) == ("auto", completed["compaction_id"])
    assert record["id"] == completed["part_id"] and assistant[0]["id"] == completed["message_id"]
    assert record["metadata"]["derived_from"]

    # Reload from clio-core == live (GET /messages, the wire both read).
    live_wire = _transcript(client, sid)
    app.state.messages.clear()
    served = _transcript(client, sid)
    assert [_kinds(m) for m in served if m["role"] == "assistant"] == [order]
    assert [[p for p in m["parts"] if _is_record(p)] for m in served] == [
        [p for p in m["parts"] if _is_record(p)] for m in live_wire
    ]
    assert [p["id"] for m in served for p in m["parts"]] == [
        p["id"] for m in live_wire for p in m["parts"]
    ]

    # The v3 wire carries the same events and the record block.
    v3 = [event_to_v3(e) for e in app.state.bus.session_events_since(sid, cursor=0)]
    typed = [env for env in v3 if env["type"].startswith("compaction.")]
    assert [env["type"] for env in typed] == ["compaction.started", "compaction.completed"]
    assert typed[1]["payload"]["part_id"] == record["id"]
    assert typed[1]["entity_id"] == completed["compaction_id"]
    block = part_to_v3_block(record)
    assert (block["type"], block["source"], block["trigger"]) == (
        "injection",
        "summarization",
        "auto",
    )


def _three_step_turn() -> list[Reply]:
    return [
        calls(("search", {"q": "q0"}), text="look up q0"),
        calls(("search", {"q": "q1"}), text="look up q1"),
        calls(("search", {"q": "q2"}), text="look up q2"),
        Reply(text="FINAL: the answer"),
    ]


def _live(app: Any, sid: str) -> list[Any]:
    return list(app.state.arc.render_working_set(sid, SCOPE))


def test_a_record_that_cannot_be_written_fails_typed_and_folds_nothing(
    app_env: tuple[Any, TestClient, SummarizerAgent, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import part_atom_minter

    app, client, _summarizer, events = app_env
    sid = _session(client)
    real_append = part_atom_minter.append_part_atom
    live_at_compaction: list[list[str]] = []

    def refusing_append(store: Any, session_id: str, content: dict[str, Any]) -> Any:
        if as_summarization(content.get("part") or {}) is not None:
            live_at_compaction.append([s.id for s in _live(app, session_id)])
            raise RuntimeError("clio-core refused the put")
        return real_append(store, session_id, content)

    monkeypatch.setattr(part_atom_minter, "append_part_atom", refusing_append)
    engines: list[ScriptedEngine] = []
    _install_loop(monkeypatch, engines, [_three_step_turn()])
    _patch_tokens(monkeypatch, high_on={2})
    status = post_turn_and_wait(client, sid, {"parts": [{"type": "text", "text": "near LA?"}]})

    assert status == "error"  # the turn fails typed (auto_compaction_failed)
    seq = _compaction_events(events)
    assert [e.event_type for e in seq] == ["compaction.started", "compaction.failed"]
    failed = seq[1].payload
    assert failed["compaction_id"] == seq[0].payload["compaction_id"]
    assert failed["trigger"] == "auto"
    assert failed["error"]["code"] == "compaction_record_write_failed"
    # Nothing folded: the context is exactly what it was when the record failed.
    [before] = live_at_compaction
    assert [s.id for s in _live(app, sid)][: len(before)] == before
    assert all(s.kind != "summary" for s in _live(app, sid))

    # The failure is recorded where it happened: a notice after the two steps, in the
    # turn's (failed) assistant message, live and reloaded the same; never a record.
    step = ["text", "tool_call", "tool_result"]
    live_wire = _transcript(client, sid)
    for served in (live_wire, _reload(app, client, sid)):
        [assistant] = [m for m in served if m["role"] == "assistant"]
        assert _kinds(assistant)[:7] == [*step, *step, "notice"], _kinds(assistant)
        [notice] = [p for p in assistant["parts"] if p["type"] == "notice"]
        assert notice["id"] == failed["part_id"] and assistant["id"] == failed["message_id"]
        assert not [p for m in served for p in m["parts"] if _is_record(p)]
    assert {k: notice[k] for k in ("source", "code", "trigger", "compaction_id")} == {
        "source": "compaction_failed",
        "code": "compaction_record_write_failed",
        "trigger": "auto",
        "compaction_id": failed["compaction_id"],
    }
    assert notice["text"].startswith("Summarizing the context failed")
    assert part_to_v3_block(notice) == {
        "id": notice["id"],
        "type": "notice",
        "source": "compaction_failed",
        "text": notice["text"],
        "code": "compaction_record_write_failed",
        "trigger": "auto",
        "compaction_id": failed["compaction_id"],
        "agent_id": SCOPE,
        "sequence": notice["sequence"],
    }


def test_a_failed_fold_retracts_the_record_and_records_a_notice_the_model_never_sees(
    app_env: tuple[Any, TestClient, SummarizerAgent, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.transcript_projection import assemble_session_messages

    app, client, _summarizer, events = app_env
    sid = _session(client)
    engines: list[ScriptedEngine] = []
    scripts = [_three_step_turn()]
    _install_loop(monkeypatch, engines, scripts)
    _patch_tokens(monkeypatch, high_on=set())
    _turn(client, sid, "near LA?")
    rows_before = _transcript(client, sid)
    before = [s.id for s in _live(app, sid)]
    real_fold = app.state.arc.summarize_segments

    def refusing_fold(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("clio-core refused the summarize")

    monkeypatch.setattr(app.state.arc, "summarize_segments", refusing_fold)
    r = client.post(f"/v1/sessions/{sid}/compact", params={"scope": SCOPE})

    assert r.status_code == 500 and r.json()["error"]["error"] == "fold_failed"
    seq = _compaction_events(events)
    assert [e.event_type for e in seq] == ["compaction.started", "compaction.failed"]
    failed = seq[1].payload
    assert (failed["error"]["code"], failed["trigger"], failed["turn_id"]) == (
        "fold_failed",
        "manual",
        "",
    )
    assert [s.id for s in _live(app, sid)] == before  # nothing folded

    # Between turns the failure is its own row: one notice part, no record. The
    # canonical log (clio-core) agrees: the record written before the fold is retracted.
    live_wire = _transcript(client, sid)
    assert live_wire[: len(rows_before)] == rows_before
    [row] = live_wire[len(rows_before) :]
    assert (row["id"], [p["type"] for p in row["parts"]]) == (failed["message_id"], ["notice"])
    assert row["parts"][0]["id"] == failed["part_id"]
    canonical = [m.to_wire() for m in assemble_session_messages(app.state.arc, sid)]
    assert [m["id"] for m in canonical] == [m["id"] for m in live_wire]
    assert not [p for m in canonical for p in m["parts"] if _is_record(p)]
    assert _reload(app, client, sid) == live_wire

    # The model is never told: not in the next turn, not when a new agent scope joins
    # the conversation and is seeded from the transcript.
    monkeypatch.setattr(app.state.arc, "summarize_segments", real_fold)
    scripts.append([Reply(text="ok")])
    _turn(client, sid, "and then?")
    notice_text = row["parts"][0]["text"]
    assert notice_text not in json.dumps(_texts(engines[1].requests[0].messages))
    scripts.append([Reply(text="ok")])
    monkeypatch.setattr(sys.modules[__name__], "SCOPE", "newcomer")
    _turn(client, sid, "and now?")
    seeded = json.dumps(_texts(engines[2].requests[0].messages))
    assert "near LA?" in seeded and notice_text not in seeded


def test_recall_context_returns_a_compacted_step_byte_exact(
    app_env: tuple[Any, TestClient, SummarizerAgent, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client, _summarizer, events = app_env
    sid = _session(client)
    engines: list[ScriptedEngine] = []
    scripts = [_three_step_turn()]
    _install_loop(monkeypatch, engines, scripts)
    _patch_tokens(monkeypatch, high_on={2})
    _turn(client, sid, "which stations are near LA?")
    [done] = [e.payload for e in _compaction_events(events) if e.event_type.endswith("completed")]

    # The agent asks for the originals by the compaction id its summary names, then
    # searches; both in a real turn, results as the model gets them.
    scripts.append(
        [
            calls(("recall_context", {"ids": [done["compaction_id"]]})),
            calls(("recall_context", {"query": "q0"})),
            Reply(text="recalled"),
        ]
    )
    _turn(client, sid, "what exactly did the first search return?")
    by_ids, by_query = engines[1].requests[1], engines[1].requests[2]

    [(_, [(_, _, recalled_text, is_error)])] = _texts(by_ids.messages[-1:])
    assert not is_error
    recalled = {row["id"]: row for row in json.loads(recalled_text)}
    history = {s.id: s for s in app.state.arc.list_segments(sid, SCOPE, include_tombstoned=True)}
    replaced = [history[i] for i in recalled]
    assert replaced and all(s.status == "tombstoned" for s in replaced)
    assert all(recalled[s.id]["compacted"] for s in replaced)
    for seg in replaced:  # byte-exact: the stored content, as clio-core holds it
        assert json.dumps(recalled[seg.id]["content"], sort_keys=True) == json.dumps(
            seg.content, sort_keys=True
        )
    obs = [row for row in recalled.values() if row["kind"] == "observation"][0]
    first_result = _texts(engines[0].requests[1].messages)[-1]
    assert first_result == ("tool", [("result", "call_0_0", obs["content"]["text"], False)])

    [(_, [(_, _, searched_text, searched_error)])] = _texts(by_query.messages[-1:])
    if app.state.arc._segments.supports_search():
        assert not searched_error and json.loads(searched_text)
    else:  # #905: a plain tool error naming the typed reason, never a crash
        assert searched_error and "search" in searched_text


def test_panel_compaction_with_scope_gives_the_same_events_and_record(
    app_env: tuple[Any, TestClient, SummarizerAgent, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client, summarizer, events = app_env
    sid = _session(client)
    engines: list[ScriptedEngine] = []
    scripts = [_three_step_turn()]
    _install_loop(monkeypatch, engines, scripts)
    _patch_tokens(monkeypatch, high_on=set())
    _turn(client, sid, "which stations are near LA?")

    app.state.arc.append_segment(sid, "helper", "user", {"text": "a helper's own task"}, step=0)
    helper = [s.id for s in app.state.arc.render_working_set(sid, "helper")]
    r = client.post(f"/v1/sessions/{sid}/compact", params={"scope": SCOPE}, json={})
    assert r.status_code == 200, r.text
    [done] = r.json()["compactions"]  # only the scope asked for
    assert [s.id for s in app.state.arc.render_working_set(sid, "helper")] == helper
    seq = _compaction_events(events)
    assert [e.event_type for e in seq] == ["compaction.started", "compaction.completed"]
    started, completed = (e.payload for e in seq)
    assert started == {k: completed[k] for k in started}
    assert (started["trigger"], started["scope"], started["turn_id"]) == ("manual", SCOPE, "")
    assert {k: v for k, v in done.items() if k != "summary"} == completed

    # Between turns the record is its own row: one assistant message, one injection.
    live_wire = _transcript(client, sid)
    row = live_wire[-1]
    assert row["id"] == completed["message_id"] and row["role"] == "assistant"
    [record] = row["parts"]
    assert (record["type"], record["source"], record["trigger"]) == (
        "injection",
        "summarization",
        "manual",
    )
    assert record["text"] == done["summary"]
    app.state.messages.clear()
    assert _transcript(client, sid) == live_wire  # reload == live

    # The next turn's first request: system + summary + the new question.
    scripts.append([Reply(text="ok")])
    _turn(client, sid, "and their elevations?")
    first = _texts(engines[1].requests[0].messages)
    assert first[0] == ("user", [("text", f"[earlier context]\n{done['summary']}")])
    assert "and their elevations?" in first[-1][1][0][1]
    assert not any("HITS-q2" in str(m) for m in first[1:])
    assert "HITS-q2" in summarizer.prompts[0]


def test_a_stored_compaction_part_renders_as_the_summarization_injection() -> None:
    from clio_agent.gact.protocol.v3.message import message_to_v3
    from clio_agent.gact.types import Message, Part, Tokens

    legacy = Part(
        id="part_compact_1",
        type="compaction",
        summary="older summary",
        auto=True,
        compacted_message_ids=["m1"],
        metadata={"synthetic": "compact_summary", "memory_event_id": "mem_1"},
    )
    block = part_to_v3_block(legacy.to_wire())
    assert block == {
        "id": "part_compact_1",
        "type": "injection",
        "source": "summarization",
        "text": "older summary",
        "call_id": "",
        "trigger": "auto",
        "compaction_id": "mem_1",
    }
    row = Message(
        id="msg_compact_1",
        session_id="s",
        role="assistant",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        parts=[legacy],
        tokens=Tokens(),
        stop_reason="end_turn",
    )
    assert message_to_v3(row)["blocks"] == [block]
    record = as_summarization(legacy)
    assert record is not None and record.compacted_message_ids == ("m1",)


def test_keeping_the_last_two_turns_keeps_them_verbatim_after_the_summary(
    app_env: tuple[Any, TestClient, SummarizerAgent, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, client, summarizer, _events = app_env
    monkeypatch.setenv("CLIO_COMPACTION_KEEP_LAST_TURNS", "2")
    sid = _session(client)
    engines: list[ScriptedEngine] = []
    scripts = [[Reply(text=f"answer {n}")] for n in range(3)]
    scripts.append([calls(("search", {"q": "q3"}), text="look"), Reply(text="answer 3")])
    _install_loop(monkeypatch, engines, scripts)
    # Reads: one per turn for the first three (no compaction), then turn 4's step 1.
    _patch_tokens(monkeypatch, high_on={4})
    for n in range(4):
        _turn(client, sid, f"question {n}")

    before, after = engines[3].requests  # turn 4: its first request, then after the fold
    texts = [text for _role, parts in _texts(after.messages) for _k, text, *_ in parts]
    assert texts[0].startswith("[earlier context]\n")
    rest = "\n".join(str(t) for t in texts[1:])
    assert "question 0" not in rest and "answer 0" not in rest
    for n in (1, 2):  # the last two turns, verbatim
        assert f"question {n}" in rest and f"answer {n}" in texts[1:]
    assert "question 0" in summarizer.prompts[0] and "question 1" not in summarizer.prompts[0]
    kept = [m for m in _texts(before.messages) if "question 1" in str(m)]
    assert kept and kept[0] in _texts(after.messages)
    assert any("question 3" in str(m) for m in _texts(after.messages)[-3:])


def test_the_summary_renders_first_even_when_the_oldest_context_is_kept(
    app_env: tuple[Any, TestClient, SummarizerAgent, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two kept turns come before the only summarized step: the summary still leads."""
    _app, client, summarizer, _events = app_env
    monkeypatch.setenv("CLIO_COMPACTION_KEEP_LAST_TURNS", "2")
    sid = _session(client)
    engines: list[ScriptedEngine] = []
    scripts = [[Reply(text=f"answer {n}")] for n in range(2)]
    scripts.append([calls(("search", {"q": "q2"}), text="look"), Reply(text="answer 2")])
    _install_loop(monkeypatch, engines, scripts)
    _patch_tokens(monkeypatch, high_on={3})  # turn 3's step-1 boundary
    for n in range(3):
        _turn(client, sid, f"question {n}")

    after = _texts(engines[2].requests[1].messages)
    flat = [text for _role, parts in after for _k, text, *_ in parts]
    assert flat[0].startswith("[earlier context]\n")
    assert "HITS-q2" in summarizer.prompts[0] and "question 0" not in summarizer.prompts[0]
    assert any("question 0" in t for t in flat[1:]) and "answer 1" in flat
    assert "question 2" in flat[-1]


def test_a_record_whose_turn_settled_before_the_fold_is_still_shown_live(
    app_env: tuple[Any, TestClient, SummarizerAgent, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual compaction during a turn writes its record into the turn's message;
    the turn settles before the fold lands. Live must still equal reload: the record is
    published into that message (a ``message.created`` upsert after the turn settled)."""
    from tests.turn_signals import wait_for_terminal_status

    app, client, _summarizer, events = app_env
    sid = _session(client)
    started, release = threading.Event(), threading.Event()

    def slow(q: str) -> str:
        started.set()
        release.wait()
        return f"SLOW-{q}"

    def run(question: str, session_id: str, **_kwargs: Any) -> Any:
        lm, _engine = scripted_lm([calls(("slow", {"q": "x"}), text="wait"), Reply(text="done")])
        tokens = [ctx.set_react_scope(SCOPE), ctx.set_react_session(session_id)]
        try:
            with dspy.context(lm=lm):
                tools = instrument_tools([dspy.Tool(slow, name="slow")])
                return ClioReAct("question -> answer", tools=tools)(question=question)
        finally:
            for token in reversed(tokens):
                ctx.reset(token)

    install_scripted_module(monkeypatch, run)
    _patch_tokens(monkeypatch, high_on=set())
    bus = app.state.bus
    cursor = bus.latest_event_id(sid)
    ack = client.post(
        f"/v1/sessions/{sid}/messages", json={"parts": [{"type": "text", "text": "go"}]}
    )
    assert ack.status_code == 200, ack.text
    assert started.wait(120), "the turn never reached its tool"

    real_fold = app.state.arc.summarize_segments
    settled: list[int] = []

    def fold_after_the_turn_settled(*args: Any, **kwargs: Any) -> Any:
        release.set()
        assert wait_for_terminal_status(bus, sid, after_event_id=cursor) == "idle"
        settled.append(bus.latest_event_id(sid))
        return real_fold(*args, **kwargs)

    monkeypatch.setattr(app.state.arc, "summarize_segments", fold_after_the_turn_settled)
    r = client.post(f"/v1/sessions/{sid}/compact", params={"scope": SCOPE})
    assert r.status_code == 200, r.text
    [done] = r.json()["compactions"]
    assert settled, "the fold did not wait for the turn"

    live_wire = _transcript(client, sid)
    [assistant] = [m for m in live_wire if m["role"] == "assistant"]
    assert assistant["id"] == done["message_id"]
    assert [p["id"] for p in assistant["parts"] if _is_record(p)] == [done["part_id"]]
    assert _reload(app, client, sid) == live_wire
    upserts = [
        e
        for e in bus.session_events_since(sid, cursor=settled[0] + 1)
        if e.type == "message.created" and e.payload.get("id") == done["message_id"]
    ]
    assert upserts and any(_is_record(p) for p in upserts[-1].payload["parts"])
    seq = [e.event_type for e in _compaction_events(events)]
    assert seq == ["compaction.started", "compaction.completed"]


def test_the_sdk_reads_the_record_and_the_notice_as_typed_parts() -> None:
    """The client SDK's ``Part`` declares the injection and notice fields (no extras)."""
    from clio_agent.gact.summarization_record import failure_notice_part, summarization_part
    from clio_agent.sdk.types import Part as SdkPart

    record = summarization_part(
        "S", trigger="auto", compaction_id="cmp_1", derived_from=["a"], compacted_message_ids=[]
    )
    notice = failure_notice_part("F", code="fold_failed", compaction_id="cmp_2", trigger="manual")
    parsed = SdkPart.model_validate(record.to_wire())
    assert (parsed.type, parsed.source, parsed.trigger, parsed.compaction_id) == (
        "injection",
        "summarization",
        "auto",
        "cmp_1",
    )
    parsed = SdkPart.model_validate(notice.to_wire())
    assert (parsed.type, parsed.source, parsed.code, parsed.compaction_id, parsed.trigger) == (
        "notice",
        "compaction_failed",
        "fold_failed",
        "cmp_2",
        "manual",
    )
    assert not (parsed.model_extra or {})
